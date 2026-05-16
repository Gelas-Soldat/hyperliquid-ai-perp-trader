#!/usr/bin/env python3
"""
Hyperliquid Scanner Bot — Paper Trading Mode
=============================================
Scans all Hyperliquid perp pairs every 5 minutes, scores setups
against a multi-factor system, and logs phantom trades to track
performance before going live.

HOW TO RUN:
  1. pip install hyperliquid-python-sdk python-dotenv requests
  2. Make sure your .env file has all credentials
  3. python scanner_bot.py
  4. After 7 days of paper trading, review results and decide to go live

MODE:
  PAPER (default) — finds and tracks phantom trades, no real orders
  LIVE  — places real orders on Hyperliquid (change MODE below)

.env ADDITIONS NEEDED:
  SCAN_INTERVAL=300     (seconds between scans, default 5 min)
  MAX_POSITIONS=3       (max simultaneous paper/live positions)
  DAILY_LOSS_CAP=10     (stop trading if down this much in a day)
  ACCOUNT_FLOOR=30      (stop if account drops below this)
"""

import os
import json
import time
import requests
import re
import eth_account
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv
from concurrent.futures import ThreadPoolExecutor, as_completed
# Native indicator calculation from Hyperliquid candle data
# No TradingView dependency needed
from hyperliquid.info import Info
from hyperliquid.exchange import Exchange as HLExchange
from hyperliquid.utils import constants

# ===============================================================
#  COLORS (ANSI — Windows Terminal / CMD with VT enabled)
# ===============================================================

G   = "[92m"   # green
Y   = "[93m"   # yellow
R   = "[91m"   # red
B   = "[94m"   # blue
C   = "[96m"   # cyan
W   = "[97m"   # white
DIM = "[2m"    # dim
X   = "[0m"    # reset
BOLD= "[1m"    # bold


# ===============================================================
#  CONFIG
# ===============================================================

load_dotenv()

WALLET_ADDRESS  = os.getenv("HL_WALLET",      "").strip()
API_PRIVATE_KEY = os.getenv("HL_BOT_KEY",     "").strip()
TG_TOKEN        = os.getenv("TG_TOKEN",       "").strip()
TG_CHAT_ID      = os.getenv("TG_CHAT_ID",     "").strip()
SCAN_INTERVAL   = int(os.getenv("SCAN_INTERVAL",  "300"))
MAX_POSITIONS   = int(os.getenv("MAX_POSITIONS",  "3"))
DAILY_LOSS_CAP  = float(os.getenv("DAILY_LOSS_CAP", "10.0"))
ACCOUNT_FLOOR   = float(os.getenv("ACCOUNT_FLOOR",  "30.0"))

MODE            = "paper"    # "paper" or "live" — change to "live" when ready
MIN_SCORE       = 7          # minimum score to consider a trade (7 for live mode = safer)

# Live rollout safety rails
LIVE_SAFE_MODE              = os.getenv("LIVE_SAFE_MODE", "1").strip().lower() not in ("0", "false", "no", "off")
LIVE_MAX_POSITIONS          = int(os.getenv("LIVE_MAX_POSITIONS", "1"))
LIVE_DAILY_LOSS_CAP         = float(os.getenv("LIVE_DAILY_LOSS_CAP", "5.0"))
LIVE_ACCOUNT_FLOOR          = float(os.getenv("LIVE_ACCOUNT_FLOOR", "35.0"))
LIVE_SIZE_MULTIPLIER        = float(os.getenv("LIVE_SIZE_MULTIPLIER", "0.5"))
TRAILING_HEARTBEAT_FILE     = os.getenv("TRAILING_HEARTBEAT_FILE", "trailing_bot_status.json").strip() or "trailing_bot_status.json"
TRAILING_HEARTBEAT_MAX_AGE  = int(os.getenv("TRAILING_HEARTBEAT_MAX_AGE", "90"))


COOLDOWN_MINS   = 15         # minutes before re-entering a recently closed coin
NEWS_VETO_WORDS = [          # words that trigger a hard news veto
    "hack", "exploit", "breach", "lawsuit", "sec", "fraud",
    "ban", "exit scam", "rug", "shutdown", "insolvent", "bankrupt",
    "arrested", "seized", "delisted", "attack", "vulnerability"
]
NEWS_POSITIVE_WORDS = [
    "partnership", "listing", "upgrade", "mainnet", "launch",
    "integration", "adoption", "institutional", "etf", "approval"
]

# Central Time — UTC-5 during CDT (Mar-Nov), UTC-6 during CST (Nov-Mar)
try:
    from zoneinfo import ZoneInfo
    import zoneinfo
    CDT = ZoneInfo("America/Chicago")
except Exception:
    # Windows often lacks tzdata — install with: pip install tzdata
    # Fallback: detect DST manually based on month
    _month = __import__('datetime').datetime.now(__import__('datetime').timezone.utc).month
    # DST active March through November
    _offset = -5 if 3 <= _month <= 11 else -6
    CDT = timezone(timedelta(hours=_offset))
STATE_FILE      = "scanner_state.json"
TRADES_FILE     = "paper_trades.json"

# Scoring tiers
TIERS = {
    8:  {"label": "Standard",   "size": 11, "leverage": 3,  "risk_pct": 1.5},
    9:  {"label": "High",       "size": 15, "leverage": 7,  "risk_pct": 2.0},
    10: {"label": "Very High",  "size": 20, "leverage": 10, "risk_pct": 3.0},
    11: {"label": "Max",        "size": 25, "leverage": 10, "risk_pct": 3.0},
}

# ===============================================================
#  LIVE SAFETY HELPERS
# ===============================================================

def effective_max_positions():
    if MODE == "live" and LIVE_SAFE_MODE:
        return min(MAX_POSITIONS, LIVE_MAX_POSITIONS)
    return MAX_POSITIONS

def effective_daily_loss_cap():
    if MODE == "live" and LIVE_SAFE_MODE:
        return min(DAILY_LOSS_CAP, LIVE_DAILY_LOSS_CAP)
    return DAILY_LOSS_CAP

def effective_account_floor():
    if MODE == "live" and LIVE_SAFE_MODE:
        return max(ACCOUNT_FLOOR, LIVE_ACCOUNT_FLOOR)
    return ACCOUNT_FLOOR

def trailing_bot_ready():
    """
    In live mode, require a fresh heartbeat from the trailing stop bot
    before allowing new entries.
    """
    if MODE != "live":
        return True, "paper mode"
    try:
        p = Path(TRAILING_HEARTBEAT_FILE)
        if not p.exists():
            return False, "heartbeat file missing"
        data = json.loads(p.read_text(encoding="utf-8"))
        updated_at = float(data.get("updated_at", 0) or 0)
        age = time.time() - updated_at
        if age > TRAILING_HEARTBEAT_MAX_AGE:
            return False, f"heartbeat stale ({int(age)}s old)"
        hb_wallet = (data.get("wallet") or "").lower()
        if WALLET_ADDRESS and hb_wallet and hb_wallet != WALLET_ADDRESS.lower():
            return False, "heartbeat wallet mismatch"
        return True, "ok"
    except Exception as e:
        return False, f"heartbeat unreadable: {e}"

# ===============================================================
#  STATE MANAGEMENT
# ===============================================================

def load_state():
    default = {
        "mode":             MODE,
        "daily_pnl":        0.0,
        "daily_trades":     0,
        "cap_hit":          False,
        "last_reset_date":  "",
        "cooldowns":        {},
        "paper_positions":  {},
        "started_at":       datetime.now(timezone.utc).isoformat(),
        "last_entry_time":  "",   # ISO timestamp of last entry — persists across restarts
        "balance_cache":    0.0,
    }
    try:
        with open(STATE_FILE, "r") as f:
            state = json.load(f)
            for k, v in default.items():
                if k not in state:
                    state[k] = v
            return state
    except FileNotFoundError:
        save_state(default)
        return default


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def load_trades():
    try:
        with open(TRADES_FILE, "r") as f:
            return json.load(f)
    except FileNotFoundError:
        return []


def save_trades(trades):
    with open(TRADES_FILE, "w") as f:
        json.dump(trades, f, indent=2, default=str)


def daily_reset_if_needed(state):
    today = datetime.now(CDT).strftime("%Y-%m-%d")
    if state.get("last_reset_date") != today:
        if state.get("last_reset_date"):
            # Send daily summary before reset
            summary = (
                "Daily Summary (" + state["last_reset_date"] + ")\n"
                + "  Trades:   " + str(state["daily_trades"]) + "\n"
                + "  P&L:      $" + f"{state['daily_pnl']:.2f}" + "\n"
                + "  Mode:     " + state["mode"].upper()
            )
            tg(summary)
        state["daily_pnl"]       = 0.0
        state["daily_trades"]    = 0
        state["cap_hit"]         = False
        state["last_reset_date"] = today
        # Clear expired cooldowns
        now = time.time()
        state["cooldowns"] = {
            k: v for k, v in state["cooldowns"].items()
            if v > now
        }
        save_state(state)
        print(f"  [RESET] New trading day: {today}")


# ===============================================================
#  TELEGRAM
# ===============================================================

def tg(message):
    if not TG_TOKEN or not TG_CHAT_ID:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            json={"chat_id": TG_CHAT_ID, "text": message},
            timeout=5
        )
    except Exception:
        pass


# ===============================================================
#  HYPERLIQUID DATA
# ===============================================================

def get_hl_meta(info):
    """Fetch all perp pairs and their metadata from Hyperliquid."""
    try:
        meta = info.meta()
        return meta.get("universe", [])
    except Exception as e:
        print(f"  [ERROR] get_hl_meta: {e}")
        return []


def get_hl_mark_prices(info):
    """Fetch current mark prices for all pairs."""
    try:
        mids = info.all_mids()
        return {k: float(v) for k, v in mids.items()}
    except Exception as e:
        print(f"  [ERROR] get_hl_mark_prices: {e}")
        return {}


def get_funding_rates(info):
    """Fetch current funding rates for all pairs."""
    try:
        res = requests.post(
            "https://api.hyperliquid.xyz/info",
            json={"type": "metaAndAssetCtxs"},
            timeout=10
        )
        res.raise_for_status()
        data = res.json()
        if isinstance(data, list) and len(data) > 1:
            ctxs   = data[1]
            meta   = data[0].get("universe", [])
            rates  = {}
            for i, ctx in enumerate(ctxs):
                if i < len(meta):
                    coin = meta[i]["name"]
                    rates[coin] = float(ctx.get("funding", 0))
            return rates
        return {}
    except Exception as e:
        print(f"  [ERROR] get_funding_rates: {e}")
        return {}


