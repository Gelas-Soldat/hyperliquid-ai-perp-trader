# HyperLiquid AI Perp Trader

AI assisted perpetual futures trading suite built for HyperLiquid.

This project combines:

• Multi strategy trade scanning  
• Dynamic stop loss and take profit logic  
• Adaptive trend and volatility analysis  
• Aggregate crypto market sentiment  
• Paper and live trading modes  
• Position management with trailing logic  
• Telegram alerts  
• Dashboard and monitoring tools

This repository is actively being developed and tested.

## Current Status

⚠️ Work in Progress

This system is under active development and strategy tuning.

Paper trading and validation come first.

Do not assume profitability or production readiness.

Current goals:

✅ Improve signal quality  
✅ Reduce choppy market entries  
✅ Improve trend detection  
✅ Improve liquidity filtering  
✅ Validate performance in paper mode  
⬜ Expand live execution safeguards  
⬜ Improve market regime detection  
⬜ Portfolio level risk management

---

## Features

### Scanner

Scans HyperLiquid perpetual pairs using:

EMA structure

RSI

ADX

MACD

ATR

Bollinger Bands

Funding rates

Volatility filters

Trend quality analysis

Stop Hunt Reversal

Opening Range Break

Dynamic setup classification

---

### Dynamic Risk Logic

Stop loss adjusts based on:

market volatility

trend quality

setup classification

position structure

Take profit levels adapt to market conditions.

Trades can be skipped entirely during poor structure.

---

### Market Sentiment

Aggregate sentiment currently uses:

Internet Panic Index

CoinMind AI Fear and Greed

FearGreedMeter

CoinMarketCap Fear and Greed

Combined sentiment influences filtering and trade selection.

---

## Modes

scanner_bot_v1.py

Paper mode

Designed for testing and validation.

scanner_bot_v2.py

Live mode

Uses stricter filtering and safeguards.

trailing_bot_v1.py

Paper trade management

trailing_bot_v2.py

Live trade management

---

## Setup

Clone repository:

```bash
git clone https://github.com/Gelas-Soldat/hyperliquid-ai-perp-trader.git

cd hyperliquid-ai-perp-trader
