#!/usr/bin/env python3
"""
Hyperliquid Trailing Stop Bot — Production
==========================================
Auto-detects open positions and manages trailing stops.
Sends Telegram push notifications and accepts commands.

HOW TO RUN:
  1. pip install hyperliquid-python-sdk python-dotenv
  2. Create .env in the same folder:
       HL_WALLET=0xyourmainwalletaddress
       HL_BOT_KEY=0xyourbotprivatekey
       TG_TOKEN=your_telegram_bot_token
       TG_CHAT_ID=your_telegram_chat_id
  3. python trailing_stop_bot.py
  4. Leave running — Ctrl+C to stop

TELEGRAM COMMANDS:
  /status          — all positions with P&L, CDT time, duration
  /price [COIN]    — live price(s)
  /sl COIN %       — update stop loss (% from current price)
  /tp1 COIN %      — update take profit
  /pause COIN      — pause bot management for a coin
  /resume COIN     — resume management
  /close COIN      — market close a position immediately
  /defaults        — show current settings + command list
"""

import os
import json
import time
import requests
import re
import eth_account
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv
from hyperliquid.exchange import Exchange
from hyperliquid.info import Info
from hyperliquid.utils import constants

# ===============================================================
#  CONFIG
# ===============================================================

load_dotenv()
WALLET_ADDRESS  = os.getenv("HL_WALLET",   "").strip()
API_PRIVATE_KEY = os.getenv("HL_BOT_KEY",  "").strip()
TG_TOKEN        = os.getenv("TG_TOKEN",    "").strip()
TG_CHAT_ID      = os.getenv("TG_CHAT_ID",  "").strip()

CHECK_INTERVAL = 30
MAX_RETRIES    = 3
RETRY_DELAY    = 5

DEFAULTS = {
    "sl_pct":         1.5,
    "tp1_pct":        2.5,
    "tp2_pct":        5.0,
    "trail_step_pct": 0.5,
}

DYNAMIC_EXITS = True

CDT = timezone(timedelta(hours=-5))   # Central Daylight Time


SENTIMENT_CACHE_SECS = 900
_market_sentiment_cache = {
    "score": 0.0,
    "label": "Neutral",
    "checked_at": 0.0,
    "sources": [],
}

def _safe_get(url, headers=None, timeout=8):
    try:
        r = requests.get(url, headers=headers or {}, timeout=timeout)
        if r.status_code == 200:
            return r.text
    except Exception:
        pass
    return None

def _parse_ipi_greed_score(html):
    if not html:
        return None
    m = re.search(r"₿\s*Crypto Market.*?Updated:.*?BTC \$.*?(?:\n|\r|\s)+(\d{1,3})(?:\n|\r|\s)+[A-Za-z]+", html, re.S)
    if not m:
        m = re.search(r"₿\s*Crypto Market.*?(\d{1,3})", html, re.S)
    if not m:
        return None
    panic_val = int(m.group(1))
    if 0 <= panic_val <= 100:
        return max(0, min(100, 100 - panic_val))
    return None

def _parse_feargreedmeter_score(html):
    if not html:
        return None
    m = re.search(r"Now\s+([A-Za-z ]+)\s+(\d{1,3})\s+Yesterday", html, re.S)
    if not m:
        m = re.search(r"Crypto Fear\s*&\s*Greed Index.*?(\d{1,3}).*?Now", html, re.S)
    if not m:
        return None
    val = int(m.group(2) if len(m.groups()) >= 2 else m.group(1))
    if 0 <= val <= 100:
        return val
    return None

def _fetch_alternative_score():
    try:
        r = requests.get("https://api.alternative.me/fng/?limit=1&format=json", timeout=8)
        if r.status_code == 200:
            data = r.json()
            items = data.get("data", [])
            if items:
                val = int(float(items[0].get("value")))
                if 0 <= val <= 100:
                    return val
    except Exception:
        pass
    return None

def _fetch_cmc_score():
    api_key = os.getenv("CMC_API_KEY", "").strip()
    if not api_key:
        return None
    try:
        r = requests.get(
            "https://pro-api.coinmarketcap.com/v3/fear-and-greed/latest",
            headers={"X-CMC_PRO_API_KEY": api_key, "Accept": "application/json"},
            timeout=8,
        )
        if r.status_code == 200:
            data = r.json()
            val = data.get("data", {}).get("value")
            if val is None and isinstance(data.get("data"), list) and data["data"]:
                val = data["data"][0].get("value")
            if val is not None:
                val = int(float(val))
                if 0 <= val <= 100:
                    return val
    except Exception:
        pass
    return None

def _label_from_score(score):
    if score <= 24:
        return "Extreme Fear"
    if score <= 44:
        return "Fear"
    if score <= 55:
        return "Neutral"
    if score <= 74:
        return "Greed"
    return "Extreme Greed"

