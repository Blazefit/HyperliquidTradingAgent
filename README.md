# Hyperliquid Trading Agent

Manual trade execution agent for Hyperliquid with bracket orders, stop-loss management, and 24/7 self-healing.

## What it does

- Places entry limit orders with automatic bracket (SL + multiple TPs)
- Watches for fills and auto-adjusts stop-loss after each TP
- Moves SL to breakeven/trail level after configurable target threshold
- Hourly health checks verify all orders are correct and self-heals if anything drifts
- Resumes from any state — restart the script and it picks up where it left off
- Runs 24/7 with retry logic for network errors

## Setup

1. Copy `.env.example` to `.env` and fill in your Hyperliquid credentials
2. Install dependencies: `pip install -r requirements.txt`
3. Run a trade setup:

```bash
# CLI mode
python manual_setup.py --asset BTC --direction long --entry 75507 \
  --sl 73997 --tp1 76615,25 --tp2 78384,35 --tp3 79644,20 \
  --trail-sl 78384 --trail-after 2

# Interactive mode
python manual_setup.py
```

## Resuming

If the script stops (crash, terminal close, Ctrl+C), just run the same command again. It will:
- Detect existing entry orders and resume watching
- Detect open positions and re-place the bracket if needed
- Detect existing TP/SL orders and resume watching fills

## Health Checks

Every hour the agent verifies:
- Stop-loss exists and matches expected price + size
- All unfilled TP orders exist with correct sizes
- Position size matches order sizes
- Auto-replaces any missing or drifted orders

## CLI Options

| Flag | Description | Default |
|------|-------------|---------|
| `--asset` | Trading pair (BTC, ETH, SOL, HYPE) | BTC |
| `--direction` | long or short | long |
| `--entry` | Entry limit price | required |
| `--sl` | Stop-loss price | required |
| `--tp1`..`--tp4` | Target as price,pct (e.g. 76615,25) | - |
| `--trail-sl` | Move SL to this price after threshold TPs fill | - |
| `--trail-after` | Number of TPs before moving trail SL | 2 |
| `--size` | Override position size (auto-calculated if omitted) | auto |
| `--leverage` | Leverage multiplier | 3 |
| `--equity-pct` | Max equity % per trade | 10 |

## Environment Variables

| Variable | Description |
|----------|-------------|
| `HL_PRIVATE_KEY` | Hyperliquid wallet private key (0x...) |
| `HL_WALLET_ADDRESS` | Hyperliquid wallet address (0x...) |