def get_account_balance(info):
    """
    Get current USDC balance from Hyperliquid.
    Tries multiple fields to handle unified/portfolio accounts.
    """
    try:
        state = info.user_state(WALLET_ADDRESS)

        # Try all known balance fields in order of reliability
        checks = [
            state.get("marginSummary", {}).get("accountValue"),
            state.get("crossMarginSummary", {}).get("accountValue"),
            state.get("withdrawable"),
        ]
        for val in checks:
            if val is not None:
                f = float(val)
                if f > 0:
                    return f

        # If all zero, try spot balance as fallback
        spot = info.spot_user_state(WALLET_ADDRESS)
        for bal in spot.get("balances", []):
            if bal.get("coin") == "USDC":
                return float(bal.get("total", 0))

        return 0.0
    except Exception as e:
        print(f"  [ERROR] get_account_balance: {e}")
        return 0.0


def get_open_hl_positions(info):
    """Get coins with currently open positions on Hyperliquid."""
    try:
        state = info.user_state(WALLET_ADDRESS)
        coins = set()
        for ap in state.get("assetPositions", []):
            p   = ap.get("position", {})
            szi = float(p.get("szi", 0))
            if szi != 0:
                coins.add(p.get("coin"))
        return coins
    except Exception as e:
        print(f"  [ERROR] get_open_hl_positions: {e}")
        return set()


# ===============================================================
#  NATIVE HYPERLIQUID INDICATOR ENGINE
# ===============================================================

# Global objects — set in main()
_hl_info    = None
_exchange   = None   # HLExchange instance for live order placement
_max_lev_map = {}    # coin → max leverage, updated each scan cycle


def fetch_candles(coin, interval_str, n=210):
    """
    Fetch OHLCV candles via the Hyperliquid SDK info object.
    Uses the same connection as the trailing stop bot.
    interval_str: "5m" or "1h"
    Returns list of dicts with o, h, l, c, v keys, oldest first.
    
    IMPROVED: Handles missing coins gracefully (KeyError for new/low-vol coins)
    """
    global _hl_info
    if _hl_info is None:
        return None
    
    end_ms   = int(time.time() * 1000)
    mins     = 5 if interval_str == "5m" else 60
    start_ms = end_ms - (n * mins * 60 * 1000)

    for attempt in range(3):
        try:
            data = _hl_info.candles_snapshot(coin, interval_str, start_ms, end_ms)
            if not isinstance(data, list) or len(data) < 10:
                return None
            candles = []
            for c in data:
                candles.append({
                    "t": c["t"],
                    "o": float(c["o"]),
                    "h": float(c["h"]),
                    "l": float(c["l"]),
                    "c": float(c["c"]),
                    "v": float(c["v"]),
                })
            return candles
        except KeyError:
            # Coin doesn't exist in candle API (new, low-volume, or delisted)
            return None
        except Exception as e:
            err = str(e)
            if "429" in err:
                time.sleep(0.5 + attempt * 0.5)   # back off on rate limit
                continue
            # Other errors (connection issues, etc.) — skip silently
            return None
    return None


def calc_ema(values, period):
    """Calculate EMA for a list of values."""
    if len(values) < period:
        return None
    k = 2.0 / (period + 1)
    ema = sum(values[:period]) / period
    for v in values[period:]:
        ema = v * k + ema * (1 - k)
    return ema


def calc_stoch_rsi(closes, rsi_period=14, stoch_period=14, smooth_k=3, smooth_d=3):
    """
    Stochastic RSI — faster momentum signal than standard RSI.
    Returns (k, d) where k/d are 0-100.
    K > 80 = overbought, K < 20 = oversold.
    """
    if len(closes) < rsi_period + stoch_period + smooth_k:
        return None, None
    try:
        # Calculate RSI values
        rsi_vals = []
        for i in range(rsi_period, len(closes)):
            gains = [max(closes[j] - closes[j-1], 0) for j in range(i-rsi_period+1, i+1)]
            losses = [abs(min(closes[j] - closes[j-1], 0)) for j in range(i-rsi_period+1, i+1)]
            avg_gain = sum(gains) / rsi_period
            avg_loss = sum(losses) / rsi_period
            if avg_loss == 0:
                rsi_vals.append(100.0)
            else:
                rs = avg_gain / avg_loss
                rsi_vals.append(100 - (100 / (1 + rs)))

        if len(rsi_vals) < stoch_period:
            return None, None

        # Stochastic of RSI
        stoch_k_raw = []
        for i in range(stoch_period - 1, len(rsi_vals)):
            window = rsi_vals[i - stoch_period + 1:i + 1]
            low_rsi  = min(window)
            high_rsi = max(window)
            if high_rsi == low_rsi:
                stoch_k_raw.append(50.0)
            else:
                stoch_k_raw.append((rsi_vals[i] - low_rsi) / (high_rsi - low_rsi) * 100)

        # Smooth K
        if len(stoch_k_raw) < smooth_k:
            return None, None
        k_smooth = sum(stoch_k_raw[-smooth_k:]) / smooth_k

        # D is SMA of K
        if len(stoch_k_raw) < smooth_k + smooth_d - 1:
            return None, None
        k_vals_for_d = []
        for i in range(smooth_d):
            window = stoch_k_raw[-(smooth_k + smooth_d - 1 - i):-(smooth_d - 1 - i) if (smooth_d - 1 - i) > 0 else len(stoch_k_raw)]
            k_vals_for_d.append(sum(window) / smooth_k)
        d_smooth = sum(k_vals_for_d) / smooth_d

        return round(k_smooth, 2), round(d_smooth, 2)
    except Exception:
        return None, None


def calc_rsi(closes, period=14):
    """Calculate RSI."""
    if len(closes) < period + 1:
        return None
    gains, losses = [], []
    for i in range(1, len(closes)):
        diff = closes[i] - closes[i-1]
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


def calc_macd(closes, fast=12, slow=26, signal=9):
    """Calculate MACD line, signal line, and histogram."""
    if len(closes) < slow + signal + 5:
        return None, None, None

    macd_series = []
    for i in range(slow, len(closes) + 1):
        window = closes[:i]
        ema_fast = calc_ema(window, fast)
        ema_slow = calc_ema(window, slow)
        if ema_fast is None or ema_slow is None:
            continue
        macd_series.append(ema_fast - ema_slow)

    if len(macd_series) < signal:
        return None, None, None

    signal_line = calc_ema(macd_series, signal)
    if signal_line is None:
        return None, None, None

    macd_line = macd_series[-1]
    hist = macd_line - signal_line
    return macd_line, signal_line, hist


def calc_bb(closes, period=20, std_dev=2):
    """Calculate Bollinger Bands."""
    if len(closes) < period:
        return None, None, None
    window = closes[-period:]
    mid    = sum(window) / period
    variance = sum((x - mid) ** 2 for x in window) / period
    std    = variance ** 0.5
    return mid + std_dev * std, mid, mid - std_dev * std


def calc_adx(candles, period=14):
    """Calculate ADX, +DI, -DI."""
    if len(candles) < period * 2:
        return None, None, None
    tr_list, plus_dm, minus_dm = [], [], []
    for i in range(1, len(candles)):
        h, l, pc = candles[i]["h"], candles[i]["l"], candles[i-1]["c"]
        tr = max(h - l, abs(h - pc), abs(l - pc))
        tr_list.append(tr)
        up   = candles[i]["h"] - candles[i-1]["h"]
        down = candles[i-1]["l"] - candles[i]["l"]
        plus_dm.append(up   if up > down and up > 0   else 0)
        minus_dm.append(down if down > up and down > 0 else 0)
    # Smoothed averages
    def smooth(lst, p):
        s = sum(lst[:p])
        result = [s]
        for v in lst[p:]:
            s = s - s / p + v
            result.append(s)
        return result
    atr_s  = smooth(tr_list,  period)
    pdi_s  = smooth(plus_dm,  period)
    mdi_s  = smooth(minus_dm, period)
    dx_list = []
    for i in range(len(atr_s)):
        if atr_s[i] == 0:
            continue
        pdi = 100 * pdi_s[i] / atr_s[i]
        mdi = 100 * mdi_s[i] / atr_s[i]
        dx  = 100 * abs(pdi - mdi) / (pdi + mdi) if (pdi + mdi) > 0 else 0
        dx_list.append((pdi, mdi, dx))
    if len(dx_list) < period:
        return None, None, None
    adx = sum(d[2] for d in dx_list[-period:]) / period
    pdi = dx_list[-1][0]
    mdi = dx_list[-1][1]
    return adx, pdi, mdi


def get_indicators(coin, interval_str, n=210):
    """
    Fetch candles from Hyperliquid and calculate all indicators natively.
    Works for all 229 HL perp pairs directly.
    Returns dict of indicators or None if unavailable.
    """
    candles = fetch_candles(coin, interval_str, n=n)
    if candles is None or len(candles) < 50:
        return None

    closes = [c["c"] for c in candles]
    vols   = [c["v"] for c in candles]
    close  = closes[-1]
    last   = candles[-1]

    rsi      = calc_rsi(closes, 14)
    stoch_k, stoch_d = calc_stoch_rsi(closes)
    macd, macd_sig, macd_hist = calc_macd(closes)
    bb_up, bb_mid, bb_low = calc_bb(closes, 20)
    adx, plus_di, minus_di = calc_adx(candles, 14)
    ema20  = calc_ema(closes, 20)
    ema50  = calc_ema(closes, 50)
    ema200 = calc_ema(closes, 200)

    if None in (rsi, ema20, ema50):
        return None

    avg_vol = sum(vols[-20:]) / min(len(vols), 20) if vols else 0
    vol_trend = "rising" if avg_vol and last["v"] > avg_vol * 1.15 else ("falling" if avg_vol and last["v"] < avg_vol * 0.85 else "flat")

    high = last.get("h", close)
    low  = last.get("l", close)
    op   = last.get("o", close)
    rng  = max(high - low, 1e-12)
    body = abs(close - op)
    upper_wick = max(high - max(op, close), 0)
    lower_wick = max(min(op, close) - low, 0)
    body_pct = body / rng
    upper_wick_pct = upper_wick / rng
    lower_wick_pct = lower_wick / rng
    bb_width_pct = ((bb_up - bb_low) / bb_mid * 100) if bb_mid not in (None, 0) and bb_up is not None and bb_low is not None else 0
    dist_ema20_pct = ((close - ema20) / ema20 * 100) if ema20 else 0
    dist_ema50_pct = ((close - ema50) / ema50 * 100) if ema50 else 0

    return {
        "close":       close,
        "rsi":         rsi,
        "stoch_k":     stoch_k,
        "stoch_d":     stoch_d,
        "macd":        macd,
        "macd_signal": macd_sig,
        "macd_hist":   macd_hist,
        "ema20":       ema20,
        "ema50":       ema50,
        "ema200":      ema200,
        "adx":         adx,
        "plus_di":     plus_di,
        "minus_di":    minus_di,
        "bb_upper":    bb_up,
        "bb_lower":    bb_low,
        "bb_mid":      bb_mid,
        "bb_width_pct": bb_width_pct,
        "dist_ema20_pct": dist_ema20_pct,
        "dist_ema50_pct": dist_ema50_pct,
        "volume":      last["v"],
        "avg_volume":  avg_vol,
        "vol_trend":   vol_trend,
        "body_pct":    body_pct,
        "upper_wick_pct": upper_wick_pct,
        "lower_wick_pct": lower_wick_pct,
        "high":        high,
        "low":         low,
        "open":        op,
    }



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
#  SCORING ENGINE
# ===============================================================

