# TakerShield AI Observer

Real-time risk monitoring client for Kalshi markets.

## Installation

```bash
pip install git+https://github.com/takershield/observer.git
```

## Usage

```bash
takershield --token YOUR_TOKEN
```

Or with custom server:

```bash
takershield --url wss://api.takershield.com/ws --token YOUR_TOKEN
```

## Features

- 📊 Live risk scores and regime status (SAFE / CAUTION / NO_QUOTE)
- 🚨 "Would have canceled" alerts with trigger reasons
- ⏱️ Latency monitoring (poll, compute, websocket)
- 📈 Per-market p99 volatility stats

## Requirements

- Python 3.8+
- websockets
- rich

## License

MIT License - see LICENSE
