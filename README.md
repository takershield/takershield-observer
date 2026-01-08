# TakerShield AI Observer

Real-time risk monitoring client for Kalshi markets.

## ⚠️ Shadow Mode Only

**This is a monitoring tool, not a trading system.**

- ✅ Shows when you WOULD have been protected (risk avoidance)
- ✅ Tracks adverse moves after NO_QUOTE signals
- ✅ Calculates hypothetical savings
- ❌ Does NOT execute trades
- ❌ Does NOT cancel your orders
- ❌ Does NOT predict market direction

This tool helps you understand market risk in real-time. It is NOT alpha generation.

## Installation

```bash
pip install git+https://github.com/takershield/observer.git
```

## Usage

```bash
takershield --token YOUR_TOKEN
```

With position size and quote side (for savings calculation):

```bash
takershield --token YOUR_TOKEN --size 100 --side yes
```

Options:
- `--size` - Position size in contracts (default: 100)
- `--side` - Quote side: `yes`, `no`, `both`, or `unknown` (default: unknown)

## Keyboard Controls

| Key | Action |
|-----|--------|
| `a` | Add market (paste Kalshi URL or ticker) |
| `r` | Remove market |
| `b` | Browse available markets by series |
| `d` | Demo mode (load latest BTC 15m) |
| `l` | List watched tickers |
| `c` | Clear events |
| `q` | Quit |

## What the Signals Mean

| Signal | Meaning | Action |
|--------|---------|--------|
| **SAFE** | Low risk (score < 0.35) | OK to quote normally |
| **CAUTION** | Medium risk (0.35-0.55) | Consider widening spreads |
| **NO_QUOTE** | High risk (≥ 0.55 or trigger) | Would cancel all quotes |

## Triggers

- `time_to_event` - Less than 5 min to market close
- `spread_blowout` - Spread ≥ 8¢
- `high_volatility` - p99 move ≥ 6¢
- `ml_risk` - ML model score ≥ 0.55 (crypto only)

## ML Coverage

**ML risk scoring is crypto-specialist in v1:**
- ✅ Enabled for: KXBTC*, KXETH* (Bitcoin & Ethereum markets)
- ❌ Disabled for: Sports, politics, other markets

For non-crypto markets, baseline triggers are still active (spread, volatility, time). These rules are market-agnostic and provide protection without ML.

Risk column shows `-- N/A --` when ML is not applicable to that market.

## Savings Calculation

Savings are shown only when:
1. `--side` is specified (yes/no/both)
2. Would-fill condition is met (touch crossed your hypothetical quote)

If side is unknown, we show adverse move in cents only (no $ claims).

## Requirements

- Python 3.8+
- websockets
- rich

## License

MIT License - see LICENSE

## Support

Contact: s@takershield.com