def score_trend_direction(ind, direction):
    """EMA stack alignment. Max 3 points. Returns -99 if EMAs too compressed."""
    pts   = 0
    price = ind.get("close", 0)
    ema20 = ind.get("ema20")
    ema50 = ind.get("ema50")
    ema200= ind.get("ema200")

    # Need at minimum EMA20 and EMA50
    if None in (price, ema20, ema50):
        return 0

    # EMA compression check — use EMA20 vs EMA50 if EMA200 unavailable
    if ema200 is not None:
        ema_range = (max(ema20, ema50, ema200) - min(ema20, ema50, ema200)) / ema20
    else:
        ema_range = abs(ema20 - ema50) / ema20

    # Raised threshold from 0.003 (0.3%) to 0.01 (1.0%)
    # Original was too strict, rejecting all normal markets
    if ema_range < 0.01:
        return -99   # EMAs too compressed — ranging market, skip

    if direction == "long":
        if price > ema20 and price > ema50: pts += 2
        if ema200 is not None and price > ema200: pts += 1
    else:
        if price < ema20 and price < ema50: pts += 2
        if ema200 is not None and price < ema200: pts += 1
    return pts


def score_momentum(ind, direction):
    """Stochastic RSI + MACD + Volume trend. Max 3 points."""
    pts      = 0
    rsi      = ind.get("rsi", 50)
    hist     = ind.get("macd_hist", 0)
    macd     = ind.get("macd")
    signal   = ind.get("macd_signal")
    stoch_k  = ind.get("stoch_k")
    vol_trend= ind.get("vol_trend", "flat")

    # Stochastic RSI — better momentum signal than plain RSI
    if stoch_k is not None:
        if direction == "long"  and stoch_k < 30:         pts += 1  # oversold, bounce likely
        elif direction == "long"  and 30 <= stoch_k <= 60: pts += 1  # momentum building
        if direction == "short" and stoch_k > 70:         pts += 1  # overbought, drop likely
        elif direction == "short" and 40 <= stoch_k <= 70: pts += 1  # momentum falling
    else:
        # Fallback to regular RSI if Stoch RSI unavailable
        if direction == "long"  and rsi < 55: pts += 1
        if direction == "short" and rsi > 45: pts += 1

    # MACD confirms direction
    if hist is not None:
        if direction == "long"  and hist > 0: pts += 1
        if direction == "short" and hist < 0: pts += 1

    # Volume rising in direction of trade = strong confirmation
    if vol_trend == "rising": pts += 1

    return min(pts, 3)


def score_volatility(ind, direction):
    """Bollinger Band position and squeeze. Max 2 points. Returns -99 if too flat."""
    pts     = 0
    price   = ind.get("close", 0)
    bb_mid  = ind.get("bb_mid")
    bb_up   = ind.get("bb_upper")
    bb_low  = ind.get("bb_lower")
    if None in (bb_mid, bb_up, bb_low) or bb_up == bb_low:
        return 0
    bb_width = (bb_up - bb_low) / bb_mid if bb_mid > 0 else 0

    # Hard veto only for truly dead markets (< 0.3% width)
    if bb_width < 0.003:
        return -99   # market completely dead — BB width < 0.3%

    # Soft scoring based on width
    # Squeeze setup (coiling for a breakout): 0.3-1.5% width
    if 0.003 <= bb_width < 0.015: pts += 1
    # Healthy volatility (> 1.5% width)
    elif bb_width >= 0.015: pts += 1

    # Price position relative to midline
    if direction == "long"  and price > bb_mid: pts += 1
    if direction == "short" and price < bb_mid: pts += 1
    return pts


def score_trend_strength(ind):
    """ADX strength. Max 2 points. Returns -99 if veto.
    
    Threshold: ADX >= 20 AND DI gap >= 5 points.
    
    Research consensus (TradingView traders, 2024-2025):
    - ADX 20 = minimum viable trend (standard default)
    - ADX 25 = confirmed trend (recommended for most)
    - ADX 30 = strong trend (strict)
    
    We use 20 as the hard floor but require the DI gap (|+DI - -DI| >= 5)
    to confirm there's actual directional pressure behind the trend.
    This avoids entering on weak ADX with compressed DI lines (no real bias).
    """
    adx      = ind.get("adx")
    plus_di  = ind.get("plus_di", 0)
    minus_di = ind.get("minus_di", 0)
    if adx is None:
        return 0

    # Hard veto — below 20 is ranging/choppy regardless of DI
    if adx < 20:
        return -99

    # DI gap check — require meaningful directional separation
    di_gap = abs(plus_di - minus_di)
    if di_gap < 5:
        return -99   # ADX present but no clear directional bias

    # Score by strength
    if adx >= 30:
        return 2     # strong trend
    if adx >= 25:
        return 2     # confirmed trend
    return 1         # moderate trend (20-25)


def determine_direction(ind):
    """
    Determine long or short bias from 5m indicators.
    Returns 'long', 'short', or None if no clear bias.
    
    LIVE MODE (v2):
    - ADX is optional (if it fails to calculate, default to 22)
    - Longs require ADX >= 24, Shorts require ADX >= 20
    - Require 2+ of 4 signals aligned
    """
    price   = ind.get("close", 0)
    ema20   = ind.get("ema20")
    ema50   = ind.get("ema50")
    plus_di = ind.get("plus_di")
    minus_di= ind.get("minus_di")
    rsi     = ind.get("rsi")
    adx     = ind.get("adx", 22)  # ← Default to 22 if None/missing (live mode = safer)
    
    # Must have core indicators
    if None in (price, ema20, ema50, plus_di, minus_di, rsi):
        return None
    
    # If ADX is None, use default of 22 (conservative for live)
    if adx is None:
        adx = 22
    
    long_signals  = 0
    short_signals = 0
    if price > ema20:  long_signals  += 1
    else:              short_signals += 1
    if price > ema50:  long_signals  += 1
    else:              short_signals += 1
    if plus_di > minus_di: long_signals  += 1
    else:                  short_signals += 1
    if rsi > 50:       long_signals  += 1
    else:              short_signals += 1
    
    # Longs: stricter ADX requirement (ADX >= 24) + 2+ signals
    if long_signals >= 2:
        if adx < 24:
            return None  # Longs need stronger trend confirmation
        return "long"
    
    # Shorts: standard ADX requirement (ADX >= 20) + 2+ signals
    if short_signals >= 2:
        if adx < 20:
            return None  # Shorts need minimum trend
        return "short"
    
    return None


def check_hard_vetoes(ind, direction, rsi):
    """Check RSI extremes. Returns veto reason or None."""
    if direction == "long"  and rsi > 70: return "RSI overbought"
    if direction == "short" and rsi < 30: return "RSI oversold"
    return None


def confirm_1h(coin, direction):
    """
    1h timeframe should agree with direction IF available.
    Returns True if:
    - 1h candles exist AND align with direction
    - 1h candles don't exist (assume OK)
    Returns False only if candles exist but don't align.
    """
    ind_1h = get_indicators(coin, "1h", n=50)
    
    # If 1h candles don't exist, don't reject (assume OK)
    if ind_1h is None:
        return True  # ← Changed from False to True
    
    price  = ind_1h.get("close", 0)
    ema20  = ind_1h.get("ema20")
    rsi    = ind_1h.get("rsi")
    
    if None in (ema20, rsi):
        return True  # ← Changed from False to True (missing data = assume OK)
    
    if direction == "long"  and price > ema20 and rsi > 50: return True
    if direction == "short" and price < ema20 and rsi < 50: return True
    
    return False  # Only reject if candles exist but don't align


def check_news(coin):
    """
    Search for recent news. Returns: 'positive', 'neutral', 'veto'
    """
    if not TG_TOKEN:   # If no Telegram, skip news (no web search available)
        return "neutral"
    try:
        res = requests.get(
            "https://api.coingecko.com/api/v3/search/trending",
            timeout=8
        )
        # Simple heuristic: check if coin appears in trending with negative context
        # Full news search requires a news API — for now use neutral as default
        # and rely on veto words in a basic headline scrape
        news_res = requests.get(
            f"https://cryptopanic.com/api/v1/posts/?auth_token=public&currencies={coin}&filter=important",
            timeout=8
        )
        if news_res.status_code == 200:
            posts = news_res.json().get("results", [])
            for post in posts[:5]:
                title = post.get("title", "").lower()
                for word in NEWS_VETO_WORDS:
                    if word in title:
                        print(f"  [{coin}] News veto: '{word}' found in '{title[:60]}'")
                        return "veto"
                for word in NEWS_POSITIVE_WORDS:
                    if word in title:
                        return "positive"
        return "neutral"
    except Exception:
        return "neutral"


def get_funding_adjustment(coin, direction, funding_rates):
    """
    If funding rate is heavily against direction, drop one tier.
    Returns tier adjustment: 0 (no change) or -1 (drop one tier)
    """
    rate = funding_rates.get(coin, 0)
    # Positive funding = longs pay shorts
    # If rate > 0.05%/hr and we're long, cost is high
    if direction == "long"  and rate >  0.0005: return -1
    if direction == "short" and rate < -0.0005: return -1
    return 0





