#!/usr/bin/env python3
"""
TakerShield Observer Client

Real-time risk monitoring CLI that connects to the brain server.

Usage:
    python observer_client.py --url wss://your-server.com/ws --token YOUR_TOKEN

Features:
- Live risk scores and regime status
- "Would have canceled" alerts
- Latency monitoring
- Adverse move tracking
"""

import argparse
import asyncio
import json
import sys
import time
from datetime import datetime
from typing import Optional, Dict, Any

try:
    import websockets
    import ssl
    from rich.console import Console
    from rich.live import Live
    from rich.table import Table
    from rich.panel import Panel
    from rich.layout import Layout
    from rich.text import Text
    from rich import box
except ImportError:
    print("Install dependencies: pip install websockets rich")
    sys.exit(1)

# SSL context for connecting
SSL_CONTEXT = ssl.create_default_context()
SSL_CONTEXT.check_hostname = False
SSL_CONTEXT.verify_mode = ssl.CERT_NONE


console = Console()


class ObserverState:
    """Track observer state."""
    
    def __init__(self):
        self.connected = False
        self.server_url = ""
        self.last_heartbeat: Optional[float] = None
        
        # Market data
        self.markets: Dict[str, dict] = {}
        
        # Events
        self.would_cancel_events: list[dict] = []
        self.max_events = 20  # Keep last N events
        
        # Stats
        self.updates_received = 0
        self.connect_time: Optional[float] = None
        
        # Latency tracking
        self.last_poll_latency = 0.0
        self.last_compute_latency = 0.0
        self.last_ws_latency = 0.0
    
    def update_market(self, data: dict):
        ticker = data.get("ticker")
        if ticker:
            self.markets[ticker] = data
            self.updates_received += 1
            self.last_poll_latency = data.get("poll_latency_ms", 0)
            self.last_compute_latency = data.get("compute_latency_ms", 0)
    
    def add_would_cancel(self, data: dict):
        self.would_cancel_events.append(data)
        if len(self.would_cancel_events) > self.max_events:
            self.would_cancel_events.pop(0)
    
    def update_heartbeat(self, data: dict):
        self.last_heartbeat = time.time()


state = ObserverState()


def get_regime_style(regime: str) -> str:
    """Get color style for regime."""
    if regime == "SAFE":
        return "bold green"
    elif regime == "CAUTION":
        return "bold yellow"
    else:  # NO_QUOTE
        return "bold red"


def get_risk_style(score: float) -> str:
    """Get color style for risk score."""
    if score < 0.35:
        return "green"
    elif score < 0.55:
        return "yellow"
    else:
        return "red"


