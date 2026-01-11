# TakerShield Observer

**Stand-down signals for Kalshi market makers. Read-only. No keys. No trading.**

![TakerShield Observer terminal screenshot](./assets/observer_terminal.png)

---

## Read-Only by Design

- **No API keys required** — Observer never touches your Kalshi credentials
- **No trading permissions** — Cannot place, cancel, or modify orders
- **Open-source client** — Inspect every line; signals come from TakerShield brain server

This is a monitoring tool, not trading software. It shows you when conditions are dangerous—what you do with that is up to you.

---

## Requirements

- Python 3.8+
- macOS or Linux (Windows not currently supported)
- Terminal with Unicode support

---

## Quickstart (60 seconds)

```bash
# Install
pip install git+https://github.com/takershield/takershield-observer.git

# Run
takershield --token YOUR_TOKEN
```

**Adding markets:**
1. Press `a` to add
2. Paste any Kalshi market URL, e.g.:
   ```
   https://kalshi.com/markets/kxunitedcupmatch/united-cup-match/kxunitedcupmatch-26jan11swiben
   ```
3. If the event has multiple contracts, select which to observe (or `0` for all)

Check version: `pip show takershield`

Upgrade: `pip install --upgrade git+https://github.com/takershield/takershield-observer.git`

---

## What You'll See

### Market Status Table

| Column | Description |
|--------|-------------|
| **Ticker** | Kalshi market ticker |
| **Bid / Ask / Mid** | Current book prices (cents) |
| **Spread** | Ask − Bid |
| **Depth** | Contracts at top of book |
| **Signal** | SAFE / CAUTION / NO_QUOTE + trigger reason |
| **Closes** | Time until market close |
| **p99** | 99th percentile volatility (recent price moves) |

### Status Panel (top right)

- READ-ONLY indicator
- Connection status + uptime
- Updates received + heartbeat age
- **Cancels**: Count of NO_QUOTE events (would-cancel if live)
- **Avoided**: Estimated cents saved by standing down

### Latency Panel

- **Poll**: Kalshi API fetch time
- **Compute**: Risk calculation time
- **WS**: WebSocket round-trip to your terminal

### Risk Events Table

Each NO_QUOTE event logs:
- **Trigger**: What caused the signal (spread, time, volatility)
- **Age / Shielded**: How long ago, how long you were protected
- **Move (30s/2m/5m)**: Worst post-signal mid move per window (see Move Column section)

Risk Events shows the most recent 20 cancel events (rolling window). Older events age out automatically.

---

## Signals

| Signal | Meaning |
|--------|---------|
| ✅ **SAFE** | Market conditions normal. Quoting is reasonable. |
| ⚠️ **CAUTION** | Risk rising. Consider widening quotes or reducing size. |
| 🛑 **NO_QUOTE** | High adverse-selection risk. Do not quote. |

Triggers are OR-logic: any single condition fires the signal.

---

## Move Column

- Shows worst price move AFTER a NO_QUOTE signal.
- Windows: 30s / 2m / 5m from trigger time.
- ▼ means mid moved DOWN (YES side would lose).
- ▲ means mid moved UP (NO side would lose).
- Numbers are cents vs mid at trigger (t0_mid).
- Example: `▼4(4/3)` means 4¢ worst move, YES quotes would be picked off by 4¢, NO by 3¢.

---

## What This Is

- Shadow-mode risk observer.
- Shows what you avoided by standing down.
- Not trading advice.

---

## Keyboard Controls

| Key | Action |
|-----|--------|
| `a` | Add market (paste Kalshi URL or ticker) |
| `r` | Remove market |
| `d` | Demo mode (BTC 15-min markets) |
| `c` | Clear risk events |
| `q` | Quit |

---

## FAQ

**Does it place or cancel orders?**  
No. Observer is read-only. It cannot interact with Kalshi on your behalf.

**What do I need to use it?**  
A TakerShield token. That's it. No Kalshi API keys, no account linking.

**Is this trading advice?**  
No. TakerShield provides risk signals based on market microstructure. It does not recommend trades, predict outcomes, or guarantee protection. You are responsible for your own trading decisions.

---

## Troubleshooting

| Problem | Fix |
|---------|-----|
| `Invalid token` | Check token string, no extra spaces. Contact support for new token. |
| No data / empty table | Add markets with `a` key or `d` for demo. Server may be restarting. |
| Stale heartbeat (>30s) | Connection dropped. Restart observer. Check internet. |
| Markets stuck after close | Should auto-remove. Restart if persists. |

---

## Support

Email: s@takershield.com

---

## Disclaimer

TakerShield Observer provides informational risk signals only. It is not investment advice, trading advice, or a recommendation to buy or sell any contract. Past signal performance does not guarantee future results. Use at your own risk.