def classify_strategy(ind, direction):
    """
    Classify a setup into the three strategy buckets discussed in the Claude chat:
    EMA Bounce, Stop Hunt Reversal, or Opening Range Breakout (ORB).
    Returns (strategy_code, strategy_label, setup_note).
    """
    price = ind.get("close", 0)
    ema20 = ind.get("ema20")
    ema50 = ind.get("ema50")
    bb_up = ind.get("bb_upper")
    bb_low = ind.get("bb_lower")
    bb_width_pct = ind.get("bb_width_pct", 0)
    stoch_k = ind.get("stoch_k")
    rsi = ind.get("rsi", 50)
    adx = ind.get("adx") or 0
    hist = ind.get("macd_hist") or 0
    body_pct = ind.get("body_pct", 0)
    upper_wick_pct = ind.get("upper_wick_pct", 0)
    lower_wick_pct = ind.get("lower_wick_pct", 0)

    near_ema20 = ema20 is not None and abs(price - ema20) / ema20 <= 0.006
    near_ema50 = ema50 is not None and abs(price - ema50) / ema50 <= 0.008
    near_band_low = bb_low is not None and abs(price - bb_low) / price <= 0.004
    near_band_up  = bb_up is not None and abs(price - bb_up) / price <= 0.004
    squeeze = 0.30 <= bb_width_pct <= 1.50

    if direction == "long":
        if (near_band_low and ((stoch_k is not None and stoch_k <= 20) or rsi <= 35)
                and (lower_wick_pct >= 0.35 or body_pct >= 0.55)):
            return "SHR", "Stop Hunt Reversal", "swept lower band and rebounded"
        if (near_ema20 or near_ema50) and hist >= 0 and adx >= 25:
            return "EMA", "EMA Bounce", "pullback into trend support"
        if squeeze and price >= (ema20 or price) and hist >= 0 and body_pct >= 0.55:
            return "ORB", "Opening Range Breakout", "squeeze expanding upward"
    else:
        if (near_band_up and ((stoch_k is not None and stoch_k >= 80) or rsi >= 65)
                and (upper_wick_pct >= 0.35 or body_pct >= 0.55)):
            return "SHR", "Stop Hunt Reversal", "swept upper band and rejected"
        if (near_ema20 or near_ema50) and hist <= 0 and adx >= 20:
            return "EMA", "EMA Bounce", "retest into EMA resistance"
        if squeeze and price <= (ema20 or price) and hist <= 0 and body_pct >= 0.55:
            return "ORB", "Opening Range Breakout", "squeeze breaking downward"

    if near_ema20 or near_ema50:
        return "EMA", "EMA Bounce", "trend continuation structure"
    if squeeze:
        return "ORB", "Opening Range Breakout", "volatility compression setup"
    return "SHR", "Stop Hunt Reversal", "mean reversion style entry"


def compute_confidence(result, sentiment_score):
    """Convert the raw scoring stack into a 0-100 confidence percentage."""
    base = 45
    base += min(max(result.get("score", 0), 0), 10) * 4

    adx = result.get("adx") or 0
    if adx >= 40:
        base += 10
    elif adx >= 30:
        base += 7
    elif adx >= 25:
        base += 4

    news = result.get("news", "neutral")
    if news == "positive":
        base += 4

    funding = result.get("funding", 0)
    direction = result.get("direction")
    if direction == "long" and funding > 0.0005:
        base -= 4
    if direction == "short" and funding < -0.0005:
        base -= 4

    if direction == "long" and sentiment_score > 0.1:
        base += 4
    elif direction == "short" and sentiment_score < -0.1:
        base += 4
    elif abs(sentiment_score) < 0.1:
        base += 1
    else:
        base -= 3

    strat = result.get("strategy_code")
    if strat == "EMA":
        base += 2
    elif strat == "ORB":
        base += 1

    return max(50, min(95, int(round(base))))






def check_liquidity_quality(ind, funding=0.0):
    """
    Filter out structurally low-quality perp candidates.
    Uses price floor and extreme funding only (dollar vol fields not available).
    """
    price = ind.get("close", 0) or 0

    if price and price < MIN_PRICE_FILTER:
        return f"Price < {MIN_PRICE_FILTER:g} (too noisy)"
    
    # funding is a decimal, e.g. 0.0006 = 0.06%
    if funding is not None:
        funding_pct = abs(float(funding)) * 100.0
        if funding_pct > MAX_ABS_FUNDING_BPS:
            return f"Funding > {MAX_ABS_FUNDING_BPS:.2f}%"
    return None

def detect_choppy_structure(ind):
    """
    Return a veto reason string when structure is too choppy to trade.
    This is stricter than the base scoring vetoes and is meant to avoid
    forcing trades in thin, compressed, low-conviction conditions.
    """
    price = ind.get("close", 0) or 0
    ema20 = ind.get("ema20")
    ema50 = ind.get("ema50")
    ema200 = ind.get("ema200")
    adx = ind.get("adx") or 0
    plus_di = ind.get("plus_di") or 0
    minus_di = ind.get("minus_di") or 0
    bb_width_pct = ind.get("bb_width_pct", 0) or 0
    body_pct = ind.get("body_pct", 0) or 0

    if not price:
        return None

    di_gap = abs(plus_di - minus_di)

    ema20_50 = (abs(ema20 - ema50) / price * 100) if ema20 and ema50 else 999
    ema50_200 = (abs(ema50 - ema200) / price * 100) if ema50 and ema200 else 999

    # Dead market / fee trap
    if bb_width_pct < 0.80:
        return "BB width < 0.8% (too flat)"

    # Tight EMAs + weak trend = range chop
    if ema20_50 < 0.35 and ema50_200 < 0.80 and adx < 25:
        return "EMAs compressed + weak ADX (ranging)"

    # DI lines too close together and candles lack conviction
    if adx < 25 and di_gap < 8 and body_pct < 0.55:
        return "Low ADX + tight DI gap + weak candle body (choppy)"

    # Narrow bands with weak trend conviction
    if bb_width_pct < 1.20 and adx < 25:
        return "Low volatility + weak trend (choppy)"

    return None


def compute_trend_quality(result):
    """
    Convert trend context into a simple quality bucket used for exits.
    Returns a dict with label, score, di_gap, and regime.
    """
    adx = result.get("adx") or 0
    plus_di = result.get("plus_di") or 0
    minus_di = result.get("minus_di") or 0
    di_gap = abs(plus_di - minus_di)
    vol_trend = result.get("vol_trend", "flat")
    bb_width_pct = result.get("bb_width_pct", 0) or 0
    sentiment = result.get("sentiment_score", 0) or 0
    direction = result.get("direction")

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

    if bb_width_pct >= 2.5:
        regime = "high"
    elif bb_width_pct >= 1.2:
        regime = "normal"
    else:
        regime = "low"

    if direction == "long" and sentiment > 0.10:
        score += 1
    elif direction == "short" and sentiment < -0.10:
        score += 1

    if score >= 6:
        label = "strong"
    elif score >= 3:
        label = "normal"
    else:
        label = "weak"

    return {
        "label": label,
        "score": score,
        "di_gap": di_gap,
        "vol_regime": regime,
    }


def compute_dynamic_risk_targets(result, size, leverage):
    """
    Dynamic stop / target model for Hyperliquid perps.

    Logic:
      classify setup
      measure volatility
      measure trend quality
      set stop width from setup + volatility
      set TP1 / TP2 from trend quality + notional
      avoid one-size-fits-all exit distances
    """
    coin = result["coin"]
    direction = result["direction"]
    price = result["price"]
    strategy = result.get("strategy_code", "EMA")
    adx = result.get("adx") or 25
    bb_width_pct = result.get("bb_width_pct", 1.5) or 1.5
    trend_ctx = compute_trend_quality(result)
    trend_label = trend_ctx["label"]
    vol_regime = trend_ctx["vol_regime"]
    notional = max(size * leverage, 1e-9)

    # ---- stop width ----
    # Base stop by setup type
    if strategy == "EMA":
        sl_pct = 1.15
    elif strategy == "ORB":
        sl_pct = 1.40
    else:  # SHR
        sl_pct = 1.85

    # Volatility adjustment
    if vol_regime == "high":
        sl_pct += 0.35
    elif vol_regime == "low":
        sl_pct -= 0.10

    # Trend-quality adjustment
    if trend_label == "strong":
        sl_pct -= 0.10 if strategy in ("EMA", "ORB") else 0.05
    elif trend_label == "weak":
        sl_pct += 0.20

    # Slight asymmetry: shorts can snap harder on HL alts
    if direction == "short":
        sl_pct += 0.10

    # Hard guardrails
    sl_pct = max(0.95, min(2.60, sl_pct))

    # ---- TP dollar targets ----
    if trend_label == "strong":
        tp1_usd, tp2_usd = 5.0, 12.0
    elif trend_label == "normal":
        tp1_usd, tp2_usd = 3.5, 8.5
    else:
        tp1_usd, tp2_usd = 2.25, 5.5

    # Setup adjustment
    if strategy == "EMA":
        tp1_usd *= 0.95
        tp2_usd *= 1.00
    elif strategy == "ORB":
        tp1_usd *= 1.05
        tp2_usd *= 1.15
    else:  # SHR
        tp1_usd *= 0.90
        tp2_usd *= 1.05

    # Volatility adjustment
    if vol_regime == "high":
        tp1_usd *= 1.10
        tp2_usd *= 1.15
    elif vol_regime == "low":
        tp1_usd *= 0.90
        tp2_usd *= 0.95

    # Convert target dollars to move percentages
    tp1_pct = tp1_usd / notional
    tp2_pct = tp2_usd / notional
    sl_frac = sl_pct / 100.0

    # Keep reward above risk
    min_tp1_rr = 1.35 if strategy != "SHR" else 1.20
    min_tp2_rr = 2.40 if trend_label != "strong" else 2.80

    tp1_pct = max(tp1_pct, sl_frac * min_tp1_rr)
    tp2_pct = max(tp2_pct, sl_frac * min_tp2_rr)

    # Reasonable caps for a small account on HL
    tp1_pct = min(tp1_pct, 0.10)
    tp2_pct = min(tp2_pct, 0.18)

    if direction == "long":
        sl  = round(price * (1 - sl_frac), 6)
        tp1 = round(price * (1 + tp1_pct), 6)
        tp2 = round(price * (1 + tp2_pct), 6)
    else:
        sl  = round(price * (1 + sl_frac), 6)
        tp1 = round(price * (1 - tp1_pct), 6)
        tp2 = round(price * (1 - tp2_pct), 6)

    return {
        "sl": sl,
        "tp1": tp1,
        "tp2": tp2,
        "sl_pct": round(sl_pct, 2),
        "tp1_pct": round(tp1_pct * 100, 2),
        "tp2_pct": round(tp2_pct * 100, 2),
        "trend_quality": trend_label,
        "trend_quality_score": trend_ctx["score"],
        "volatility_regime": vol_regime,
        "di_gap": round(trend_ctx["di_gap"], 2),
        "notional": round(notional, 2),
    }



