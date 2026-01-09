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
import select
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
    from rich.prompt import Prompt
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
        
        # Websocket reference for commands
        self.ws: Optional[Any] = None
        
        # Status message
        self.status_msg = ""
        self.status_time: Optional[float] = None
        self.status_duration = 5
        
        # Input mode - pause display
        self.input_mode = False
        self.live = None  # Live display reference
        
        # Available markets from browse
        self.available_markets: list = []
        self.available_markets_info: Optional[list] = None
        
        # Search results
        self.search_results: list = []
        
        # Config (set from CLI args)
        self.position_size = 100  # Contracts per trade (default)
        self.quote_side = "unknown"  # yes, no, both, unknown
        
        # Event tracking (from server)
        self.active_events: Dict[str, dict] = {}  # event_id -> EventRecord
        
        # Regime transition tracking
        self.last_regime: Dict[str, str] = {}  # ticker -> last regime
        self.cleared_at: Dict[str, float] = {}  # ticker -> timestamp when cleared from NO_QUOTE
    
    def set_status(self, msg: str, duration: float = 5):
        self.status_msg = msg
        self.status_time = time.time()
        self.status_duration = duration
    
    def get_status(self) -> str:
        if self.status_time and time.time() - self.status_time < self.status_duration:
            return self.status_msg
        return ""
    
    def update_market(self, data: dict):
        ticker = data.get("ticker")
        if ticker:
            # Track regime transitions
            new_regime = data.get("regime", "")
            old_regime = self.last_regime.get(ticker, "")
            
            # Detect NO_QUOTE → SAFE transition (cleared)
            if old_regime == "NO_QUOTE" and new_regime in ("SAFE", "CAUTION"):
                self.cleared_at[ticker] = time.time()
            
            self.last_regime[ticker] = new_regime
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
    """Format seconds as time string."""
    if seconds < 0:
        return "[red]EXPIRED[/red]"
    if seconds > 86400 * 7:  # > 1 week
        days = int(seconds // 86400)
        return f"[dim]{days}d[/dim]"
    if seconds > 86400:  # > 1 day
        days = int(seconds // 86400)
        hours = int((seconds % 86400) // 3600)
        return f"{days}d {hours}h"
    if seconds > 3600:  # > 1 hour
        hours = int(seconds // 3600)
        mins = int((seconds % 3600) // 60)
        return f"{hours}h {mins}m"
    mins = int(seconds // 60)
    secs = int(seconds % 60)
    return f"{mins}:{secs:02d}"


def format_time_with_type(seconds: float, time_type: str) -> str:
    """Format time with indicator for type (ends vs closes)."""
    # Now that we pick the earlier of close_time and expected_expiration_time,
    # we're always showing the most useful time. No need for ~ prefix.
    return format_time(seconds)


def build_market_table() -> Table:
    """Build market status table."""
    table = Table(
        title="📊 Market Status",
        box=box.ROUNDED,
        show_header=True,
        header_style="bold cyan",
        expand=True
    )
    
    table.add_column("Ticker", style="dim", width=28)
    table.add_column("Bid", justify="right", width=6)
    table.add_column("Ask", justify="right", width=6)
    table.add_column("Mid", justify="right", width=7)
    table.add_column("Spread", justify="right", width=6)
    table.add_column("Depth", justify="right", width=8)
    table.add_column("Signal", justify="center", width=18)
    table.add_column("Closes", justify="right", width=10)
    table.add_column("p99", justify="right", width=5)
    
    for ticker, data in state.markets.items():
        regime = data.get("regime", "?")
        trigger_reasons = data.get("trigger_reasons", [])
        depth = data.get("depth", 0)
        
        # Format depth with color based on level
        if depth and depth >= 1000:
            depth_str = f"[green]{depth:,}[/green]"
        elif depth and depth >= 300:
            depth_str = f"[yellow]{depth:,}[/yellow]"
        elif depth:
            depth_str = f"[red]{depth:,}[/red]"
        else:
            depth_str = "[dim]-[/dim]"
        
        # Signal with trigger reason for NO_QUOTE or CAUTION
        caution_reasons = data.get("caution_reasons", [])
        
        if regime == "NO_QUOTE" and trigger_reasons:
            # Show first trigger reason
            reason = trigger_reasons[0]
            if reason == "time_to_event":
                signal_str = "[red]NO_QUOTE[/red] [dim](ttc)[/dim]"
            elif reason == "spread_blowout":
                signal_str = "[red]NO_QUOTE[/red] [dim](sprd)[/dim]"
            elif reason == "high_volatility":
                signal_str = "[red]NO_QUOTE[/red] [dim](p99)[/dim]"
            elif reason == "ttc_spread":
                signal_str = "[red]NO_QUOTE[/red] [dim](ttc+sprd)[/dim]"
            elif reason == "vol_spread":
                signal_str = "[red]NO_QUOTE[/red] [dim](p99+sprd)[/dim]"
            elif reason == "ml_risk":
                signal_str = "[red]NO_QUOTE[/red] [dim](ml)[/dim]"
            else:
                signal_str = Text(regime, style=get_regime_style(regime))
        elif regime == "CAUTION" and caution_reasons:
            # Show first caution reason
            reason = caution_reasons[0]
            reason_labels = {
                "spread_elevated": "sprd↑",
                "spread_widening": "sprd⇡",
                "volatility_rising": "vol↑",
                "depth_dropping": "depth↓",
                "time_approaching": "ttc↓",
            }
            label = reason_labels.get(reason, reason[:6])
            signal_str = f"[yellow]CAUTION[/yellow] [dim]({label})[/dim]"
        elif regime == "SAFE":
            # Check if recently cleared from NO_QUOTE
            cleared_time = state.cleared_at.get(ticker, 0)
            if time.time() - cleared_time < 5:
                signal_str = "[bold green]SAFE[/bold green] [cyan](cleared)[/cyan]"
            else:
                signal_str = Text(regime, style=get_regime_style(regime))
        else:
            signal_str = Text(regime, style=get_regime_style(regime))
        
        table.add_row(
            ticker[-28:],  # Truncate ticker
            str(data.get("bid", "-")),
            str(data.get("ask", "-")),
            f"{data.get('mid', 0):.1f}" if data.get("mid") else "-",
            str(data.get("spread", "-")),
            depth_str,
            signal_str,
            format_time_with_type(data.get("time_to_close_s", 0), data.get("time_type", "closes")),
            f"{data.get('p99_move', 0):.1f}",
        )
    
    if not state.markets:
        table.add_row("Waiting for data...", "-", "-", "-", "-", "-", "-", "-", "-")
    
    return table


def build_events_table() -> Table:
    """Build would-cancel events table with savings tracking."""
    table = Table(
        title="🚨 Risk Events",
        box=box.ROUNDED,
        show_header=True,
        header_style="bold red",
        expand=True
    )
    
    table.add_column("Ticker", width=26)
    table.add_column("Trigger", width=12)
    table.add_column("Action", justify="center", width=8)
    table.add_column("Age", justify="right", width=6)
    table.add_column("Protected", justify="right", width=8)
    table.add_column("Move (30s/2m/5m)", justify="right", width=16)
    
    now_ms = int(time.time() * 1000)
    now_sec = time.time()
    
    # Show active events from server
    for event_id, event in list(state.active_events.items())[-10:]:
        full_ticker = event.get("ticker", "?")
        ticker = full_ticker[-26:]
        triggers = ", ".join(event.get("trigger_reasons", []))[:12]
        
        # Get adverse moves (use max of both sides since we don't know direction)
        adv_30s = max(event.get("adverse_yes_30s", 0), event.get("adverse_no_30s", 0))
        adv_2m = max(event.get("adverse_yes_2m", 0), event.get("adverse_no_2m", 0))
        adv_5m = max(event.get("adverse_yes_5m", 0), event.get("adverse_no_5m", 0))
        
        # Calculate elapsed time (Age)
        t0_ts = event.get("t0_ts", now_ms)
        t0_sec = t0_ts / 1000
        elapsed_sec = (now_ms - t0_ts) / 1000
        tracking_complete = event.get("tracking_complete", False)
        
        if tracking_complete:
            age_str = "[green]done[/green]"
        elif elapsed_sec < 60:
            age_str = f"{int(elapsed_sec)}s"
        elif elapsed_sec < 300:
            mins = int(elapsed_sec // 60)
            secs = int(elapsed_sec % 60)
            age_str = f"{mins}m{secs:02d}s"
        else:
            age_str = "[green]5m+[/green]"
        
        # Calculate duration (Protected) - how long they were protected
        cleared_time = state.cleared_at.get(full_ticker, 0)
        current_regime = state.last_regime.get(full_ticker, "")
        
        if current_regime == "NO_QUOTE":
            # Still in NO_QUOTE - show ongoing duration
            duration_sec = now_sec - t0_sec
            if duration_sec < 60:
                duration_str = f"[yellow]{int(duration_sec)}s[/yellow]"
            else:
                mins = int(duration_sec // 60)
                secs = int(duration_sec % 60)
                duration_str = f"[yellow]{mins}m{secs:02d}s[/yellow]"
        elif cleared_time > t0_sec:
            # Cleared - show final duration
            duration_sec = cleared_time - t0_sec
            if duration_sec < 60:
                duration_str = f"[green]{int(duration_sec)}s[/green]"
            else:
                mins = int(duration_sec // 60)
                secs = int(duration_sec % 60)
                duration_str = f"[green]{mins}m{secs:02d}s[/green]"
        else:
            duration_str = "[dim]-[/dim]"
        
        # Action - always CANCEL in shadow mode
        action_str = "[red bold]CANCEL[/red bold]"
        
        # Move display - color code by severity
        def color_move(m):
            if m >= 10:
                return f"[red]{m:.0f}¢[/red]"
            elif m >= 5:
                return f"[yellow]{m:.0f}¢[/yellow]"
            elif m > 0:
                return f"[green]{m:.0f}¢[/green]"
            else:
                return "[dim]0¢[/dim]"
        
        move_str = f"{color_move(adv_30s)}/{color_move(adv_2m)}/{color_move(adv_5m)}"
        
        table.add_row(ticker, triggers, action_str, age_str, duration_str, move_str)
    
    if not state.active_events:
        # Fall back to legacy events
        for event in reversed(state.would_cancel_events[-5:]):
            triggers = ", ".join(event.get("trigger_reasons", []))
            table.add_row(
                event.get("ticker", "?")[-26:],
                triggers[:12],
                "[red bold]CANCEL[/red bold]",
                "-",
                "-",
                "-"
            )
        if not state.would_cancel_events:
            table.add_row("No events yet", "-", "-", "-", "-", "-")
    
    return table


def build_stats_panel() -> Panel:
    """Build stats panel."""
    uptime = time.time() - state.connect_time if state.connect_time else 0
    
    heartbeat_ago = ""
    data_stale = False
    if state.last_heartbeat:
        ago = time.time() - state.last_heartbeat
        heartbeat_ago = f"{ago:.1f}s ago"
        if ago > 15:
            data_stale = True
    
    # Count cancels and total adverse move avoided
    cancel_count = len(state.active_events) + len(state.would_cancel_events)
    total_adverse_cents = 0.0
    for event in state.active_events.values():
        adv_5m = max(event.get("adverse_yes_5m", 0), event.get("adverse_no_5m", 0))
        total_adverse_cents += adv_5m
    
    content = Text()
    
    # Shadow mode label
    content.append("⚠️  SHADOW MODE\n", style="yellow bold")
    
    # Data stale warning
    if data_stale:
        content.append("⚠️  DATA STALE\n", style="red bold")
    
    content.append("🔗 ", style="bold")
    content.append("Connected" if state.connected else "Disconnected", 
                   style="green" if state.connected else "red")
    content.append(f"\n⏱️  Uptime: {format_time(uptime)}")
    content.append(f"\n📨 Updates: {state.updates_received}")
    content.append(f"\n💓 Heartbeat: {heartbeat_ago}")
    
    # Show cancel stats
    if cancel_count > 0:
        content.append(f"\n\n🚨 Cancels: ")
        content.append(f"{cancel_count}", style="red bold")
        content.append(f"\n💰 Avoided: ")
        content.append(f"{total_adverse_cents:.0f}¢", style="green bold")
    
    return Panel(content, title="Status", border_style="blue")


def build_latency_panel() -> Panel:
    """Build latency panel."""
    content = Text()
    
    # Color code latencies
    poll_style = "green" if state.last_poll_latency < 100 else "yellow" if state.last_poll_latency < 200 else "red"
    compute_style = "green" if state.last_compute_latency < 10 else "yellow" if state.last_compute_latency < 50 else "red"
    ws_latency = abs(state.last_ws_latency)  # Absolute value due to clock skew
    ws_style = "green" if ws_latency < 50 else "yellow" if ws_latency < 100 else "red"
    
    content.append("📡 Poll: ", style="dim")
    content.append(f"{state.last_poll_latency:.0f}ms\n", style=poll_style)
    content.append("🧠 Compute: ", style="dim")
    content.append(f"{state.last_compute_latency:.1f}ms\n", style=compute_style)
    content.append("🌐 WS: ", style="dim")
    content.append(f"{ws_latency:.0f}ms", style=ws_style)
    
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
        Layout(name="left", ratio=3),
        Layout(name="sidebar", ratio=1)
    )
    
    layout["left"].split_column(
        Layout(name="markets", size=12),
        Layout(name="events")
    )
    
    layout["sidebar"].split_column(
        Layout(name="stats", size=11),
        Layout(name="latency", size=7),
    )
    
    # Header
    header = Panel(
        Text("🛡️  TakerShield Observer [SHADOW MODE]", justify="center", style="bold white"),
        border_style="cyan"
    )
    layout["header"].update(header)
    
    # Left side - markets and events
    layout["markets"].update(build_market_table())
    layout["events"].update(build_events_table())
    
    # Sidebar
    layout["stats"].update(build_stats_panel())
    layout["latency"].update(build_latency_panel())
    
    # Footer
    footer_text = "[a]dd  [r]emove  [b]rowse  [d]emo  [l]ist  [c]lear  [q]uit"
    status = state.get_status()
    if status:
        footer_text = f"{status}  |  {footer_text}"
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
                state.ws = ws
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
                        
                        # Remove if expired
                        ttc = payload.get("time_to_close_s", 0)
                        if ttc < 0:
                            ticker = payload.get("ticker")
                            state.markets.pop(ticker, None)
                        else:
                            state.update_market(payload)
                    
                    elif msg_type == "would_cancel":
                        state.add_would_cancel(payload)
                    
                    elif msg_type == "event_update":
                        # Update event tracking from server
                        event_id = payload.get("event_id")
                        if event_id:
                            state.active_events[event_id] = payload
                    
                    elif msg_type == "heartbeat":
                        state.update_heartbeat(payload)
                    
                    elif msg_type == "ticker_added":
                        state.set_status(f"✅ Added: {data.get('ticker')}")
                    
                    elif msg_type == "ticker_removed":
                        ticker = data.get('ticker')
                        state.markets.pop(ticker, None)
                        state.set_status(f"➖ Removed: {ticker}")
                    
                    elif msg_type == "tickers_list":
                        watched = data.get('watched', [])
                        state.set_status(f"📋 Watching: {', '.join(watched) if watched else 'none'}")
                    
                    elif msg_type == "available_list":
                        markets = data.get('markets', [])
                        # Handle both old format (list of strings) and new format (list of dicts)
                        if markets and isinstance(markets[0], dict):
                            state.available_markets = [m['ticker'] for m in markets]
                            state.available_markets_info = markets
                        else:
                            state.available_markets = markets
                            state.available_markets_info = None
                        if markets:
                            state.set_status(f"📋 Found {len(markets)} markets - check terminal", duration=10)
                        else:
                            state.set_status("❌ No markets found")
                    
                    elif msg_type == "error":
                        state.set_status(f"❌ {data.get('message', 'Unknown error')}", duration=10)
                    
                    elif msg_type == "search_results":
                        state.search_results = data.get('tickers', [])
                    
                    elif msg_type == "ticker_expired":
                        ticker = data.get('ticker')
                        if ticker:
                            state.markets.pop(ticker, None)
                            state.set_status(f"⏰ Expired: {ticker}", duration=5)
                    
                    elif msg_type == "ticker_expired":
                        ticker = data.get('ticker')
                        state.markets.pop(ticker, None)
                        state.set_status(f"⏰ Expired: {ticker}")
                        
        except websockets.exceptions.ConnectionClosed:
            state.connected = False
            state.ws = None
            console.print("❌ Connection lost, reconnecting in 3s...", style="red")
            await asyncio.sleep(3)
            
        except Exception as e:
            state.connected = False
            state.ws = None
            console.print(f"❌ Error: {e}, reconnecting in 5s...", style="red")
            await asyncio.sleep(5)


async def send_command(cmd_type: str, ticker: str = None):
    """Send command to server."""
    if not state.ws:
        state.set_status("❌ Not connected")
        return
    
    msg = {"type": cmd_type}
    if ticker:
        msg["ticker"] = ticker
    
    try:
        await state.ws.send(json.dumps(msg))
    except Exception as e:
        state.set_status(f"❌ Send failed: {e}")


async def run_display():
    """Run the live display."""
    with Live(build_layout(), refresh_per_second=4, console=console) as live:
        state.live = live
        while True:
            if not state.input_mode:
                live.update(build_layout())
            await asyncio.sleep(0.25)


async def handle_keyboard():
    """Handle keyboard input for commands."""
    import termios
    import tty
    
    # Save terminal settings
    old_settings = termios.tcgetattr(sys.stdin)
    
    try:
        tty.setcbreak(sys.stdin.fileno())
        
        while True:
            # Check if input available
            if select.select([sys.stdin], [], [], 0.1)[0]:
                char = sys.stdin.read(1)
                
                if char == 'q':
                    console.print("\n👋 Goodbye!", style="bold")
                    sys.exit(0)
                
                elif char == 'l':
                    await send_command("list_tickers")
                
                elif char == 'd':
                    # Demo mode - load latest BTC 15m
                    await send_command("demo_btc15m")
                    state.set_status("🎯 Loading BTC 15m demo...")
                
                elif char == 'c':
                    state.would_cancel_events.clear()
                    state.active_events.clear()
                    state.set_status("🗑️ Events cleared")
                
                elif char == 'a':
                    # Stop display and restore terminal for input
                    state.input_mode = True
                    if state.live:
                        state.live.stop()
                    termios.tcsetattr(sys.stdin, termios.TCSADRAIN, old_settings)
                    
                    console.print("\n[bold]Add Market[/bold]")
                    console.print("Paste Kalshi URL or enter ticker directly:")
                    
                    user_input = Prompt.ask("URL or ticker")
                    
                    ticker = None
                    if user_input:
                        # Check if it's a URL
                        if "kalshi.com" in user_input.lower():
                            # Extract ticker from URL: .../kxunitedcupmatch-26jan08merkre
                            parts = user_input.rstrip('/').split('/')
                            ticker_part = parts[-1].upper()
                            
                            # Clear previous results
                            state.search_results = []
                            
                            # Search for matching tickers
                            if state.ws:
                                await state.ws.send(json.dumps({"type": "search_ticker", "query": ticker_part}))
                            await asyncio.sleep(1)
                            
                            if state.search_results:
                                if len(state.search_results) == 1:
                                    ticker = state.search_results[0]
                                    console.print(f"[green]Found: {ticker}[/green]")
                                else:
                                    console.print(f"\n[bold]Event has {len(state.search_results)} markets:[/bold]")
                                    for i, t in enumerate(state.search_results[:10], 1):
                                        console.print(f"  {i}. {t}")
                                    if len(state.search_results) > 10:
                                        console.print(f"  ... and {len(state.search_results) - 10} more")
                                    console.print(f"  [cyan]0. Add ALL[/cyan]")
                                    choice = Prompt.ask("Enter number (0=all)")
                                    if choice == "0":
                                        # Add all markets
                                        for t in state.search_results:
                                            await send_command("add_ticker", t)
                                        console.print(f"[green]Added {len(state.search_results)} markets[/green]")
                                        ticker = None  # Already added
                                    elif choice.isdigit():
                                        idx = int(choice) - 1
                                        if 0 <= idx < len(state.search_results):
                                            ticker = state.search_results[idx]
                            else:
                                console.print(f"[yellow]No markets found matching: {ticker_part}[/yellow]")
                                console.print("[dim]Press Enter to continue...[/dim]")
                                input()
                        else:
                            ticker = user_input.upper()
                    
                    tty.setcbreak(sys.stdin.fileno())
                    if state.live:
                        state.live.start()
                    state.input_mode = False
                    if ticker:
                        await send_command("add_ticker", ticker)
                
                elif char == 'r':
                    # Stop display and restore terminal for input
                    state.input_mode = True
                    if state.live:
                        state.live.stop()
                    termios.tcsetattr(sys.stdin, termios.TCSADRAIN, old_settings)
                    
                    # Show current watched tickers
                    watched = list(state.markets.keys())
                    if watched:
                        console.print("\n[bold]Currently watching:[/bold]")
                        for i, ticker in enumerate(watched, 1):
                            console.print(f"  {i}. {ticker}")
                        console.print(f"  [cyan]0. Remove ALL[/cyan]")
                        choice = Prompt.ask("Enter number (0=all)")
                        
                        ticker = None
                        if choice == "0":
                            # Remove all
                            for t in watched:
                                await send_command("remove_ticker", t)
                            state.markets.clear()
                            console.print(f"[green]Removed {len(watched)} markets[/green]")
                        elif choice.isdigit():
                            idx = int(choice) - 1
                            if 0 <= idx < len(watched):
                                ticker = watched[idx]
                        elif choice:
                            ticker = choice.upper()
                    else:
                        console.print("\n[yellow]No tickers being watched[/yellow]")
                        await asyncio.sleep(1)
                        ticker = None
                    
                    tty.setcbreak(sys.stdin.fileno())
                    if state.live:
                        state.live.start()
                    state.input_mode = False
                    if ticker:
                        await send_command("remove_ticker", ticker)
                
                elif char == 'b':
                    # Browse available markets
                    state.input_mode = True
                    if state.live:
                        state.live.stop()
                    termios.tcsetattr(sys.stdin, termios.TCSADRAIN, old_settings)
                    
                    console.print("\n[bold]Popular series:[/bold]")
                    console.print("  KXBTC15M    - BTC 15-minute")
                    console.print("  KXBTCD      - BTC Daily")
                    console.print("  KXETH15M    - ETH 15-minute")
                    console.print("  KXETHD      - ETH Daily")
                    console.print("  KXWTAMATCH  - WTA Tennis")
                    console.print("  KXATPMATCH  - ATP Tennis")
                    console.print("  KXNBAMATCH  - NBA Games")
                    console.print("  Or enter any series ticker from URL")
                    
                    series = Prompt.ask("Enter series", default="KXBTC15M")
                    
                    # Request available markets
                    if state.ws:
                        await state.ws.send(json.dumps({"type": "list_available", "series": series.upper()}))
                    await asyncio.sleep(1)  # Wait for response
                    
                    if state.available_markets:
                        console.print(f"\n[bold]Available {series.upper()} Markets (soonest first):[/bold]")
                        if state.available_markets_info:
                            for i, info in enumerate(state.available_markets_info, 1):
                                ttc = info.get('ttc_mins', 0)
                                if ttc < 60:
                                    ttc_str = f"{ttc}m"
                                else:
                                    ttc_str = f"{ttc // 60}h {ttc % 60}m"
                                console.print(f"  {i}. {info['ticker']}  [dim]({ttc_str})[/dim]")
                        else:
                            for i, ticker in enumerate(state.available_markets, 1):
                                console.print(f"  {i}. {ticker}")
                        choice = Prompt.ask("Enter number to add (or press Enter to cancel)")
                        if choice.isdigit():
                            idx = int(choice) - 1
                            if 0 <= idx < len(state.available_markets):
                                await send_command("add_ticker", state.available_markets[idx])
                    else:
                        console.print(f"\n[yellow]No open markets found for {series}[/yellow]")
                        await asyncio.sleep(1)
                    
                    tty.setcbreak(sys.stdin.fileno())
                    if state.live:
                        state.live.start()
                    state.input_mode = False
            
            await asyncio.sleep(0.1)
    
    except Exception:
        pass
    finally:
        termios.tcsetattr(sys.stdin, termios.TCSADRAIN, old_settings)


async def run_observer(url: str, token: str):
    """Main entry point."""
    # Run all tasks
    await asyncio.gather(
        connect_and_listen(url, token),
        run_display(),
        handle_keyboard()
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
    parser.add_argument(
        "--size", "-s",
        type=int,
        default=100,
        help="Position size in contracts (default: 100)"
    )
    parser.add_argument(
        "--side",
        choices=["yes", "no", "both", "unknown"],
        default="unknown",
        help="Quote side: yes, no, both, or unknown (default: unknown)"
    )
    return parser.parse_args()


def main():
    """Entry point for console script."""
    args = parse_args()
    
    # Store config in state
    state.position_size = args.size
    state.quote_side = args.side
    
    try:
        asyncio.run(run_observer(args.url, args.token))
    except KeyboardInterrupt:
        console.print("\n👋 Goodbye!", style="bold")


if __name__ == "__main__":
    main()