def get_market_sentiment_details():
    global _market_sentiment_cache
    now = time.time()
    if now - _market_sentiment_cache["checked_at"] < SENTIMENT_CACHE_SECS:
        return (
            _market_sentiment_cache["score"],
            _market_sentiment_cache["label"],
            _market_sentiment_cache["sources"],
        )

    scores = []
    sources = []

    ipi_html = _safe_get("https://www.internetpanicindex.com/")
    ipi_score = _parse_ipi_greed_score(ipi_html)
    if ipi_score is not None:
        scores.append(ipi_score)
        sources.append(("IPI", ipi_score))

    fgm_html = _safe_get("https://feargreedmeter.com/crypto")
    fgm_score = _parse_feargreedmeter_score(fgm_html)
    if fgm_score is not None:
        scores.append(fgm_score)
        sources.append(("FGM", fgm_score))

    alt_score = _fetch_alternative_score()
    if alt_score is not None:
        scores.append(alt_score)
        sources.append(("ALT", alt_score))

    cmc_score = _fetch_cmc_score()
    if cmc_score is not None:
        scores.append(cmc_score)
        sources.append(("CMC", cmc_score))

    if not scores:
        return 0.0, "Neutral", []

    ordered = sorted(scores)
    n = len(ordered)
    if n % 2 == 1:
        agg = float(ordered[n // 2])
    else:
        agg = (ordered[n // 2 - 1] + ordered[n // 2]) / 2.0

    label = _label_from_score(agg)
    bias = round((agg - 50.0) / 50.0, 4)

    _market_sentiment_cache = {
        "score": bias,
        "label": label,
        "checked_at": now,
        "sources": sources,
    }
    return bias, label, sources

def get_market_sentiment():
    score, label, _sources = get_market_sentiment_details()
    return score, label



# ===============================================================
#  SETUP
# ===============================================================

def setup():
    wallet   = eth_account.Account.from_key(API_PRIVATE_KEY)
    info     = Info(constants.MAINNET_API_URL, skip_ws=True)
    exchange = Exchange(wallet, constants.MAINNET_API_URL, account_address=WALLET_ADDRESS)
    return info, exchange


# ===============================================================
#  TELEGRAM
# ===============================================================

_last_update_id = 0


def tg(message):
    """Send Telegram notification. Fails silently."""
    if not TG_TOKEN or not TG_CHAT_ID:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            json={"chat_id": TG_CHAT_ID, "text": message, "parse_mode": "HTML"},
            timeout=5
        )
    except Exception:
        pass


def tg_get_updates():
    """Poll Telegram for new messages from the user."""
    global _last_update_id
    if not TG_TOKEN or not TG_CHAT_ID:
        return []
    try:
        res = requests.get(
            f"https://api.telegram.org/bot{TG_TOKEN}/getUpdates",
            params={"offset": _last_update_id + 1, "timeout": 1},
            timeout=5
        )
        updates = res.json().get("result", [])
        if updates:
            _last_update_id = updates[-1]["update_id"]
        return [
            u["message"] for u in updates
            if "message" in u and str(u["message"].get("chat", {}).get("id", "")) == TG_CHAT_ID
        ]
    except Exception:
        return []


# ===============================================================
#  RETRY WRAPPER
# ===============================================================

def with_retry(fn, label="call"):
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            return fn()
        except Exception as e:
            print(f"  [WARN] {label} failed (attempt {attempt}/{MAX_RETRIES}): {e}")
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_DELAY)
    print(f"  [ERROR] {label} failed after {MAX_RETRIES} attempts")
    return None


# ===============================================================
#  INFO QUERIES
# ===============================================================

def get_open_positions(info):
    state = with_retry(lambda: info.user_state(WALLET_ADDRESS), label="user_state")
    if state is None:
        return []
    positions = []
    for ap in state.get("assetPositions", []):
        p   = ap.get("position", {})
        szi = float(p.get("szi", 0))
        if szi == 0:
            continue
        positions.append({
            "coin":     p.get("coin"),
            "side":     "long" if szi > 0 else "short",
            "size":     abs(szi),
            "entry_px": float(p.get("entryPx", 0)),
            "liq_px":   float(p.get("liquidationPx") or 0),
            "leverage": float(p.get("leverage", {}).get("value", 10)),
        })
    return positions


def get_mark_prices(info):
    mids = with_retry(lambda: info.all_mids(), label="all_mids")
    if mids is None:
        return {}
    return {k: float(v) for k, v in mids.items()}


def get_open_orders(info, coin):
    orders = with_retry(lambda: info.open_orders(WALLET_ADDRESS), label="open_orders")
    if orders is None:
        return []
    return [o for o in orders if o.get("coin") == coin]


def fetch_candles(info, coin, interval_str="5m", n=120):
    """Fetch recent candles from Hyperliquid."""
    end_ms   = int(time.time() * 1000)
    mins     = 5 if interval_str == "5m" else 60
    start_ms = end_ms - (n * mins * 60 * 1000)
    try:
        data = info.candles_snapshot(coin, interval_str, start_ms, end_ms)
        if not isinstance(data, list) or len(data) < 20:
            return None
        candles = []
        for c in data:
            candles.append({
                "t": c["t"],
                "o": float(c["o"]),
                "h": float(c["h"]),
                "l": float(c["l"]),
                "c": float(c["c"]),
                "v": float(c.get("v", 0)),
            })
        return candles
    except Exception:
        return None


def calc_ema(values, period):
    if len(values) < period:
        return None
    k = 2.0 / (period + 1)
    ema = sum(values[:period]) / period
    for v in values[period:]:
        ema = v * k + ema * (1 - k)
    return ema


def calc_rsi(closes, period=14):
    if len(closes) < period + 1:
        return None
    gains, losses = [], []
    for i in range(1, len(closes)):
        diff = closes[i] - closes[i - 1]
        gains.append(max(diff, 0))
        losses.append(max(-diff, 0))
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def calc_bb(closes, period=20, std_dev=2):
    if len(closes) < period:
        return None, None, None
    window = closes[-period:]
    mid = sum(window) / period
    variance = sum((x - mid) ** 2 for x in window) / period
    std = variance ** 0.5
    return mid + std_dev * std, mid, mid - std_dev * std


def calc_adx(candles, period=14):
    if len(candles) < period * 2:
        return None, None, None
    tr_list, plus_dm, minus_dm = [], [], []
    for i in range(1, len(candles)):
        h, l, pc = candles[i]["h"], candles[i]["l"], candles[i - 1]["c"]
        tr = max(h - l, abs(h - pc), abs(l - pc))
        tr_list.append(tr)
        up = candles[i]["h"] - candles[i - 1]["h"]
        down = candles[i - 1]["l"] - candles[i]["l"]
        plus_dm.append(up if up > down and up > 0 else 0)
        minus_dm.append(down if down > up and down > 0 else 0)

    def smooth(lst, p):
        s = sum(lst[:p])
        result = [s]
        for v in lst[p:]:
            s = s - s / p + v
            result.append(s)
        return result

    atr_s = smooth(tr_list, period)
    pdi_s = smooth(plus_dm, period)
    mdi_s = smooth(minus_dm, period)
    dx_list = []
    for i in range(len(atr_s)):
        if atr_s[i] == 0:
            continue
        pdi = 100 * pdi_s[i] / atr_s[i]
        mdi = 100 * mdi_s[i] / atr_s[i]
        dx = 100 * abs(pdi - mdi) / (pdi + mdi) if (pdi + mdi) > 0 else 0
        dx_list.append((pdi, mdi, dx))
    if len(dx_list) < period:
        return None, None, None
    adx = sum(d[2] for d in dx_list[-period:]) / period
    pdi = dx_list[-1][0]
    mdi = dx_list[-1][1]
    return adx, pdi, mdi


def get_market_context(info, coin, fallback_entry):
    """
    Build a lightweight context for dynamic exits from live 5m candles.
    """
    candles = fetch_candles(info, coin, "5m", 120)
    if not candles:
        return {
            "price": fallback_entry,
            "ema20": fallback_entry,
            "ema50": fallback_entry,
            "rsi": 50.0,
            "adx": 25.0,
            "plus_di": 18.0,
            "minus_di": 18.0,
            "bb_upper": fallback_entry * 1.01,
            "bb_lower": fallback_entry * 0.99,
            "bb_width_pct": 2.0,
            "body_pct": 0.5,
            "atr_pct": 0.9,
            "vol_trend": "flat",
            "sentiment_score": 0.0,
            "sentiment_label": "Neutral",
            "sentiment_sources": [],
        }

    closes = [c["c"] for c in candles]
    vols = [c["v"] for c in candles]
    last = candles[-1]
    price = last["c"]
    ema20 = calc_ema(closes, 20) or price
    ema50 = calc_ema(closes, 50) or price
    rsi = calc_rsi(closes, 14) or 50.0
    adx, plus_di, minus_di = calc_adx(candles, 14)
    adx = adx or 25.0
    plus_di = plus_di or 18.0
    minus_di = minus_di or 18.0
    bb_upper, bb_mid, bb_lower = calc_bb(closes, 20, 2)
    bb_upper = bb_upper or price * 1.01
    bb_lower = bb_lower or price * 0.99
    bb_width_pct = abs(bb_upper - bb_lower) / price * 100 if price else 2.0

    body = abs(last["c"] - last["o"])
    rng = max(last["h"] - last["l"], 1e-9)
    body_pct = body / rng

    # ATR-like true range percent over recent bars
    trs = []
    for i in range(1, min(len(candles), 21)):
        h, l, pc = candles[-i]["h"], candles[-i]["l"], candles[-i - 1]["c"]
        tr = max(h - l, abs(h - pc), abs(l - pc))
        trs.append(tr)
    atr_pct = ((sum(trs) / len(trs)) / price * 100) if trs and price else 0.9

    avg_v_short = sum(vols[-5:]) / max(len(vols[-5:]), 1)
    avg_v_long = sum(vols[-20:]) / max(len(vols[-20:]), 1)
    vol_trend = "rising" if avg_v_short > avg_v_long * 1.05 else ("falling" if avg_v_short < avg_v_long * 0.95 else "flat")

    sentiment_score, sentiment_label, sentiment_sources = get_market_sentiment_details()

    return {
        "price": price,
        "ema20": ema20,
        "ema50": ema50,
        "rsi": rsi,
        "adx": adx,
        "plus_di": plus_di,
        "minus_di": minus_di,
        "bb_upper": bb_upper,
        "bb_lower": bb_lower,
        "bb_width_pct": bb_width_pct,
        "body_pct": body_pct,
        "atr_pct": atr_pct,
        "vol_trend": vol_trend,
        "sentiment_score": sentiment_score,
        "sentiment_label": sentiment_label,
        "sentiment_sources": sentiment_sources,
    }


def classify_setup_from_context(side, entry, ctx):
    """
    Best-effort setup classification for live positions.
    """
    price = ctx["price"]
    ema20 = ctx["ema20"]
    ema50 = ctx["ema50"]
    bb_upper = ctx["bb_upper"]
    bb_lower = ctx["bb_lower"]
    rsi = ctx["rsi"]
    bb_width_pct = ctx["bb_width_pct"]
    body_pct = ctx["body_pct"]

    near_ema20 = abs(entry - ema20) / max(entry, 1e-9) * 100 <= 0.7
    near_ema50 = abs(entry - ema50) / max(entry, 1e-9) * 100 <= 0.8

    if near_ema20 or near_ema50:
        return "EMA Bounce"

    if bb_width_pct < 1.4 and body_pct > 0.55:
        return "ORB"

    if side == "long":
        if entry <= bb_lower * 1.01 or rsi <= 40:
            return "Stop Hunt Reversal"
    else:
        if entry >= bb_upper * 0.99 or rsi >= 60:
            return "Stop Hunt Reversal"

    return "EMA Bounce"


def compute_trend_quality(side, ctx):
    adx = ctx["adx"]
    di_gap = abs(ctx["plus_di"] - ctx["minus_di"])
    bb_width_pct = ctx["bb_width_pct"]
    vol_trend = ctx["vol_trend"]
    sentiment_score = ctx.get("sentiment_score", 0.0)

    score = 0
    if adx >= 40:
        score += 3
    elif adx >= 30:
        score += 2
    elif adx >= 25:
        score += 1

    if di_gap >= 20:
        score += 3
    elif di_gap >= 12:
        score += 2
    elif di_gap >= 8:
        score += 1

    if vol_trend == "rising":
        score += 1

    if side == "long" and sentiment_score > 0.10:
        score += 1
    elif side == "short" and sentiment_score < -0.10:
        score += 1

    if bb_width_pct >= 2.5:
        vol_regime = "high"
    elif bb_width_pct >= 1.2:
        vol_regime = "normal"
    else:
        vol_regime = "low"

    if score >= 6:
        label = "strong"
    elif score >= 3:
        label = "normal"
    else:
        label = "weak"

    return {"label": label, "score": score, "vol_regime": vol_regime, "di_gap": di_gap}


def build_dynamic_plan(pos, info):
    entry = pos["entry_px"]
    side = pos["side"]
    leverage = max(float(pos.get("leverage", 5)), 1.0)
    size = max(float(pos.get("size", 0)), 0.0)
    notional = max(entry * size, 1e-9)

    ctx = get_market_context(info, pos["coin"], entry)
    strategy = classify_setup_from_context(side, entry, ctx)
    tq = compute_trend_quality(side, ctx)

    if strategy == "EMA Bounce":
        sl_pct = 1.15
    elif strategy == "ORB":
        sl_pct = 1.40
    else:
        sl_pct = 1.85

    if tq["vol_regime"] == "high":
        sl_pct += 0.35
    elif tq["vol_regime"] == "low":
        sl_pct -= 0.10

    if tq["label"] == "strong":
        sl_pct -= 0.10 if strategy != "Stop Hunt Reversal" else 0.05
    elif tq["label"] == "weak":
        sl_pct += 0.20

    if side == "short":
        sl_pct += 0.10

    # blend ATR reality into stop width
    atr_floor = max(0.95, ctx["atr_pct"] * 1.35)
    sl_pct = max(sl_pct, atr_floor)
    sl_pct = max(0.95, min(2.60, sl_pct))

    if tq["label"] == "strong":
        tp1_usd, tp2_usd = 5.0, 12.0
    elif tq["label"] == "normal":
        tp1_usd, tp2_usd = 3.5, 8.5
    else:
        tp1_usd, tp2_usd = 2.25, 5.5

    if strategy == "EMA Bounce":
        tp1_usd *= 0.95
        tp2_usd *= 1.00
    elif strategy == "ORB":
        tp1_usd *= 1.05
        tp2_usd *= 1.15
    else:
        tp1_usd *= 0.90
        tp2_usd *= 1.05

    if tq["vol_regime"] == "high":
        tp1_usd *= 1.10
        tp2_usd *= 1.15
    elif tq["vol_regime"] == "low":
        tp1_usd *= 0.90
        tp2_usd *= 0.95

    sl_frac = sl_pct / 100.0
    tp1_pct = max(tp1_usd / notional, sl_frac * (1.35 if strategy != "Stop Hunt Reversal" else 1.20))
    tp2_pct = max(tp2_usd / notional, sl_frac * (2.80 if tq["label"] == "strong" else 2.40))

    tp1_pct = min(tp1_pct, 0.10)
    tp2_pct = min(tp2_pct, 0.18)

    if side == "long":
        sl = round(entry * (1 - sl_frac), 6)
        tp1 = round(entry * (1 + tp1_pct), 6)
        tp2 = round(entry * (1 + tp2_pct), 6)
    else:
        sl = round(entry * (1 + sl_frac), 6)
        tp1 = round(entry * (1 - tp1_pct), 6)
        tp2 = round(entry * (1 - tp2_pct), 6)

    # wider trail for stronger trends and higher volatility
    trail_step_pct = max(0.40, min(1.20, sl_pct * (0.60 if tq["label"] == "strong" else 0.45)))
    trail_step = round(entry * trail_step_pct / 100.0, 6)

    return {
        "strategy": strategy,
        "trend_quality": tq["label"],
        "volatility_regime": tq["vol_regime"],
        "market_sentiment": ctx.get("sentiment_label", "Neutral"),
        "market_sentiment_score": round(ctx.get("sentiment_score", 0.0), 4),
        "market_sentiment_sources": ctx.get("sentiment_sources", []),
        "adx": round(ctx["adx"], 2),
        "di_gap": round(tq["di_gap"], 2),
        "sl": sl,
        "tp1": tp1,
        "tp2": tp2,
        "sl_pct": round(sl_pct, 2),
        "tp1_pct": round(tp1_pct * 100, 2),
        "tp2_pct": round(tp2_pct * 100, 2),
        "trail_step": trail_step,
        "trail_step_pct": round(trail_step_pct, 2),
    }


# ===============================================================
#  ORDER MANAGEMENT
# ===============================================================

def cancel_existing_orders(info, exchange, coin):
    """Cancel all open orders for this coin."""
    orders = get_open_orders(info, coin)
    if not orders:
        return
    oid_list   = []
    cloid_list = []
    for o in orders:
        cloid = o.get("cloid")
        oid   = o.get("oid")
        if cloid:
            cloid_list.append(cloid)
        elif oid:
            oid_list.append(int(oid))
    cancelled = 0
    for oid in oid_list:
        result = with_retry(lambda o=oid: exchange.cancel(coin, o), label=f"cancel_oid ({coin})")
        if result and result.get("status") == "ok":
            cancelled += 1
    for cloid in cloid_list:
        result = with_retry(lambda c=cloid: exchange.cancel_by_cloid(coin, c), label=f"cancel_cloid ({coin})")
        if result and result.get("status") == "ok":
            cancelled += 1
    if cancelled:
        print(f"  [{coin}] Cancelled {cancelled} existing order(s)")


def format_price(price):
    """Format price to Hyperliquid's required precision."""
    if price >= 10000: return round(price, 0)
    elif price >= 1000: return round(price, 1)
    elif price >= 100:  return round(price, 2)
    elif price >= 10:   return round(price, 3)
    elif price >= 1:    return round(price, 4)
    else:               return round(price, 5)


def _place_trigger_order(exchange, coin, is_buy, size, trigger_px, tpsl_type):
    """Place a stop-market or take-profit-market trigger order."""
    trigger_px = format_price(trigger_px)
    limit_px   = format_price(trigger_px * 0.95) if not is_buy else format_price(trigger_px * 1.05)
    order_type = {"trigger": {"triggerPx": trigger_px, "isMarket": True, "tpsl": tpsl_type}}
    return with_retry(
        lambda: exchange.order(coin, is_buy, size, limit_px, order_type, reduce_only=True),
        label=f"place_{tpsl_type} ({coin})"
    )


def _check_order_result(result, coin, label, price):
    """Check inner order statuses and log result. Returns True if order rested."""
    if result is None:
        return False
    if result.get("status") == "ok":
        statuses = result.get("response", {}).get("data", {}).get("statuses", [])
        for s in statuses:
            if "resting" in s:
                print(f"  [OK]    {coin} {label} resting at ${price} | oid: {s['resting']['oid']}")
                return True
            elif "filled" in s:
                print(f"  [OK]    {coin} {label} filled immediately at ${price}")
                return True
            elif "error" in s:
                print(f"  [WARN]  {coin} {label} inner error: {s['error']}")
                return False
        return True
    print(f"  [WARN]  {coin} {label} response: {result}")
    return False


def place_stop_loss(info, exchange, coin, side, sl_price, size, liq_px):
    """Cancel existing orders and place a new SL."""
    sl_price = format_price(sl_price)
    if liq_px and liq_px > 0:
        if side == "long" and sl_price <= liq_px:
            sl_price = format_price(liq_px * 1.005)
            print(f"  [{coin}] SL adjusted above liquidation to ${sl_price}")
        elif side == "short" and sl_price >= liq_px:
            sl_price = format_price(liq_px * 0.995)
            print(f"  [{coin}] SL adjusted below liquidation to ${sl_price}")
    cancel_existing_orders(info, exchange, coin)
    is_buy = (side == "short")
    result = _place_trigger_order(exchange, coin, is_buy, size, sl_price, "sl")
    ok = _check_order_result(result, coin, "SL", sl_price)
    if ok:
        tg("SL set: " + coin + " @ $" + str(sl_price))
    return ok


def place_take_profit(info, exchange, coin, side, tp_price, size, liq_px):
    """Place a TP order without cancelling the SL."""
    tp_price = format_price(tp_price)
    is_buy   = (side == "short")
    result   = _place_trigger_order(exchange, coin, is_buy, size, tp_price, "tp")
    ok = _check_order_result(result, coin, "TP", tp_price)
    if ok:
        tg("TP set: " + coin + " @ $" + str(tp_price))
    return ok


# ===============================================================
#  STATE MANAGEMENT
# ===============================================================

def calc_price(entry, pct_val, side, direction):
    if side == "long":
        m = (1 + pct_val / 100) if direction == "profit" else (1 - pct_val / 100)
    else:
        m = (1 - pct_val / 100) if direction == "profit" else (1 + pct_val / 100)
    return round(entry * m, 6)


def init_state(pos, info):
    entry = pos["entry_px"]
    side  = pos["side"]
    if DYNAMIC_EXITS:
        plan = build_dynamic_plan(pos, info)
    else:
        plan = {
            "strategy": "Static",
            "trend_quality": "normal",
            "volatility_regime": "normal",
            "adx": 25.0,
            "di_gap": 0.0,
            "sl": calc_price(entry, DEFAULTS["sl_pct"], side, "loss"),
            "tp1": calc_price(entry, DEFAULTS["tp1_pct"], side, "profit"),
            "tp2": calc_price(entry, DEFAULTS["tp2_pct"], side, "profit"),
            "sl_pct": DEFAULTS["sl_pct"],
            "tp1_pct": DEFAULTS["tp1_pct"],
            "tp2_pct": DEFAULTS["tp2_pct"],
            "trail_step": round(entry * DEFAULTS["trail_step_pct"] / 100, 6),
            "trail_step_pct": DEFAULTS["trail_step_pct"],
        }
    return {
        "coin":        pos["coin"],
        "side":        side,
        "size":        pos["size"],
        "entry":       entry,
        "liq_px":      pos["liq_px"],
        "leverage":    pos["leverage"],
        "strategy":    plan["strategy"],
        "trend_quality": plan["trend_quality"],
        "volatility_regime": plan["volatility_regime"],
        "market_sentiment": plan.get("market_sentiment", "Neutral"),
        "market_sentiment_score": plan.get("market_sentiment_score", 0.0),
        "market_sentiment_sources": plan.get("market_sentiment_sources", []),
        "adx":         plan["adx"],
        "di_gap":      plan["di_gap"],
        "sl":          plan["sl"],
        "tp1":         plan["tp1"],
        "tp2":         plan["tp2"],
        "sl_pct":      plan["sl_pct"],
        "tp1_pct":     plan["tp1_pct"],
        "tp2_pct":     plan["tp2_pct"],
        "trail_step":  plan["trail_step"],
        "trail_step_pct": plan["trail_step_pct"],
        "breakeven":   entry,
        "tp1_hit":     False,
        "trail_high":  None,
        "sl_placed":   False,
        "tp_placed":   False,
        "tp2_alerted": False,
        "paused":      False,
        "open_time":   datetime.now(timezone.utc),
    }


# ===============================================================
#  POSITION MANAGEMENT
# ===============================================================

def manage(info, exchange, state, price, live_size, liq_px):
    coin = state["coin"]
    side = state["side"]
    state["size"]   = live_size
    state["liq_px"] = liq_px

    if state.get("paused"):
        return

    if not state["sl_placed"]:
        print(f"  [{coin}] Placing initial SL at ${state['sl']:.4f} | TP at ${state['tp1']:.4f}")
        if place_stop_loss(info, exchange, coin, side, state["sl"], live_size, liq_px):
            state["sl_placed"] = True
        if not state["tp_placed"]:
            if place_take_profit(info, exchange, coin, side, state["tp1"], live_size, liq_px):
                state["tp_placed"] = True
        tg(
            "New " + side.upper() + " detected: " + coin + "\n"
            "Entry: $" + f"{state['entry']:.4f}" + "\n"
            "Strategy: " + state.get("strategy","?") + " | Trend: " + state.get("trend_quality","?") + "\n""SL: $" + f"{state['sl']:.4f}" + " (" + f"{state.get('sl_pct',0):.2f}" + "%) | TP1: $" + f"{state['tp1']:.4f}" + " (" + f"{state.get('tp1_pct',0):.2f}" + "%) | TP2: $" + f"{state['tp2']:.4f}" + " (" + f"{state.get('tp2_pct',0):.2f}" + "%)"
        )
        return

    if side == "long":
        if not state["tp1_hit"]:
            if price >= state["tp1"]:
                print(f"  [{coin}] TP1 hit at ${price:.4f} — moving SL to breakeven ${state['breakeven']:.4f}")
                if place_stop_loss(info, exchange, coin, side, state["breakeven"], live_size, liq_px):
                    state["sl"]         = state["breakeven"]
                    state["tp1_hit"]    = True
                    state["trail_high"] = price
                    tg("TP1 hit: " + coin + " @ $" + f"{price:.4f}" + " — SL moved to breakeven $" + f"{state['breakeven']:.4f}")
        else:
            if state["trail_high"] is None or price > state["trail_high"]:
                state["trail_high"] = price
            trail_sl = format_price(state["trail_high"] - state["trail_step"])
            if trail_sl > state["sl"]:
                print(f"  [{coin}] Trailing SL to ${trail_sl} (high: ${state['trail_high']:.4f})")
                if place_stop_loss(info, exchange, coin, side, trail_sl, live_size, liq_px):
                    state["sl"] = trail_sl
                    tg("Trailing SL moved: " + coin + " SL now $" + str(trail_sl))
            if price >= state["tp2"] and not state["tp2_alerted"]:
                print(f"  [{coin}] TP2 reached at ${price:.4f}!")
                tg("TP2 reached: " + coin + " @ $" + f"{price:.4f}" + " — consider closing!")
                state["tp2_alerted"] = True

    elif side == "short":
        if not state["tp1_hit"]:
            if price <= state["tp1"]:
                print(f"  [{coin}] TP1 hit at ${price:.4f} — moving SL to breakeven ${state['breakeven']:.4f}")
                if place_stop_loss(info, exchange, coin, side, state["breakeven"], live_size, liq_px):
                    state["sl"]         = state["breakeven"]
                    state["tp1_hit"]    = True
                    state["trail_high"] = price
                    tg("TP1 hit: " + coin + " @ $" + f"{price:.4f}" + " — SL moved to breakeven $" + f"{state['breakeven']:.4f}")
        else:
            if state["trail_high"] is None or price < state["trail_high"]:
                state["trail_high"] = price
            trail_sl = format_price(state["trail_high"] + state["trail_step"])
            if trail_sl < state["sl"]:
                print(f"  [{coin}] Trailing SL to ${trail_sl} (low: ${state['trail_high']:.4f})")
                if place_stop_loss(info, exchange, coin, side, trail_sl, live_size, liq_px):
                    state["sl"] = trail_sl
                    tg("Trailing SL moved: " + coin + " SL now $" + str(trail_sl))
            if price <= state["tp2"] and not state["tp2_alerted"]:
                print(f"  [{coin}] TP2 reached at ${price:.4f}!")
                tg("TP2 reached: " + coin + " @ $" + f"{price:.4f}" + " — consider closing!")
                state["tp2_alerted"] = True


def print_status(state_map, prices):
    print()
    print(f"  {'COIN':<8} {'SIDE':<6} {'STRAT':<18} {'PRICE':>10} {'ENTRY':>10} {'SL':>10} {'TP1':>10} {'TP1?':<6} {'TRAIL':>10}")
    print(f"  {'-'*8} {'-'*6} {'-'*18} {'-'*10} {'-'*10} {'-'*10} {'-'*10} {'-'*6} {'-'*10}")
    for coin, s in state_map.items():
        price = prices.get(coin, 0)
        th    = "$" + f"{s['trail_high']:.4f}" if s["trail_high"] else "---"
        hit   = "Yes" if s["tp1_hit"] else "No"
        strat = s.get("strategy", "?")[:18]
        print(f"  {coin:<8} {s['side']:<6} {strat:<18} ${price:>9.4f} ${s['entry']:>9.4f} ${s['sl']:>9.4f} ${s['tp1']:>9.4f} {hit:<6} {th:>10}")
    print()


# ===============================================================
#  TELEGRAM COMMANDS
# ===============================================================

def handle_commands(messages, state_map, info, exchange, prices):
    for msg in messages:
        text = msg.get("text", "").strip()
        if not text.startswith("/"):
            continue
        parts = text.split()
        cmd   = parts[0].lower()
        coin  = parts[1].upper() if len(parts) > 1 else None
        val   = None
        if len(parts) > 2:
            try:
                val = float(parts[2])
            except ValueError:
                pass

        if cmd == "/status":
            if not state_map:
                tg("No open positions being tracked.")
                continue
            reply = "Position Status\n\n"
            for c, s in state_map.items():
                price   = prices.get(c, 0)
                entry   = s["entry"]
                side    = s["side"]
                lev     = s.get("leverage", 10)
                pnl_pct = ((price - entry) / entry * 100) if side == "long" else ((entry - price) / entry * 100)
                pnl_pct *= lev
                sign    = "+" if pnl_pct >= 0 else ""
                tp1_tag = "(HIT)" if s["tp1_hit"] else "(pending)"
                trail   = "$" + f"{s['trail_high']:.4f}" if s["trail_high"] else "---"
                open_t  = s.get("open_time")
                if open_t:
                    cdt_t    = open_t.astimezone(CDT)
                    time_str = cdt_t.strftime("%I:%M %p CDT")
                    elapsed  = datetime.now(timezone.utc) - open_t
                    mins     = int(elapsed.total_seconds() // 60)
                    hrs      = mins // 60
                    duration = (str(hrs) + "h " + str(mins % 60) + "m") if hrs > 0 else (str(mins) + "m")
                else:
                    time_str = "unknown"
                    duration = "unknown"
                reply += (
                    c + " " + side.upper() + "\n"
                    + "  Strategy: " + s.get("strategy", "?") + " | Trend: " + s.get("trend_quality", "?") + "\n"
                    + "  Price:    $" + f"{price:.4f}" + "\n"
                    + "  Entry:    $" + f"{entry:.4f}" + "\n"
                    + "  P&L:      " + sign + f"{pnl_pct:.2f}" + "%\n"
                    + "  SL:       $" + f"{s['sl']:.4f}" + " (" + f"{s.get('sl_pct',0):.2f}" + "%)\n"
                    + "  TP1:      $" + f"{s['tp1']:.4f}" + " (" + f"{s.get('tp1_pct',0):.2f}" + "%) " + tp1_tag + "\n"
                    + "  TP2:      $" + f"{s['tp2']:.4f}" + " (" + f"{s.get('tp2_pct',0):.2f}" + "%)\n"
                    + "  Trail:    " + trail + " | step $" + f"{s.get('trail_step',0):.4f}" + "\n"
                    + "  Opened:   " + time_str + "\n"
                    + "  Duration: " + duration + "\n\n"
                )
            tg(reply.strip())

        elif cmd == "/price":
            if coin:
                price = prices.get(coin)
                tg(coin + ": $" + f"{price:.4f}" if price else "No price for " + coin)
            elif state_map:
                reply = "Live Prices\n"
                for c in state_map:
                    p = prices.get(c, 0)
                    reply += "  " + c + ": $" + f"{p:.4f}" + "\n"
                tg(reply.strip())
            else:
                tg("No positions tracked.")

        elif cmd == "/sl" and coin and val is not None:
            if coin not in state_map:
                tg("No tracked position for " + coin)
                continue
            s      = state_map[coin]
            price  = prices.get(coin, s["entry"])
            new_sl = format_price(price * (1 - val / 100) if s["side"] == "long" else price * (1 + val / 100))
            if place_stop_loss(info, exchange, coin, s["side"], new_sl, s["size"], s["liq_px"]):
                s["sl"] = new_sl
                tg(coin + " SL updated to $" + str(new_sl) + " (" + str(val) + "% from $" + f"{price:.4f}" + ")")
            else:
                tg("Failed to update " + coin + " SL.")

        elif cmd == "/tp1" and coin and val is not None:
            if coin not in state_map:
                tg("No tracked position for " + coin)
                continue
            s      = state_map[coin]
            price  = prices.get(coin, s["entry"])
            new_tp = format_price(price * (1 + val / 100) if s["side"] == "long" else price * (1 - val / 100))
            s["tp1"] = new_tp
            tg(coin + " TP1 updated to $" + str(new_tp) + " (" + str(val) + "% from $" + f"{price:.4f}" + ")")

        elif cmd == "/close" and coin:
            if coin not in state_map:
                tg("No tracked position for " + coin)
                continue
            tg("Closing " + coin + " at market...")
            result = with_retry(lambda: exchange.market_close(coin), label="market_close")
            if result and result.get("status") == "ok":
                tg(coin + " closed at market.")
            else:
                tg("Failed to close " + coin + ": " + str(result))

        elif cmd == "/pause" and coin:
            if coin not in state_map:
                tg("No tracked position for " + coin)
                continue
            state_map[coin]["paused"] = True
            tg(coin + " paused. Send /resume " + coin + " to restart.")

        elif cmd == "/resume" and coin:
            if coin not in state_map:
                tg("No tracked position for " + coin)
                continue
            state_map[coin]["paused"] = False
            tg(coin + " resumed.")

        elif cmd == "/defaults":
            tg(
                ("Dynamic exit engine: ON\n"
                 + "  Setup aware\n"
                 + "  Volatility aware\n"
                 + "  Trend quality aware\n\n") if DYNAMIC_EXITS else
                ("Current Defaults\n"
                 + "  SL:    " + str(DEFAULTS["sl_pct"]) + "%\n"
                 + "  TP1:   " + str(DEFAULTS["tp1_pct"]) + "%\n"
                 + "  TP2:   " + str(DEFAULTS["tp2_pct"]) + "%\n"
                 + "  Trail: " + str(DEFAULTS["trail_step_pct"]) + "%\n\n")
                + "Commands:\n"
                + "  /status\n"
                + "  /price [COIN]\n"
                + "  /sl COIN %\n"
                + "  /tp1 COIN %\n"
                + "  /pause COIN\n"
                + "  /resume COIN\n"
                + "  /close COIN\n"
                + "  /defaults"
            )

        else:
            tg("Unknown command: " + text + "\nSend /defaults for help.")


# ===============================================================
#  POSITION CACHE (for dashboard leverage display)
# ===============================================================

POSITION_CACHE_FILE = "position_cache.json"
HEARTBEAT_FILE = "trailing_bot_status.json"

def write_position_cache(coin, pos):
    """
    Save position details to position_cache.json so the dashboard
    can display accurate leverage on sync, even after the position closes.
    """
    try:
        try:
            with open(POSITION_CACHE_FILE, "r") as f:
                cache = json.load(f)
        except FileNotFoundError:
            cache = {}

        cache[coin] = {
            "leverage":   int(pos.get("leverage", 5)),
            "entry":      pos.get("entry_px", 0),
            "side":       pos.get("side", "long"),
            "size":       pos.get("size", 0),
            "opened_at":  datetime.now(timezone.utc).isoformat(),
        }

        with open(POSITION_CACHE_FILE, "w") as f:
            json.dump(cache, f, indent=2)

        print(f"  [{coin}] Position cached (lev: {int(pos.get('leverage', 5))}x)")
    except Exception as e:
        print(f"  [WARN] Could not write position cache: {e}")


def write_heartbeat(state_map, cycle):
    """
    Let the scanner confirm the trailing bot is alive before it opens
    a new live position.
    """
    try:
        payload = {
            "updated_at": time.time(),
            "cycle": cycle,
            "wallet": WALLET_ADDRESS,
            "watching": sorted(list(state_map.keys())),
            "position_count": len(state_map),
            "bot": "trailing_stop_bot_dynamic",
        }
        with open(HEARTBEAT_FILE, "w") as f:
            json.dump(payload, f, indent=2)
    except Exception as e:
        print(f"  [WARN] Could not write heartbeat: {e}")

# ===============================================================
#  MAIN LOOP
# ===============================================================

def main():
    print()
    print("  Hyperliquid Trailing Stop Bot")
    print("  ================================")

    if not WALLET_ADDRESS or not API_PRIVATE_KEY:
        print()
        print("  ERROR: Missing credentials in .env file.")
        print("  Required: HL_WALLET, HL_BOT_KEY")
        return

    print(f"  Wallet   : {WALLET_ADDRESS[:10]}...{WALLET_ADDRESS[-6:]}")
    print(f"  Interval : {CHECK_INTERVAL}s | Retries: {MAX_RETRIES}")
    print("  Exits    : dynamic" if DYNAMIC_EXITS else f"  SL: {DEFAULTS['sl_pct']}% | TP1: {DEFAULTS['tp1_pct']}% | TP2: {DEFAULTS['tp2_pct']}% | Trail: {DEFAULTS['trail_step_pct']}%")
    print(f"  Telegram : {'connected' if TG_TOKEN and TG_CHAT_ID else 'not configured'}")
    print()
    print("  Initializing SDK...")

    try:
        info, exchange = setup()
        print("  SDK ready.")
    except Exception as e:
        print(f"  ERROR: Could not initialize SDK: {e}")
        print("  pip install hyperliquid-python-sdk")
        return

    print()
    print("  Press Ctrl+C to stop.")
    print()

    tg("Trailing Stop Bot started.")

    state_map = {}
    cycle     = 0
    write_heartbeat(state_map, cycle)

    while True:
        cycle += 1
        write_heartbeat(state_map, cycle)
        ts = time.strftime("%H:%M:%S")
        print(f"-- Cycle {cycle} [{ts}] " + "-" * 42)

        open_positions = get_open_positions(info)
        open_coins     = {p["coin"] for p in open_positions}
        live_data      = {p["coin"]: p for p in open_positions}

        if not open_positions:
            print("  No open positions. Watching for trades...")
        else:
            print(f"  Open: {', '.join(open_coins)}")

        for coin in list(state_map.keys()):
            if coin not in open_coins:
                print(f"  [{coin}] Closed — removing from watchlist")
                tg(coin + " position closed. Check Trade History for P&L.")
                del state_map[coin]

        for pos in open_positions:
            coin = pos["coin"]
            if coin not in state_map:
                s = init_state(pos, info)
                state_map[coin] = s
                liq_str = "$" + f"{pos['liq_px']:.4f}" if pos["liq_px"] else "N/A"
                print(f"  [{coin}] NEW {pos['side'].upper()} detected")
                print(f"         Entry: ${pos['entry_px']:.4f} | Size: {pos['size']} | Lev: {pos['leverage']:.0f}x | Liq: {liq_str}")
                print(f"         {s['strategy']} | trend: {s['trend_quality']} | vol: {s['volatility_regime']} | ADX: {s['adx']:.1f}")
                print(f"         Auto SL: ${s['sl']:.4f} ({s['sl_pct']:.2f}%) | TP1: ${s['tp1']:.4f} ({s['tp1_pct']:.2f}%) | TP2: ${s['tp2']:.4f} ({s['tp2_pct']:.2f}%) | Trail: ${s['trail_step']:.4f}")
                # Save leverage to position cache for dashboard sync
                write_position_cache(coin, pos)

        prices = get_mark_prices(info)

        try:
            messages = tg_get_updates()
            if messages:
                handle_commands(messages, state_map, info, exchange, prices)
        except Exception as e:
            print(f"  [WARN] Telegram command error: {e}")

        for coin, state in state_map.items():
            price     = prices.get(coin)
            pos_data  = live_data.get(coin, {})
            if price is None:
                print(f"  [{coin}] Price unavailable — skipping")
                continue
            live_size = pos_data.get("size", state["size"])
            liq_px    = pos_data.get("liq_px", state["liq_px"])
            paused    = " (PAUSED)" if state.get("paused") else ""
            print(f"  [{coin}] ${price:.4f} | {state.get('strategy','?')} | SL: ${state['sl']:.4f} | TP1 hit: {state['tp1_hit']}{paused}")
            try:
                manage(info, exchange, state, price, live_size, liq_px)
            except Exception as e:
                print(f"  [ERROR] {coin}: {e}")

        if state_map and cycle % 5 == 0:
            print_status(state_map, prices)

        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n\n  Bot stopped.")
        tg("Trailing Stop Bot stopped.")