def format_time(seconds: float) -> str:
    """Format seconds as mm:ss."""
    mins = int(seconds // 60)
    secs = int(seconds % 60)
    return f"{mins}:{secs:02d}"


def build_market_table() -> Table:
    """Build market status table."""
    table = Table(
        title="📊 Market Status",
        box=box.ROUNDED,
        show_header=True,
        header_style="bold cyan"
    )
    
    table.add_column("Ticker", style="dim", width=28)
    table.add_column("Bid", justify="right", width=6)
    table.add_column("Ask", justify="right", width=6)
    table.add_column("Mid", justify="right", width=7)
    table.add_column("Spread", justify="right", width=6)
    table.add_column("Risk", justify="right", width=7)
    table.add_column("Regime", justify="center", width=10)
    table.add_column("TTC", justify="right", width=7)
    table.add_column("p99", justify="right", width=6)
    
    for ticker, data in state.markets.items():
        regime = data.get("regime", "?")
        risk_score = data.get("risk_score", 0)
        
        table.add_row(
            ticker[-20:],  # Truncate ticker
            str(data.get("bid", "-")),
            str(data.get("ask", "-")),
            f"{data.get('mid', 0):.1f}" if data.get("mid") else "-",
            str(data.get("spread", "-")),
            Text(f"{risk_score:.2f}", style=get_risk_style(risk_score)),
            Text(regime, style=get_regime_style(regime)),
            format_time(data.get("time_to_close_s", 0)),
            f"{data.get('p99_move', 0):.1f}",
        )
    
    if not state.markets:
        table.add_row("Waiting for data...", "-", "-", "-", "-", "-", "-", "-", "-")
    
    return table


def build_events_table() -> Table:
    """Build would-cancel events table."""
    table = Table(
        title="🚨 Would-Cancel Events",
        box=box.ROUNDED,
        show_header=True,
        header_style="bold red"
    )
    
    table.add_column("Time", width=10)
    table.add_column("Ticker", width=20)
    table.add_column("Risk", justify="right", width=6)
    table.add_column("Mid", justify="right", width=6)
    table.add_column("Triggers", width=30)
    
    for event in reversed(state.would_cancel_events[-10:]):
        ts = event.get("timestamp_ms", 0) / 1000
        time_str = datetime.fromtimestamp(ts).strftime("%H:%M:%S")
        
        triggers = ", ".join(event.get("trigger_reasons", []))
        
        table.add_row(
            time_str,
            event.get("ticker", "?")[-20:],
            f"{event.get('risk_score', 0):.2f}",
            f"{event.get('mid_at_trigger', 0):.1f}",
            triggers[:30]
        )
    
    if not state.would_cancel_events:
        table.add_row("-", "No events yet", "-", "-", "-")
    
    return table


def build_stats_panel() -> Panel:
    """Build stats panel."""
    uptime = time.time() - state.connect_time if state.connect_time else 0
    
    heartbeat_ago = ""
    if state.last_heartbeat:
        ago = time.time() - state.last_heartbeat
        heartbeat_ago = f"{ago:.1f}s ago"
    
    content = Text()
    content.append("🔗 ", style="bold")
    content.append("Connected" if state.connected else "Disconnected", 
                   style="green" if state.connected else "red")
    content.append(f"\n⏱️  Uptime: {format_time(uptime)}")
    content.append(f"\n📨 Updates: {state.updates_received}")
    content.append(f"\n💓 Heartbeat: {heartbeat_ago}")
    
    return Panel(content, title="Connection", border_style="blue")


def build_latency_panel() -> Panel:
    """Build latency panel."""
    content = Text()
    
    # Color code latencies
    poll_style = "green" if state.last_poll_latency < 100 else "yellow" if state.last_poll_latency < 200 else "red"
    compute_style = "green" if state.last_compute_latency < 10 else "yellow" if state.last_compute_latency < 50 else "red"
    ws_style = "green" if state.last_ws_latency < 50 else "yellow" if state.last_ws_latency < 100 else "red"
    
    content.append("📡 Poll: ", style="dim")
    content.append(f"{state.last_poll_latency:.0f}ms\n", style=poll_style)
    content.append("🧠 Compute: ", style="dim")
    content.append(f"{state.last_compute_latency:.1f}ms\n", style=compute_style)
    content.append("🌐 WS: ", style="dim")
    content.append(f"{state.last_ws_latency:.0f}ms", style=ws_style)
    
    return Panel(content, title="Latency", border_style="magenta")


def build_layout() -> Layout:
    """Build the full layout."""
    layout = Layout()
    
    layout.split_column(
        Layout(name="header", size=3),
        Layout(name="main"),
        Layout(name="footer", size=3)
    )
    
    layout["main"].split_row(
        Layout(name="markets", ratio=3),
        Layout(name="sidebar", ratio=1)
    )
    
    layout["sidebar"].split_column(
        Layout(name="stats", size=8),
        Layout(name="latency", size=7),
        Layout(name="events")
    )
    
    # Header
    header = Panel(
        Text("🛡️  TakerShield Observer", justify="center", style="bold white"),
        border_style="cyan"
    )
    layout["header"].update(header)
    
    # Markets table
    layout["markets"].update(build_market_table())
    
    # Sidebar
    layout["stats"].update(build_stats_panel())
    layout["latency"].update(build_latency_panel())
    layout["events"].update(build_events_table())
    
    # Footer
    footer_text = f"Server: {state.server_url}  |  Press Ctrl+C to exit"
    layout["footer"].update(Panel(Text(footer_text, justify="center", style="dim")))
    
    return layout


async def connect_and_listen(url: str, token: str):
    """Connect to brain server and listen for updates."""
    full_url = f"{url}?token={token}"
    state.server_url = url
    
    console.print(f"🔌 Connecting to {url}...", style="yellow")
    
    while True:
        try:
            async with websockets.connect(full_url, ssl=SSL_CONTEXT) as ws:
                state.connected = True
                state.connect_time = time.time()
                console.print("✅ Connected!", style="green")
                
                while True:
                    msg = await ws.recv()
                    recv_time = time.time() * 1000
                    
                    data = json.loads(msg)
                    msg_type = data.get("type")
                    payload = data.get("data", {})
                    
                    if msg_type == "market_update":
                        # Calculate WS latency
                        msg_ts = payload.get("timestamp_ms", 0)
                        if msg_ts:
                            state.last_ws_latency = recv_time - msg_ts
                        state.update_market(payload)
                    
                    elif msg_type == "would_cancel":
                        state.add_would_cancel(payload)
                    
                    elif msg_type == "heartbeat":
                        state.update_heartbeat(payload)
                        
        except websockets.exceptions.ConnectionClosed:
            state.connected = False
            console.print("❌ Connection lost, reconnecting in 3s...", style="red")
            await asyncio.sleep(3)
            
        except Exception as e:
            state.connected = False
            console.print(f"❌ Error: {e}, reconnecting in 5s...", style="red")
            await asyncio.sleep(5)


async def run_display():
    """Run the live display."""
    with Live(build_layout(), refresh_per_second=4, console=console) as live:
        while True:
            live.update(build_layout())
            await asyncio.sleep(0.25)


async def run_observer(url: str, token: str):
    """Main entry point."""
    # Run both tasks
    await asyncio.gather(
        connect_and_listen(url, token),
        run_display()
    )


def parse_args():
    parser = argparse.ArgumentParser(description="TakerShield AI Observer")
    parser.add_argument(
        "--url", "-u",
        default="wss://api.takershield.com/ws",
        help="Brain server WebSocket URL (default: wss://api.takershield.com/ws)"
    )
    parser.add_argument(
        "--token", "-t",
        required=True,
        help="Authentication token (required)"
    )
    return parser.parse_args()


def main():
    """Entry point for console script."""
    args = parse_args()
    
    try:
        asyncio.run(run_observer(args.url, args.token))
    except KeyboardInterrupt:
        console.print("\n👋 Goodbye!", style="bold")


if __name__ == "__main__":
    main()