def score_pair(coin, prices, funding_rates, state):
    """
    Full scoring pipeline for one coin.
    Returns dict with score, direction, tier info or None if vetoed.
    """
    price = prices.get(coin)
    if price is None:
        return {"coin": coin, "status": "missing_price"}

    ind_5m = get_indicators(coin, "5m")
    if ind_5m is None:
        return {"coin": coin, "status": "missing_indicators"}

    direction = determine_direction(ind_5m)
    if direction is None:
        return {"coin": coin, "status": "no_direction"}

    rsi = ind_5m.get("rsi", 50)
    liq_veto = check_liquidity_quality(ind_5m, funding_rates.get(coin, 0))
    if liq_veto:
        return {"coin": coin, "status": "vetoed", "vetoed": True, "reason": liq_veto}

    veto = check_hard_vetoes(ind_5m, direction, rsi)
    if veto:
        return {"coin": coin, "status": "vetoed", "vetoed": True, "reason": veto}

    chop_veto = detect_choppy_structure(ind_5m)
    if chop_veto:
        return {"coin": coin, "status": "vetoed", "vetoed": True, "reason": chop_veto}

    s_trend    = score_trend_direction(ind_5m, direction)
    s_momentum = score_momentum(ind_5m, direction)
    s_vol      = score_volatility(ind_5m, direction)
    s_adx      = score_trend_strength(ind_5m)

    if s_trend == -99:
        return {"coin": coin, "status": "vetoed", "vetoed": True, "reason": "EMAs compressed (ranging)"}
    if s_vol == -99:
        return {"coin": coin, "status": "vetoed", "vetoed": True, "reason": "BB width < 0.3% (dead market)"}
    if s_adx == -99:
        return {"coin": coin, "status": "vetoed", "vetoed": True, "reason": "ADX < 20 or DI gap < 5 (choppy)"}

    score_5m = s_trend + s_momentum + s_vol + s_adx
    if score_5m < MIN_SCORE:
        return {"coin": coin, "status": "below_min_score", "score_5m": score_5m}

    if not confirm_1h(coin, direction):
        return {"coin": coin, "status": "vetoed", "vetoed": True, "reason": "1h TF not aligned"}

    news = check_news(coin)
    if news == "veto":
        return {"coin": coin, "status": "vetoed", "vetoed": True, "reason": "Negative news"}

    final_score = score_5m + (1 if news == "positive" else 0)
    tier_adj = get_funding_adjustment(coin, direction, funding_rates)
    effective_score = final_score + tier_adj

    tier_score = min(effective_score, 11)
    tier_score = max(tier_score, 8)
    tier_key   = min(tier_score, max(TIERS.keys()))
    while tier_key not in TIERS and tier_key > 8:
        tier_key -= 1
    if tier_key not in TIERS:
        return {"coin": coin, "status": "tier_reject", "effective_score": effective_score, "tier_key": tier_key}

    strategy_code, strategy_label, setup_note = classify_strategy(ind_5m, direction)
    sentiment_score, sentiment_label, sentiment_sources = get_market_sentiment_details()

    result = {
        "status":    "valid",
        "coin":      coin,
        "vetoed":    False,
        "direction": direction,
        "score":     final_score,
        "tier_score":tier_score,
        "tier":      TIERS[tier_key],
        "price":     price,
        "rsi":       rsi,
        "adx":       ind_5m.get("adx"),
        "plus_di":   ind_5m.get("plus_di"),
        "minus_di":  ind_5m.get("minus_di"),
        "vol_trend": ind_5m.get("vol_trend", "flat"),
        "body_pct":  ind_5m.get("body_pct", 0),
        "news":      news,
        "funding":   funding_rates.get(coin, 0),
        "strategy_code": strategy_code,
        "strategy":  strategy_label,
        "setup_note": setup_note,
        "sentiment_label": sentiment_label,
        "sentiment_score": sentiment_score,
        "bb_width_pct": ind_5m.get("bb_width_pct", 0),
        "indicators": {
            "ema20": ind_5m.get("ema20"),
            "ema50": ind_5m.get("ema50"),
            "bb_upper": ind_5m.get("bb_upper"),
            "bb_lower": ind_5m.get("bb_lower"),
            "stoch_k": ind_5m.get("stoch_k"),
            "macd_hist": ind_5m.get("macd_hist"),
        },
    }
    result["confidence"] = compute_confidence(result, sentiment_score)
    return result


def close_paper_position(coin, pos, price, reason, state, trades):
    """Close a paper position early for replacement."""
    direction = pos["direction"]
    entry     = pos["entry"]
    if direction == "long":
        raw_pct = (price - entry) / entry
    else:
        raw_pct = (entry - price) / entry
    final_pnl = round(pos["size"] * raw_pct * pos["leverage"], 4)

    pos["pnl"]    = final_pnl
    pos["status"] = "closed_replaced"
    state["daily_pnl"]    += final_pnl
    state["daily_trades"] += 1

    for t in trades:
        if t["coin"] == coin and t["status"] == "open":
            t.update({"status": "closed_replaced", "exit": price, "pnl": final_pnl, "closed_at": datetime.now(timezone.utc).isoformat()})

    del state["paper_positions"][coin]
    save_state(state)
    save_trades(trades)

    sign = "+" if final_pnl >= 0 else ""
    tg(f"[PAPER] REPLACED {coin} ({reason})\nExit @ ${price:.5f} | P&L: {sign}${final_pnl:.2f}")
    print(f"  [{coin}] [PAPER] REPLACED — {reason} | P&L: {sign}${final_pnl:.2f}")


def find_replacement_candidate(state, results, prices):
    """
    Conservative replacement logic — avoids over-churning.

    Rule 1 — Early bad trade (10+ min open):
      Net loss > 2x the fee AND replacement scores strictly higher.

    Rule 2 — Score upgrade (30+ min open):
      Position scored 8 or 9, a score-10 is available, position is flat/negative.

    Rule 3 — Stagnant (4+ hours open):
      Less than 30% progress toward TP1, net P&L is negative.

    Never replace:
      - TP1 already hit (riding to TP2)
      - Within 40% of TP1
      - Positive positions (except stagnant rule)
    """
    if not results or not state["paper_positions"]:
        return None, None

    best_new = results[0]
    now      = datetime.now(timezone.utc)

    for coin, pos in state["paper_positions"].items():
        if pos["tp1_hit"]:
            continue  # riding to TP2 — never touch

        try:
            opened       = datetime.fromisoformat(pos["opened_at"])
            minutes_open = (now - opened).total_seconds() / 60
        except Exception:
            minutes_open = 999

        entry_score = pos.get("score", MIN_SCORE)
        gross       = pos["pnl"]
        notional    = pos["size"] * pos.get("leverage", 5)
        fee         = notional * 0.0006
        net         = gross - fee

        price      = prices.get(coin, 0)
        tp1        = pos["tp1"]
        entry_px   = pos["entry"]
        dist_total = abs(tp1 - entry_px)
        dist_left  = abs(tp1 - price)
        pct_to_tp1 = (dist_left / dist_total) if dist_total > 0 else 1.0

        # Never replace if close to TP1
        if pct_to_tp1 < 0.4:
            continue

        # Rule 1 — Early bad trade: clearly losing AND better setup available
        if minutes_open >= 30:
            if net < -(fee * 2) and best_new.get("score", -999) > entry_score:
                return coin, best_new

        # Rule 2 — Score upgrade: weaker entry, score-10 now available
        if minutes_open >= 60 and entry_score < 10:
            if best_new.get("score", -999) >= 10 and net <= 0:
                return coin, best_new

        # Rule 3 — Stagnant: 4+ hours, barely moved, and losing
        if minutes_open >= 360:
            dist_moved = abs(price - entry_px)
            progress   = (dist_moved / dist_total) if dist_total > 0 else 0
            if progress < 0.3 and net < 0:
                return coin, best_new

    return None, None
def open_paper_position(result, state, trades):
    """Record a new phantom trade with dynamic stop / target distances."""
    if not result or result.get("status") not in (None, "valid"):
        return
    required = ("coin", "direction", "price", "tier", "score")
    if any(k not in result for k in required):
        return
    coin      = result["coin"]
    direction = result["direction"]
    price     = result["price"]
    tier      = result["tier"]
    size      = tier["size"]
    if MODE == "live" and LIVE_SAFE_MODE:
        size = max(10.0, round(size * LIVE_SIZE_MULTIPLIER, 2))
    max_lev   = _max_lev_map.get(coin, 20)
    leverage  = min(tier["leverage"], max_lev)

    exit_plan = compute_dynamic_risk_targets(result, size, leverage)

    opened_at = datetime.now(timezone.utc).isoformat()
    pos = {
        "coin":       coin,
        "direction":  direction,
        "entry":      price,
        "size":       size,
        "leverage":   leverage,
        "score":      result["score"],
        "tier":       tier["label"],
        "sl":         exit_plan["sl"],
        "tp1":        exit_plan["tp1"],
        "tp2":        exit_plan["tp2"],
        "sl_pct":     exit_plan["sl_pct"],
        "tp1_pct":    exit_plan["tp1_pct"],
        "tp2_pct":    exit_plan["tp2_pct"],
        "trend_quality": exit_plan["trend_quality"],
        "trend_quality_score": exit_plan["trend_quality_score"],
        "volatility_regime": exit_plan["volatility_regime"],
        "di_gap":     exit_plan["di_gap"],
        "notional":   exit_plan["notional"],
        "strategy_code": result.get("strategy_code"),
        "strategy":   result.get("strategy"),
        "confidence": result.get("confidence", 0),
        "tp1_hit":    False,
        "sl_at_be":   False,
        "opened_at":  opened_at,
        "pnl":        0.0,
        "status":     "open",
    }
    state["paper_positions"][coin] = pos

    trade_record = dict(pos)
    trade_record["mode"] = "paper"
    trades.append(trade_record)
    save_trades(trades)
    save_state(state)

    mode_tag = "[PAPER] " if MODE == "paper" else ""

    if MODE == "live" and _exchange is not None:
        try:
            is_buy = direction == "long"
            _exchange.update_leverage(leverage, coin, is_cross=True)
            qty = round(size * leverage / price, 6)
            resp = _exchange.market_open(coin, is_buy, qty, slippage=0.02)
            if resp and resp.get("status") == "ok":
                print(f"  [{coin}] [LIVE] order placed")
        except Exception as e:
            print(f"  [{coin}] [LIVE ERROR] {e}")

    sign = "📈" if direction == "long" else "📉"
    conf = result.get("confidence", 0)
    tg(f"{mode_tag}{sign} OPEN {coin} {direction.upper()}\nStrategy: {result.get('strategy', 'Unclassified')} | Confidence: {conf}%\nEntry: ${price:.5f} | SL: ${pos['sl']:.5f} | TP1: ${pos['tp1']:.5f} | TP2: ${pos['tp2']:.5f}")
    print(f"  [{coin}] {mode_tag}{direction.upper()} opened | {result.get('strategy', 'Unclassified')} | conf {conf}% | score {result['score']} | entry ${price:.5f}")


