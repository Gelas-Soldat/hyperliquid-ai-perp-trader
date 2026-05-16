# HyperLiquid AI Perp Trader

🚧 **Work in Progress**

AI assisted perpetual futures trading suite built for HyperLiquid.

This project is under active development and active paper testing. It is not finished, not audited, and should not be treated as a hands off live trading system.

## What It Does

This repo currently includes:

- HyperLiquid perpetual pair scanner
- Paper trading mode
- Live mode foundation
- Dynamic stop loss and take profit logic
- Market structure filters
- Trend and volatility checks
- Liquidity and quality filters
- Aggregate market sentiment layer
- Telegram alerts
- Trailing stop management files

## Current Files

| File | Purpose |
|---|---|
| `scanner_bot_v1.py` | Paper scanner |
| `scanner_bot_v2.py` | Live scanner |
| `trailing_stop_bot_v1.py` | Paper trailing stop manager |
| `trailing_stop_bot_v2.py` | Live trailing stop manager |
| `launch-dashboard.bat` | Optional Windows launcher |
| `.env.example` | Example environment variables |

## Current Status

This project is still being debugged and tuned.

Known active areas:

- More paper trading validation
- More accurate strategy scoring
- Reducing false positives in choppy markets
- Improving no-indicator and no-direction handling
- Hardening live execution safeguards
- Improving status/debug output
- Reviewing technical indicator logic against real market behavior

## Strategy Components

The scanner uses a mix of:

- EMA structure
- RSI
- Stochastic RSI
- MACD
- ADX and DI gap
- Bollinger Band width
- Candle body and wick structure
- Funding rates
- Liquidity and volume filters
- Market sentiment aggregation
- Dynamic SL / TP targets

Strategies may include:

- EMA continuation
- Stop Hunt Reversal
- Opening Range Break style logic

## Setup

Install dependencies:

```bash
pip install -r requirements.txt
```

Create your `.env` file from `.env.example`:

```bash
copy .env.example .env
```

Then edit `.env` with your own values.

Run paper scanner:

```bash
python scanner_bot_v1.py
```

Run live scanner only after you fully understand the code and have tested extensively:

```bash
python scanner_bot_v2.py
```

## Environment Variables

See `.env.example`.

Important:

Never commit `.env`.
Never commit real wallet keys.
Never commit Telegram bot tokens.

## GitHub Warning

Before pushing, confirm these are ignored:

- `.env`
- `paper_trades.json`
- `scanner_state.json`
- logs
- cache files

## Disclaimer

This project is experimental software.

Perpetual futures trading is high risk. You can lose money quickly, especially in crypto markets. This code is provided for research, learning, and paper trading. No profitability is promised or implied.

Use at your own risk.
