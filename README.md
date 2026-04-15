# 🔍 Solana Smart Money Tracker — Setup Guide

## What This Bot Does

1. **Discovers** up to 10,000 active Solana wallets starting from seed addresses
2. **Analyzes** each wallet's trade history — win rate, PnL, consistency
3. **Ranks** wallets by a composite profitability score
4. **Monitors** top wallets live and alerts you when they make a new trade
5. **Logs** all signals to CSV so you can act (copy the trade manually or automate)

---

## Quick Start

### 1. Install Dependencies
```bash
pip install requests pandas
```

### 2. Get a Free Helius API Key (takes 2 minutes)
- Go to → https://helius.dev
- Sign up (free tier = 100,000 requests/day — enough for ~200 wallets/hour)
- Copy your API key

### 3. Configure the Bot
Open `solana_wallet_analyzer.py` and edit the top section:

```python
HELIUS_API_KEY = "your-key-here"   # ← Paste your Helius key

SEED_WALLETS = [
    "WALLET_ADDRESS_1",             # ← Add known active trader addresses
    "WALLET_ADDRESS_2",
    # Find them at: https://solscan.io/leaderboard
    # or https://birdeye.so/find-gems
]

MAX_WALLETS = 500                   # Start small, scale up
MIN_WIN_RATE = 0.55                 # 55% win rate minimum
MIN_TRADES = 10                     # At least 10 trades
```

### 4. Run It
```bash
python solana_wallet_analyzer.py
```

---

## Finding Good Seed Wallets

The quality of seed wallets determines the quality of results.

| Source | URL | What to look for |
|--------|-----|-----------------|
| Solscan Leaderboard | https://solscan.io/leaderboard | Top traders by volume |
| Birdeye | https://birdeye.so/find-gems | Active DeFi wallets |
| Step Finance | https://app.step.finance | Portfolio trackers |
| Cielo Finance | https://app.cielo.finance | Smart money tracker |

---

## Output Files

| File | Description |
|------|-------------|
| `wallet_analysis_results.csv` | All wallets ranked by consistency score |
| `copy_trade_signals.csv` | Live trade alerts with Solscan TX links |
| `analyzer.log` | Full run log for debugging |

---

## Scaling to 10,000 Wallets

| Wallets | Time Estimate | API Calls | Notes |
|---------|--------------|-----------|-------|
| 100 | ~5 min | ~5,000 | Great for testing |
| 500 | ~25 min | ~25,000 | Good daily run |
| 2,000 | ~2 hours | ~100,000 | Uses full free Helius quota |
| 10,000 | ~10 hours | ~500,000 | Needs Helius paid tier ($9/mo) |

Set `MAX_WALLETS = 10000` and run overnight.

---

## How the Consistency Score Works

```
Score = (win_rate × 40) + (avg_pnl_per_trade / 10, max 30) + (total_pnl / 100, max 30)
```

- **Max score = 100**
- Wallets above 60 are strong candidates
- Prioritizes consistent winners over lucky one-off big trades

---

## Copy-Trading: Manual vs Automated

### Manual (this bot)
- Bot alerts you when a top wallet trades
- You see the TX on Solscan
- You manually replicate on Jupiter/Raydium

### Automated (next step)
To automate execution, integrate:
- **Jupiter SDK** for swaps → https://station.jup.ag/docs
- **Solana Web3.js** for signing transactions
- A funded wallet with SOL for gas + capital

⚠️ **Risk Warning**: Copy-trading carries real financial risk. Always test with small amounts first. Past performance doesn't guarantee future results.

---

## Troubleshooting

| Problem | Fix |
|---------|-----|
| `Rate limit` errors | Add `time.sleep(0.5)` or upgrade Helius tier |
| `No trades found` | Add more seed wallets or lower `MIN_TRADES` |
| `No profitable wallets` | Lower `MIN_WIN_RATE` to 0.5 |
| Slow performance | Reduce `TXNS_PER_WALLET` to 20 |

---

## Architecture Overview

```
SEED WALLETS
    ↓
WalletDiscovery   ← crawls transaction graph to find new wallets
    ↓
WalletAnalyzer    ← fetches txns, parses swaps, calculates PnL
    ↓
Filter & Rank     ← win rate ≥ 55%, min 10 trades, positive PnL
    ↓
Reporter          ← saves CSV, prints leaderboard
    ↓
CopyTradeMonitor  ← polls top wallets every 15s for new trades
    ↓
copy_trade_signals.csv  ← your trading alerts
```