def monitor_paper_positions(state, trades, prices):
    """Check all open paper positions against current prices."""
    closed = []
    for coin, pos in state["paper_positions"].items():
        price = prices.get(coin)
        if price is None:
            continue

        direction = pos["direction"]
        entry     = pos["entry"]
        tp1_hit   = pos["tp1_hit"]

        # Calculate current paper P&L
        if direction == "long":
            raw_pct = (price - entry) / entry
        else:
            raw_pct = (entry - price) / entry
        pnl = round(pos["size"] * raw_pct * pos["leverage"], 4)
        pos["pnl"] = pnl

        # Check SL
        sl_hit = (direction == "long"  and price <= pos["sl"]) or \
                 (direction == "short" and price >= pos["sl"])

        if sl_hit:
            final_pnl = round(pos["size"] * ((pos["sl"] - entry) / entry if direction == "long"
                               else (entry - pos["sl"]) / entry) * pos["leverage"], 4)
            pos["pnl"]    = final_pnl
            pos["status"] = "closed_sl"
            state["daily_pnl"]    += final_pnl
            state["daily_trades"] += 1
            # Cooldown
            state["cooldowns"][coin] = time.time() + (COOLDOWN_MINS * 60)
            closed.append(coin)
            tg("[PAPER] SL hit: " + coin + " @ $" + f"{price:.5f}"
               + "\nP&L: $" + f"{final_pnl:.2f}"
               + "\nDaily P&L: $" + f"{state['daily_pnl']:.2f}")
            print(f"  [{coin}] [PAPER] SL hit @ ${price:.5f} | P&L: ${final_pnl:.2f}")
            # Update trades file
            for t in trades:
                if t["coin"] == coin and t["status"] == "open":
                    t.update({"status": "closed_sl", "exit": price, "pnl": final_pnl, "closed_at": datetime.now(timezone.utc).isoformat()})
            continue

        # Check TP1
        tp1_triggered = (direction == "long"  and price >= pos["tp1"] and not tp1_hit) or \
                        (direction == "short" and price <= pos["tp1"] and not tp1_hit)

        if tp1_triggered:
            partial_pnl = round(pos["size"] * 0.5 * ((pos["tp1"] - entry) / entry
                          if direction == "long" else (entry - pos["tp1"]) / entry)
                          * pos["leverage"], 4)
            pos["tp1_hit"] = True
            pos["sl_at_be"] = True
            pos["sl"] = entry   # move SL to breakeven
            state["daily_pnl"]    += partial_pnl
            state["daily_trades"] += 1
            tg("[PAPER] TP1 hit: " + coin + " @ $" + f"{price:.5f}"
               + "\nPartial P&L: +$" + f"{partial_pnl:.2f}"
               + "\nSL moved to breakeven. Riding to TP2...")
            print(f"  [{coin}] [PAPER] TP1 hit @ ${price:.5f} | +${partial_pnl:.2f} | SL → breakeven")
            # Log TP1 partial as a separate trade record
            for t in trades:
                if t["coin"] == coin and t["status"] == "open":
                    trades.append({**t, "status": "closed_tp1", "exit": price, "pnl": partial_pnl, "closed_at": datetime.now(timezone.utc).isoformat()})
                    break

        # Check TP2
        tp2_triggered = (direction == "long"  and price >= pos["tp2"] and tp1_hit) or \
                        (direction == "short" and price <= pos["tp2"] and tp1_hit)

        if tp2_triggered:
            final_pnl = round(pos["size"] * 0.5 * ((pos["tp2"] - entry) / entry
                         if direction == "long" else (entry - pos["tp2"]) / entry)
                         * pos["leverage"], 4)
            pos["pnl"]    = pnl
            pos["status"] = "closed_tp2"
            state["daily_pnl"]    += final_pnl
            state["daily_trades"] += 1
            state["cooldowns"][coin] = time.time() + (COOLDOWN_MINS * 60)
            closed.append(coin)
            tg("[PAPER] TP2 hit: " + coin + " @ $" + f"{price:.5f}"
               + "\nFinal P&L: +$" + f"{final_pnl:.2f}"
               + "\nDaily P&L: $" + f"{state['daily_pnl']:.2f}")
            print(f"  [{coin}] [PAPER] TP2 hit @ ${price:.5f} | +${final_pnl:.2f}")
            for t in trades:
                if t["coin"] == coin and t["status"] == "open":
                    t.update({"status": "closed_tp2", "exit": price, "pnl": final_pnl, "closed_at": datetime.now(timezone.utc).isoformat()})

    # Remove closed positions
    for coin in closed:
        del state["paper_positions"][coin]

    save_state(state)
    save_trades(trades)

    # Check daily loss cap
    if state["daily_pnl"] <= -effective_daily_loss_cap() and not state["cap_hit"]:
        state["cap_hit"] = True
        save_state(state)
        tg("DAILY LOSS CAP HIT: -$" + f"{abs(state['daily_pnl']):.2f}"
           + "\nScanner stopped for today. Resumes tomorrow.")
        print(f"  [CAP] Daily loss cap hit: ${state['daily_pnl']:.2f} — stopping for today")


# ===============================================================
#  SCAN SUMMARY
# ===============================================================

def _strip_ansi(s):
    import re
    return re.sub(r"\x1b\[[0-9;]*m", "", s)

def _plain_len(s):
    try:
        from wcwidth import wcswidth
        width = wcswidth(_strip_ansi(s))
        return width if width >= 0 else len(_strip_ansi(s))
    except Exception:
        return len(_strip_ansi(s))

def _pad_cell(text, width, align="left"):
    visible = _plain_len(text)
    pad = max(width - visible, 0)
    if align == "right":
        return " " * pad + text
    if align == "center":
        left = pad // 2
        right = pad - left
        return " " * left + text + " " * right
    return text + " " * pad

def _fmt_money(val, decimals=2, sign=False):
    if sign:
        return f"${val:+,.{decimals}f}"
    return f"${val:,.{decimals}f}"

def _fmt_price(val):
    return f"${val:.5f}"

def _print_table(headers, rows, aligns=None):
    if aligns is None:
        aligns = ["left"] * len(headers)
    widths = []
    for i, h in enumerate(headers):
        w = _plain_len(str(h))
        for row in rows:
            w = max(w, _plain_len(str(row[i])))
        widths.append(w)
    top = "  ┌" + "┬".join("─" * (w + 2) for w in widths) + "┐"
    mid = "  ├" + "┼".join("─" * (w + 2) for w in widths) + "┤"
    bot = "  └" + "┴".join("─" * (w + 2) for w in widths) + "┘"
    print(f"{DIM}{top}{X}")
    header_line = "  │ " + " │ ".join(_pad_cell(str(h), widths[i], "center") for i, h in enumerate(headers)) + " │"
    print(f"{DIM}{header_line}{X}")
    print(f"{DIM}{mid}{X}")
    for row in rows:
        line = "  │ " + " │ ".join(_pad_cell(str(row[i]), widths[i], aligns[i]) for i in range(len(headers))) + " │"
        print(line)
    print(f"{DIM}{bot}{X}")

def send_scan_summary(scanned, results, vetoed_reasons, state, cycle, prices=None, trades=None, status_counts=None, exception_samples=None):
    """Send Telegram summary and print open-position/status footer."""
    if prices is None:
        prices = {}
    if trades is None:
        trades = load_trades()

    valid = [r for r in results if r and r.get("status") == "valid"]
    vetoed = [r for r in results if r and r.get("status") == "vetoed"]
    status_counts = status_counts or {}
    exception_samples = exception_samples or []
    missing_price = status_counts.get("missing_price", 0)
    missing_indicators = status_counts.get("missing_indicators", 0)
    no_direction = status_counts.get("no_direction", 0)
    uncategorized = status_counts.get("uncategorized", 0)
    open_pos = len(state["paper_positions"])
    pnl = state["daily_pnl"]
    pnl_emoji = "🟢" if pnl >= 0 else "🔴"

    closed_trades = [t for t in trades if t.get("status") not in ("open", None)]
    wins = [t for t in closed_trades if t.get("pnl", 0) > 0]
    losses = [t for t in closed_trades if t.get("pnl", 0) <= 0]
    total_pnl = sum(t.get("pnl", 0) for t in closed_trades)
    win_rate = (len(wins) / len(closed_trades) * 100) if closed_trades else 0
    profit_factor = (abs(sum(t["pnl"] for t in wins) / sum(t["pnl"] for t in losses)) if losses and sum(t["pnl"] for t in losses) != 0 else 0)
    sentiment_score, sentiment_label, sentiment_sources = get_market_sentiment_details()
    source_map = {k: v for k, v in sentiment_sources}

    accounted = len(valid) + len(vetoed) + missing_price + missing_indicators + no_direction + uncategorized
    print(
        f"Pairs: {scanned} scanned | "
        f"{G}{len(valid)} valid{X} | "
        f"{Y}{len(vetoed)} vetoed{X} | "
        f"{DIM}{missing_price} no price{X} | "
        f"{DIM}{missing_indicators} no indicators{X} | "
        f"{DIM}{no_direction} no direction{X} | "
        f"{DIM}{uncategorized} uncategorized{X}"
    )
    if accounted != scanned:
        print(f"{R}Count mismatch: accounted {accounted} of {scanned} scanned{X}")
    missing_price     = status_counts.get("missing_price", 0)
    missing_indicators= status_counts.get("missing_indicators", 0)
    no_direction      = status_counts.get("no_direction", 0)
    below_min_score   = status_counts.get("below_min_score", 0)
    tier_reject       = status_counts.get("tier_reject", 0)
    exception_count   = status_counts.get("exception", 0)
    uncategorized     = status_counts.get("uncategorized", 0)

    accounted = (
        len(valid)
        + len(vetoed)
        + missing_price
        + missing_indicators
        + no_direction
        + below_min_score
        + tier_reject
        + exception_count
        + uncategorized
    )
    print(
        f"Pairs: {scanned} scanned | "
        f"{G}{len(valid)} valid{X} | "
        f"{Y}{len(vetoed)} vetoed{X} | "
        f"{DIM}{missing_price} no price{X} | "
        f"{DIM}{missing_indicators} no indicators{X} | "
        f"{DIM}{no_direction} no direction{X} | "
        f"{DIM}{below_min_score} below score{X} | "
        f"{DIM}{tier_reject} tier reject{X} | "
        f"{R}{exception_count} exceptions{X} | "
        f"{DIM}{uncategorized} uncategorized{X}"
    )
    if accounted != scanned:
        print(f"{R}Count mismatch: accounted {accounted} of {scanned} scanned{X}")

    if vetoed_reasons:
        from collections import Counter
        counts = Counter(vetoed_reasons)
        for reason, count in counts.most_common(4):
            print(f"Veto: {reason} x{count}")
    mood_line = " | ".join([
        f"Mood: {sentiment_label}",
        f"IPI {source_map['IPI']}" if "IPI" in source_map else "IPI n/a",
        f"FGM {source_map['FGM']}" if "FGM" in source_map else "FGM n/a",
        f"ALT {source_map['ALT']}" if "ALT" in source_map else "ALT n/a",
        f"CMC {source_map['CMC']}" if "CMC" in source_map else "CMC n/a",
    ])
    print(mood_line)
    if exception_samples:
        print()
        print("  Debug: sample exceptions")
        for coin, err_type, err_msg in exception_samples[:8]:
            print(f"    - {coin}: {err_type}: {err_msg[:140]}")

    msg = f"""
📊 *SCAN #{cycle}* [{time.strftime('%H:%M:%S')}]

🏦 *Account*
├ Balance: ${state.get('balance_cache', 0):.2f}
├ Deployed: ${sum(p['size'] for p in state['paper_positions'].values()):.2f}
└ Open: {open_pos}/{effective_max_positions()}

🔍 *Scan Results*
├ Scanned: {scanned} pairs
├ Valid: {len(valid)} setups
├ Vetoed: {len(vetoed)}
├ Mood: {sentiment_label} ({sentiment_score:+.2f})
├ Sources: IPI {source_map['IPI'] if 'IPI' in source_map else 'n/a'} | FGM {source_map['FGM'] if 'FGM' in source_map else 'n/a'} | ALT {source_map['ALT'] if 'ALT' in source_map else 'n/a'} | CMC {source_map['CMC'] if 'CMC' in source_map else 'n/a'}
├ No price: {missing_price}
├ No indicators: {missing_indicators}
├ No direction: {no_direction}
"""

    if valid:
        top = sorted(valid, key=lambda x: (x.get("confidence", 0), x.get("score", 0), x.get("adx") or 0), reverse=True)[:3]
        msg += "\n📈 *Top Setups*\n"
        for i, r in enumerate(top, 1):
            arrow = "📈" if r["direction"] == "long" else "📉"
            connector = "└" if i == len(top) else "├"
            msg += f"{connector} {arrow} {r['coin']} {r['direction'].upper()} | {r['strategy']} | {r['confidence']}% | Score: {r['score']}/10\n"

    if state["paper_positions"]:
        msg += "\n💼 *Open Positions*\n"
        items = list(state["paper_positions"].items())
        for i, (coin, pos) in enumerate(items, 1):
            lev = pos.get("leverage", 5)
            gross = pos.get("pnl", 0)
            notional = pos["size"] * lev
            fee = round(notional * 0.0006, 3)
            net = round(gross - fee, 3)
            arrow = "📈" if pos["direction"] == "long" else "📉"
            sign = "+" if net >= 0 else ""
            connector = "└" if i == len(items) else "├"
            msg += f"{connector} {arrow} {coin:<6} {pos['direction'].upper():<5} {lev}x | {pos.get('strategy','?')} | {pos.get('confidence',0)}% | {sign}${net:.2f}\n"

    msg += f"""
📊 *Daily Stats*
├ P&L: {pnl_emoji} ${pnl:.2f}
├ Trades: {state['daily_trades']}
└ Unrealized: ${sum(p.get('pnl', 0) for p in state['paper_positions'].values()):.2f}

📈 *All-Time Stats*
├ Total P&L: ${total_pnl:.2f}
├ Win Rate: {win_rate:.1f}% ({len(wins)}W/{len(losses)}L)
├ Trades: {len(closed_trades)}
└ Profit Factor: {profit_factor:.2f}x
"""
    tg(msg)

    if valid:
        print()
        print(f"  {BOLD}Top 3 setups{X}  {DIM}(market mood: {sentiment_label} via aggregate sentiment, score {sentiment_score:+.2f}){X}")
        top_rows = []
        for r in sorted(valid, key=lambda x: (x.get("confidence", 0), x.get("score", 0), x.get("adx") or 0), reverse=True)[:3]:
            dir_col = G if r["direction"] == "long" else R
            top_rows.append([
                f"{C}{r['coin']}{X}",
                f"{dir_col}{r['direction'].upper()}{X}",
                f"{C if r.get('strategy_code','MIX')=='EMA' else (Y if r.get('strategy_code','MIX')=='ORB' else (B if r.get('strategy_code','MIX')=='SHR' else W))}{r.get('strategy_code', 'MIX')}{X}",
                f"{G if r.get('confidence',0) >= 80 else (Y if r.get('confidence',0) >= 65 else R)}{r.get('confidence', 0)}%{X}",
                str(r.get('score', 0)),
                f"{r.get('adx') or 0:.1f}",
                f"{r.get('rsi') or 0:.1f}",
                r['tier']['label'],
                f"{r.get('funding', 0):+.4%}",
            ])
        _print_table(["Coin", "Dir", "Strat", "Conf", "Score", "ADX", "RSI", "Tier", "Funding"], top_rows,
                     ["left", "left", "left", "right", "right", "right", "right", "left", "right"])

    if state["paper_positions"]:
        print()
        print(f"  {BOLD}Open paper positions{X}")
        pos_rows = []
        for coin, pos in state["paper_positions"].items():
            lev = pos.get("leverage", 5)
            entry = pos["entry"]
            price = prices.get(coin, entry)
            gross = pos.get("pnl", 0)
            notional = pos["size"] * lev
            fee = round(notional * 0.0006, 3)
            net = round(gross - fee, 3)
            sign_g = "+" if gross >= 0 else ""
            sign_n = "+" if net >= 0 else ""

            # Recompute live percentages from current stored levels instead of trusting stale cached pct fields
            if pos["direction"] == "long":
                sl_pct_live = max(0.0, (entry - pos["sl"]) / entry * 100) if entry else 0.0
                tp1_pct_live = max(0.0, (pos["tp1"] - entry) / entry * 100) if entry else 0.0
                tp2_pct_live = max(0.0, (pos["tp2"] - entry) / entry * 100) if entry else 0.0
            else:
                sl_pct_live = max(0.0, (pos["sl"] - entry) / entry * 100) if entry else 0.0
                tp1_pct_live = max(0.0, (entry - pos["tp1"]) / entry * 100) if entry else 0.0
                tp2_pct_live = max(0.0, (entry - pos["tp2"]) / entry * 100) if entry else 0.0

            tp1_usd = pos["size"] * 0.5 * (tp1_pct_live / 100) * lev
            tp2_usd = pos["size"] * 0.5 * (tp2_pct_live / 100) * lev
            sl_usd  = pos["size"] * (sl_pct_live / 100) * lev

            strat_code = pos.get("strategy_code", pos.get("strategy", "?"))[:4]
            strat_color = C if strat_code == "EMA" else (Y if strat_code == "ORB" else (B if strat_code == "SHR" else W))
            conf_val = pos.get('confidence', 0)
            conf_color = G if conf_val >= 80 else (Y if conf_val >= 65 else R)

            if pos.get("status") == "closed_sl":
                hit_flag = f"{R}[SL]{X}"
            elif pos.get("tp1_hit") and pos.get("sl_at_be"):
                hit_flag = f"{G}[TP1]{X} {C}[BE]{X}"
            elif pos.get("tp1_hit"):
                hit_flag = f"{G}[TP1]{X}"
            else:
                hit_flag = f"{DIM}…{X}"

            sl_prefix = f"{C}[BE]{X} " if pos.get("sl_at_be") and abs(pos['sl'] - entry) < 1e-12 else f"{R}[SL]{X} "
            tp1_prefix = f"{G}[TP1]{X} " if pos.get("tp1_hit") else f"{DIM}[..]{X} "
            tp2_prefix = f"{G}[TP2]{X} " if pos.get("status") == "closed_tp2" else f"{DIM}[..]{X} "

            pos_rows.append([
                f"{C}{coin}{X}",
                f"{G if pos['direction']=='long' else R}{pos['direction'].upper()}{X}",
                f"{strat_color}{strat_code}{X}",
                f"{conf_color}{conf_val}%{X}",
                f"{Y}{lev}x{X}",
                f"${entry:.5f}",
                f"${price:.5f}",
                f"{G if gross >= 0 else R}{sign_g}${gross:.2f}{X}",
                f"{G if net >= 0 else R}{sign_n}${net:.2f}{X}",
                f"{sl_prefix}{R}${pos['sl']:.5f}{X} {DIM}({sl_pct_live:.1f}% | ${sl_usd:.2f}){X}",
                f"{tp1_prefix}{G}${pos['tp1']:.5f}{X} {DIM}({tp1_pct_live:.1f}% | ${tp1_usd:.2f}){X}",
                f"{tp2_prefix}{G}${pos['tp2']:.5f}{X} {DIM}({tp2_pct_live:.1f}% | ${tp2_usd:.2f}){X}",
                hit_flag,
            ])
        _print_table(["Coin", "Dir", "Strat", "Conf", "Lev", "Entry", "Price", "Gross", "Net", "SL", "TP1", "TP2", "Hit"],
                     pos_rows,
                     ["left", "left", "left", "right", "right", "right", "right", "right", "right", "left", "left", "left", "left"])

    print()
    pnl_color = G if pnl >= 0 else R
    unreal = sum(p.get("pnl", 0) for p in state["paper_positions"].values())
    print(f"  {'─' * 72}")
    print(f"  Today     {pnl_color}{pnl:+.2f}{X}  │  Trades: {state['daily_trades']}  ({G}{len(wins)}W{X}/{R}{len(losses)}L{X})  Unrealized: {G if unreal >= 0 else R}{unreal:+.2f}{X}")
    if closed_trades:
        print(f"  All-time  {G if total_pnl >= 0 else R}{total_pnl:+.2f}{X}  │  Win rate: {win_rate:.1f}%  │  PF: {profit_factor:.2f}x")
    else:
        print(f"  {DIM}No closed trades yet — stats will appear after first close{X}")


def _print_stats_footer(state, trades):
    today_closed = []
    today_pnl = state["daily_pnl"]
    today_wins = []
    today_losses = []
    accurate_today_pnl = today_pnl
    pnl_sign = "+" if accurate_today_pnl >= 0 else ""
    pnl_color = G if accurate_today_pnl >= 0 else R

    closed = [t for t in trades if t.get("status") not in ("open", None)]
    wins = [t for t in closed if t.get("pnl", 0) > 0]
    losses = [t for t in closed if t.get("pnl", 0) <= 0]
    tp1s = [t for t in closed if t.get("status") == "closed_tp1"]
    tp2s = [t for t in closed if t.get("status") == "closed_tp2"]
    sls = [t for t in closed if t.get("status") == "closed_sl"]
    reps = [t for t in closed if t.get("status") == "closed_replaced"]

    try:
        today_str = datetime.now(CDT).strftime("%Y-%m-%d")
        today_closed = [t for t in closed if t.get("closed_at", t.get("opened_at", ""))[:10] == today_str]
        today_pnl = sum(t.get("pnl", 0) for t in today_closed)
        today_wins = [t for t in today_closed if t.get("pnl", 0) > 0]
        today_losses = [t for t in today_closed if t.get("pnl", 0) <= 0]
        accurate_today_pnl = today_pnl
        pnl_sign = "+" if accurate_today_pnl >= 0 else ""
        pnl_color = G if accurate_today_pnl >= 0 else R
    except Exception:
        pass

    total_pnl = sum(t.get("pnl", 0) for t in closed)
    win_rate = (len(wins) / len(closed) * 100) if closed else 0
    avg_win = (sum(t["pnl"] for t in wins) / len(wins)) if wins else 0
    avg_loss = (sum(t["pnl"] for t in losses) / len(losses)) if losses else 0
    profit_factor = abs(sum(t["pnl"] for t in wins) / sum(t["pnl"] for t in losses)) if losses and sum(t["pnl"] for t in losses) != 0 else 0
    best_trade = max((t.get("pnl", 0) for t in closed), default=0)
    worst_trade = min((t.get("pnl", 0) for t in closed), default=0)
    unrealized = sum(pos.get("pnl", 0) for pos in state["paper_positions"].values())

    sep = f"  {DIM}{'─' * 55}{X}"
    print(f"\n{sep}")
    print(f"  {BOLD}Today{X}      {pnl_color}{BOLD}{pnl_sign}${accurate_today_pnl:.2f}{X}  {DIM}│{X}  Trades: {W}{len(today_closed)}{X}  {DIM}({G}{len(today_wins)}W{X}{DIM}/{X}{R}{len(today_losses)}L{X}{DIM}){X}  Unrealized: {G if unrealized >= 0 else R}${unrealized:+.2f}{X}")
    print(sep)
    if closed:
        pf_color = G if profit_factor >= 1 else R
        wr_color = G if win_rate >= 50 else R
        tot_color = G if total_pnl >= 0 else R
        print(f"  {BOLD}All-time{X}   P&L: {tot_color}${total_pnl:+.2f}{X}  {DIM}│{X}  Trades: {W}{len(closed)}{X}  {DIM}({G}{len(wins)}W{X} {DIM}/{X} {R}{len(losses)}L{X}{DIM}){X}")
        print(f"  {BOLD}Breakdown{X}  {G}TP1: {len(tp1s)}{X}  {G}TP2: {len(tp2s)}{X}  {R}SL: {len(sls)}{X}  {Y}Replaced: {len(reps)}{X}")
        print(f"  {BOLD}Win rate{X}   {wr_color}{win_rate:.1f}%{X}  {DIM}│{X}  Profit factor: {pf_color}{profit_factor:.2f}{X}")
        print(f"  {BOLD}Avg win{X}    {G}${avg_win:+.2f}{X}  {DIM}│{X}  Avg loss: {R}${avg_loss:.2f}{X}")
        print(f"  {BOLD}Best{X}       {G}${best_trade:+.2f}{X}  {DIM}│{X}  Worst: {R}${worst_trade:.2f}{X}")
    else:
        print(f"  {DIM}No closed trades yet — stats will appear after first close{X}")
    print(sep)


def _get_scan_pairs():
    global _max_lev_map
    meta = get_hl_meta(_hl_info)
    pairs = []
    lev_map = {}
    for item in meta:
        coin = item.get("name")
        if not coin:
            continue
        pairs.append(coin)
        lev = item.get("maxLeverage")
        if lev is None:
            lev = item.get("maxLeverageCross")
        try:
            lev_map[coin] = int(float(lev)) if lev is not None else 20
        except Exception:
            lev_map[coin] = 20
    _max_lev_map = lev_map
    return pairs


def _setup_clients():
    global _hl_info, _exchange
    _hl_info = Info(constants.MAINNET_API_URL, skip_ws=True)
    _exchange = None
    if MODE == "live":
        if not WALLET_ADDRESS or not API_PRIVATE_KEY:
            raise ValueError("HL_WALLET and HL_BOT_KEY are required for live mode")
        wallet = eth_account.Account.from_key(API_PRIVATE_KEY)
        _exchange = HLExchange(wallet, constants.MAINNET_API_URL, account_address=WALLET_ADDRESS)


def _print_banner(balance, open_count, cycle, state):
    now = datetime.now(CDT).strftime("%H:%M:%S")
    wallet_disp = f"{WALLET_ADDRESS[:16]}...{WALLET_ADDRESS[-6:]}" if WALLET_ADDRESS else "not set"
    print()
    print(f"{C}━━━{X} {BOLD}HyperLiquid Perp Scanner Bot{X} {C}━━━{X}")
    print(f"Scan #{cycle}  [{now}]")
    print(f"Wallet:  {wallet_disp}")
    print(f"Balance: ${balance:.2f}  │  Open: {open_count}/{effective_max_positions()}")
    if state.get("paper_positions"):
        print(f"Monitoring {len(state['paper_positions'])} open paper position(s)...")


def _rank_valid_results(results):
    valid = [
        r for r in results
        if r and r.get("status") in (None, "valid")
        and not r.get("vetoed")
        and "score" in r
        and "direction" in r
        and "coin" in r
    ]
    return sorted(
        valid,
        key=lambda x: (x.get("score", 0), x.get("adx") or 0, -abs(x.get("funding", 0))),
        reverse=True,
    )


def _attempt_entries(state, trades, ranked_results, prices):
    if state.get("cap_hit"):
        return
    if state.get("balance_cache", 0) and state["balance_cache"] < effective_account_floor():
        return

    existing = set(state["paper_positions"].keys())
    slots = max(effective_max_positions() - len(existing), 0)
    if slots > 0:
        for r in ranked_results:
            coin = r["coin"]
            if slots <= 0:
                break
            if coin in existing:
                continue
            if coin in state.get("cooldowns", {}):
                continue
            open_paper_position(r, state, trades)
            state["last_entry_time"] = datetime.now(timezone.utc).isoformat()
            save_state(state)
            existing.add(coin)
            slots -= 1

    if len(state["paper_positions"]) >= effective_max_positions() and ranked_results:
        replace_coin, new_result = find_replacement_candidate(state, ranked_results, prices)
        if replace_coin and new_result:
            pos = state["paper_positions"].get(replace_coin)
            px = prices.get(replace_coin)
            if pos and px is not None and new_result["coin"] not in state["paper_positions"]:
                close_paper_position(replace_coin, pos, px, f"better setup: {new_result['coin']}", state, trades)
                open_paper_position(new_result, state, trades)
                state["last_entry_time"] = datetime.now(timezone.utc).isoformat()
                save_state(state)


def _countdown_loop(state, trades):
    last_pos_check = 0
    for remaining in range(SCAN_INTERVAL, 0, -1):
        mins = remaining // 60
        secs = remaining % 60
        line = "  \033[2mNext scan in \033[97m" + str(mins) + "m " + f"{secs:02d}" + "s\033[0m\033[2m...\033[0m   "
        print(line + "\r", end="", flush=True)
        time.sleep(1)
        seconds_elapsed = SCAN_INTERVAL - remaining + 1
        if seconds_elapsed - last_pos_check >= 60 and state["paper_positions"]:
            last_pos_check = seconds_elapsed
            try:
                quick_prices = get_hl_mark_prices(_hl_info)
                if quick_prices:
                    before = len(state["paper_positions"])
                    monitor_paper_positions(state, trades, quick_prices)
                    after = len(state["paper_positions"])
                    if after < before:
                        print(" " * 80 + "\r", end="", flush=True)
                        print(f"  {G}Position closed between scans — {before - after} slot(s) freed{X}")
            except Exception:
                pass
    print(" " * 80 + "\r", end="", flush=True)


def scan_loop():
    cycle = 0
    while True:
        cycle += 1
        daily_reset_if_needed(state)
        trades = load_trades()

        balance = get_account_balance(_hl_info)
        if balance > 0:
            state["balance_cache"] = balance
            save_state(state)
        else:
            balance = state.get("balance_cache", 0.0)

        prices = get_hl_mark_prices(_hl_info)
        if not prices:
            print("Could not fetch mark prices. Retrying next cycle.")
            _countdown_loop(state, trades)
            continue

        monitor_paper_positions(state, trades, prices)
        scan_pairs = _get_scan_pairs()
        funding_rates = get_funding_rates(_hl_info)

        _print_banner(balance, len(state["paper_positions"]), cycle, state)

        if state.get("cap_hit"):
            print(f"{Y}Daily loss cap hit. Scanner paused until next daily reset.{X}")
            send_scan_summary(len(scan_pairs), [], [], state, cycle, prices=prices, trades=trades)
            _countdown_loop(state, trades)
            continue

        if balance and balance < effective_account_floor():
            print(f"{Y}Account below floor (${effective_account_floor():.2f}). No new entries this cycle.{X}")

        print(f"Scanning {len(scan_pairs)} pairs...", end=" ", flush=True)
        started = time.time()
        results = []
        vetoed_reasons = []
        status_counts = {
            "missing_price": 0,
            "missing_indicators": 0,
            "no_direction": 0,
            "below_min_score": 0,
            "tier_reject": 0,
            "vetoed": 0,
            "valid": 0,
            "exception": 0,
            "uncategorized": 0,
        }
        exception_samples = []
        max_workers = min(16, max(4, (os.cpu_count() or 8)))
        with ThreadPoolExecutor(max_workers=max_workers) as ex:
            futures = {ex.submit(score_pair, coin, prices, funding_rates, state): coin for coin in scan_pairs}
            for fut in as_completed(futures):
                coin = futures[fut]
                try:
                    res = fut.result()
                except Exception as e:
                    status_counts["exception"] += 1
                    if len(exception_samples) < 10:
                        exception_samples.append((coin, type(e).__name__, str(e)))
                    continue
                if not res:
                    status_counts["uncategorized"] += 1
                    continue
                status = res.get("status", "uncategorized")
                if status in status_counts:
                    status_counts[status] += 1
                else:
                    status_counts["uncategorized"] += 1
                results.append(res)
                if res.get("status") == "vetoed":
                    vetoed_reasons.append(res.get("reason", "vetoed"))
        elapsed = time.time() - started
        print(f"done in {elapsed:.1f}s")

        ranked = _rank_valid_results(results)
        _attempt_entries(state, trades, ranked, prices)
        trades = load_trades()
        send_scan_summary(len(scan_pairs), results, vetoed_reasons, state, cycle, prices=prices, trades=trades, status_counts=status_counts, exception_samples=exception_samples)
        _countdown_loop(state, trades)


def main():
    global state
    print()
    print("  HyperLiquid Scanner Bot")
    print("  =======================")
    print(f"  Mode      : {MODE.upper()}")
    if MODE == "live":
        print(f"  Safe mode : {'ON' if LIVE_SAFE_MODE else 'OFF'}")
    print(f"  Interval  : {SCAN_INTERVAL}s")
    print(f"  Max pos   : {effective_max_positions()}")
    print(f"  Loss cap  : ${effective_daily_loss_cap():.2f}")
    print(f"  Floor     : ${effective_account_floor():.2f}")
    print(f"  Telegram  : {'configured' if TG_TOKEN and TG_CHAT_ID else 'not configured'}")
    print()

    if not WALLET_ADDRESS:
        print("  ERROR: HL_WALLET is missing in .env")
        return

    state = load_state()
    daily_reset_if_needed(state)

    try:
        _setup_clients()
    except Exception as e:
        print(f"  ERROR: Could not initialize Hyperliquid SDK: {e}")
        return

    tg(f"Scanner Bot started in {MODE.upper()} mode.")
    scan_loop()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n\n  Scanner stopped.")
        tg("Scanner Bot stopped.")
