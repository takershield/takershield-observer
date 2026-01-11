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

from . import __version__

try:
    import websockets
    import ssl
    from rich.console import Console, Group
    from rich.live import Live
    from rich.rule import Rule
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

# Display limits
MAX_EVENTS = 20  # Maximum risk events shown in table


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
        self.cleared_events_ts: float = 0  # Events before this timestamp are hidden
        
        # Regime transition tracking
        self.last_regime: Dict[str, str] = {}  # ticker -> last regime
        self.cleared_at: Dict[str, float] = {}  # ticker -> timestamp when cleared from NO_QUOTE
        
        # Help screen mode
        self.help_mode = False
    
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
        # Only add events after cleared timestamp
        # WouldCancelEvent uses timestamp_ms, EventRecord uses t0_ts
        ts = data.get("t0_ts") or data.get("timestamp_ms", 0)
        if ts > self.cleared_events_ts:
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
        return "[yellow]OT[/yellow]"  # Overtime - past expected but still active
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
    # Handle closed/settled markets
    if time_type == "closed":
        return "[red]CLOSED[/red]"
    # Now that we pick the earlier of close_time and expected_expiration_time,
    # we're always showing the most useful time. No need for ~ prefix.
    return format_time(seconds)


def build_market_table() -> Table:
    """Build market status table."""
    table = Table(
        title="📊 Market Status",
        box=box.SIMPLE,
        show_header=True,
        header_style="bold cyan",
        expand=True,
        padding=(0, 1)
    )
    
    table.add_column("Ticker", style="dim", width=28)
    table.add_column("Bid", justify="right", width=6)
    table.add_column("Ask", justify="right", width=6)
    table.add_column("Mid", justify="right", width=7)
    table.add_column("Spread", justify="right", width=6)
    table.add_column("Depth", justify="right", width=8)
    table.add_column("Signal", justify="center", width=24)
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
            # Show first trigger reason with emoji
            reason = trigger_reasons[0]
            if reason == "time_to_event":
                signal_str = "[red]🛑 NO_QUOTE[/red] [dim]( ttc ▼ )[/dim]"
            elif reason == "spread_blowout":
                signal_str = "[red]🛑 NO_QUOTE[/red] [dim]( sprd ▲ )[/dim]"
            elif reason == "high_volatility":
                signal_str = "[red]🛑 NO_QUOTE[/red] [dim]( p99 ▲ )[/dim]"
            elif reason == "ttc_spread":
                signal_str = "[red]🛑 NO_QUOTE[/red] [dim]( ttc+sprd )[/dim]"
            elif reason == "vol_spread":
                signal_str = "[red]🛑 NO_QUOTE[/red] [dim]( p99+sprd )[/dim]"
            elif reason == "no_book":
                signal_str = "[red]🛑 NO_QUOTE[/red] [dim]( no book )[/dim]"
            elif reason == "one_sided":
                signal_str = "[red]🛑 NO_QUOTE[/red] [dim]( 1-side )[/dim]"
            elif reason == "market_closed":
                signal_str = "[red]🛑 NO_QUOTE[/red] [dim]( closed )[/dim]"
            elif reason == "ml_risk":
                signal_str = "[red]🛑 NO_QUOTE[/red] [dim]( ml )[/dim]"
            else:
                signal_str = Text(regime, style=get_regime_style(regime))
        elif regime == "CAUTION" and caution_reasons:
            # Show first caution reason with direction emoji
            reason = caution_reasons[0]
            reason_labels = {
                "spread_elevated": "sprd ▲",
                "spread_widening": "sprd ▲",
                "volatility_rising": "vol ▲",
                "depth_dropping": "depth ▼",
                "time_liquidity": "late+liq",
                "time_approaching": "ttc ▼",  # legacy
            }
            label = reason_labels.get(reason, reason[:6])
            signal_str = f"[yellow]⚠️ CAUTION[/yellow] [dim]( {label} )[/dim]"
        elif regime == "SAFE":
            # Check if recently cleared from NO_QUOTE
            cleared_time = state.cleared_at.get(ticker, 0)
            if time.time() - cleared_time < 5:
                signal_str = "[bold green]✅ SAFE[/bold green] [cyan](cleared)[/cyan]"
            else:
                signal_str = "[bold green]✅ SAFE[/bold green]"
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
        box=box.SIMPLE,
        show_header=True,
        header_style="bold red",
        expand=True,
        padding=(0, 1)
    )
    
    table.add_column("Ticker", width=26)
    table.add_column("Trigger", width=14)
    table.add_column("Action", justify="center", width=12)
    table.add_column("Age", justify="right", width=6)
    table.add_column("Shielded", justify="right", width=8)
    table.add_column("Move (30s/2m/5m)", justify="right", width=30)
    
    now_ms = int(time.time() * 1000)
    now_sec = time.time()
    
    # Map trigger reasons to short display names with emoji
    trigger_labels = {
        "spread_blowout": "sprd ▲",
        "time_to_event": "ttc ▼",
        "ttc_spread": "ttc+sprd",
        "vol_spread": "vol+sprd",
        "high_volatility": "vol ▲",
        "time_liquidity": "late+liq",
        "no_book": "no book",
        "one_sided": "1-side",
        "market_closed": "closed",
    }
    
    def format_move_window(down: float, up: float) -> str:
        """Format a single window's move with direction indicator.
        
        Format: {arrow}{headline}({down}/{up})
        - down = adverse to YES quoter (price dropped)
        - up = adverse to NO quoter (price rose)
        - headline = max(down, up)
        - arrow: ▼ if down wins, ▲ if up wins, ◆ if tie
        """
        headline = max(down, up)
        if headline == 0:
            return "[dim]—[/dim]"
        
        if down > up:
            arrow = "▼"
        elif up > down:
            arrow = "▲"
        else:
            arrow = "◆"
        
        # Color code by severity
        if headline >= 10:
            style = "red"
        elif headline >= 5:
            style = "yellow"
        else:
            style = "green"
        
        return f"[{style}]{arrow}{headline:.0f}[/{style}]({down:.0f}/{up:.0f})"
    
    # Show active events from server (filter out old completed events)
    visible_events = []
    for event_id, event in list(state.active_events.items()):
        tracking_complete = event.get("tracking_complete", False)
        t0_ts = event.get("t0_ts", now_ms)
        elapsed_sec = (now_ms - t0_ts) / 1000
        
        # Skip events that completed more than 60 seconds ago
        if tracking_complete and elapsed_sec > 60:
            continue
        visible_events.append((event_id, event))
    
    # Sort by t0_ts descending (newest first), take last 10
    visible_events.sort(key=lambda x: x[1].get("t0_ts", 0), reverse=True)
    
    for event_id, event in visible_events[:MAX_EVENTS]:
        full_ticker = event.get("ticker", "?")
        ticker = full_ticker[-26:]
        raw_triggers = event.get("trigger_reasons", [])
        triggers = ", ".join(trigger_labels.get(t, t[:6]) for t in raw_triggers)
        trigger_str = f"[bold red]{triggers}[/bold red]"
        
        # Get adverse moves per direction
        # down = adverse_yes = price dropped (YES quoter loses)
        # up = adverse_no = price rose (NO quoter loses)
        down_30s = event.get("adverse_yes_30s", 0)
        up_30s = event.get("adverse_no_30s", 0)
        down_2m = event.get("adverse_yes_2m", 0)
        up_2m = event.get("adverse_no_2m", 0)
        down_5m = event.get("adverse_yes_5m", 0)
        up_5m = event.get("adverse_no_5m", 0)
        
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
        
        # Calculate duration (Shielded) - how long they were protected
        cleared_time = state.cleared_at.get(full_ticker, 0)
        current_regime = state.last_regime.get(full_ticker, "")
        
        if current_regime == "NO_QUOTE":
            # Still in NO_QUOTE - show ongoing indicator
            duration_str = "[yellow]ongoing[/yellow]"
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
        action_str = "[red bold]🛑 CANCEL[/red bold]"
        
        # Move display - expanded format with direction
        move_str = f"{format_move_window(down_30s, up_30s)}/{format_move_window(down_2m, up_2m)}/{format_move_window(down_5m, up_5m)}"
        
        table.add_row(ticker, trigger_str, action_str, age_str, duration_str, move_str)
    
    if not visible_events:
        # Fall back to legacy events
        for event in reversed(state.would_cancel_events[-5:]):
            raw_triggers = event.get("trigger_reasons", [])
            triggers = ", ".join(trigger_labels.get(t, t[:6]) for t in raw_triggers)
            trigger_str = f"[bold red]{triggers}[/bold red]"
            table.add_row(
                event.get("ticker", "?")[-26:],
                trigger_str,
                "[red bold]🛑 CANCEL[/red bold]",
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
    content.append("🔒 READ-ONLY (no keys)\n", style="cyan bold")
    
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


def build_help_screen() -> Panel:
    """Build full-screen help overlay."""
    content = Text()
    
    content.append("TakerShield – Risk Signals (30s overview)\n\n", style="bold cyan")
    
    content.append("SIGNALS\n", style="bold yellow")
    content.append("  SAFE      ", style="bold green")
    content.append("Market conditions normal. Quoting is reasonable.\n")
    content.append("  CAUTION   ", style="bold yellow")
    content.append("Risk rising. Consider widening quotes or reducing size.\n")
    content.append("  NO_QUOTE  ", style="bold red")
    content.append("High adverse-selection risk. Do not quote.\n\n")
    
    content.append("MOVE COLUMN\n", style="bold yellow")
    content.append("  • Shows worst price move AFTER a NO_QUOTE signal.\n")
    content.append("  • Windows: 30s / 2m / 5m from trigger time.\n")
    content.append("  • ▼ means mid moved DOWN (YES side would lose).\n")
    content.append("  • ▲ means mid moved UP (NO side would lose).\n")
    content.append("  • Numbers are cents vs mid at trigger (t0_mid).\n")
    content.append("  • Example: ▼4¢ means YES quotes would be picked off by 4¢.\n\n")
    
    content.append("WHAT THIS IS\n", style="bold yellow")
    content.append("  • Shadow-mode risk observer.\n")
    content.append("  • Shows what you avoided by standing down.\n")
    content.append("  • Not trading advice.\n\n")
    
    content.append("Press [h] to close help.", style="dim")
    
    return Panel(content, title="Help", border_style="cyan")


def build_layout() -> Layout:
    """Build the full layout."""
    # Check if help mode is active
    if state.help_mode:
        layout = Layout()
        layout.split_column(
            Layout(name="header", size=3),
            Layout(name="help"),
            Layout(name="footer", size=3)
        )
        
        # Header
        header = Panel(
            Text(f"🛡️ TakerShield Observer v{__version__}", justify="center", style="bold white"),
            border_style="cyan"
        )
        layout["header"].update(header)
        
        # Help screen
        layout["help"].update(build_help_screen())
        
        # Footer
        layout["footer"].update(Panel(Text("Press [h] to close help", justify="center", style="dim")))
        
        return layout
    
    # Normal layout
    layout = Layout()
    
    layout.split_column(
        Layout(name="header", size=3),
        Layout(name="main"),
        Layout(name="legend", size=1),
        Layout(name="footer", size=3)
    )
    
    layout["main"].split_row(
        Layout(name="content"),
        Layout(name="sidebar", size=30)  # Fixed width for sidebar
    )
    
    layout["sidebar"].split_column(
        Layout(name="stats", size=11),
        Layout(name="latency", size=7),
    )
    
    # Header
    header = Panel(
        Text(f"🛡️ TakerShield Observer v{__version__}", justify="center", style="bold white"),
        border_style="cyan"
    )
    layout["header"].update(header)
    
    # Content - stack both tables vertically with separator
    layout["content"].update(Group(
        build_market_table(),
        Rule(style="dim"),
        build_events_table()
    ))
    
    # Sidebar
    layout["stats"].update(build_stats_panel())
    layout["latency"].update(build_latency_panel())
    
    # Legend footer (one-line, dim)
    legend_text = Text("Move: worst @30s/2m/5m. ▲ NO hurt, ▼ YES hurt. (Y/N)=¢ vs t0_mid", style="dim", justify="center")
    layout["legend"].update(legend_text)
    
    # Footer with key bindings
    footer_text = "[a]dd  [r]emove  [d]emo  [c]lear  [h]elp  [q]uit"
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
                
                # Clear stale market data on reconnect
                # Don't re-subscribe - tickers may have expired/changed
                state.markets.clear()
                
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
                        
                        # Trust brain - just display what it sends
                        state.update_market(payload)
                    
                    elif msg_type == "would_cancel":
                        state.add_would_cancel(payload)
                    
                    elif msg_type == "event_update":
                        # Update event tracking from server
                        event_id = payload.get("event_id")
                        ts = payload.get("t0_ts") or payload.get("timestamp_ms", 0)
                        # Only show events after cleared timestamp
                        if event_id and ts > state.cleared_events_ts:
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
                            state.set_status(f"📋 Found {len(markets)} contracts - check terminal", duration=10)
                        else:
                            state.set_status("❌ No contracts found")
                    
                    elif msg_type == "error":
                        state.set_status(f"❌ {data.get('message', 'Unknown error')}", duration=10)
                    
                    elif msg_type == "search_results":
                        # Handle both old format (tickers list) and new format (contracts list with subtitle)
                        contracts = data.get('contracts', [])
                        if contracts:
                            state.search_results = contracts  # List of {ticker, subtitle}
                        else:
                            # Fallback for old format
                            tickers = data.get('tickers', [])
                            state.search_results = [{"ticker": t, "subtitle": ""} for t in tickers]
                    
                    elif msg_type == "ticker_expired":
                        ticker = data.get('ticker')
                        if ticker:
                            state.markets.pop(ticker, None)
                            state.set_status(f"⏰ Expired: {ticker}", duration=5)
                        
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
                
                elif char == 'h':
                    # Toggle help screen
                    state.help_mode = not state.help_mode
                
                elif char == 'd':
                    # Demo mode - load latest BTC 15m
                    await send_command("demo_btc15m")
                    state.set_status("🎯 Loading BTC 15m demo...")
                
                elif char == 'c':
                    # Set cleared timestamp - events before this will be hidden
                    state.cleared_events_ts = time.time() * 1000  # ms to match t0_ts
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
                            console.print(f"[dim]Searching for: {ticker_part}...[/dim]")
                            if state.ws:
                                await state.ws.send(json.dumps({"type": "search_ticker", "query": ticker_part}))
                            
                            # Wait up to 3 seconds for results
                            for _ in range(6):
                                await asyncio.sleep(0.5)
                                if state.search_results:
                                    break
                            
                            if state.search_results:
                                if len(state.search_results) == 1:
                                    ticker = state.search_results[0].get("ticker") if isinstance(state.search_results[0], dict) else state.search_results[0]
                                    console.print(f"[green]Found: {ticker}[/green]")
                                else:
                                    console.print(f"\n[bold]Event has {len(state.search_results)} contracts:[/bold]")
                                    for i, item in enumerate(state.search_results[:10], 1):
                                        if isinstance(item, dict):
                                            t = item.get("ticker", "")
                                            subtitle = item.get("subtitle", "")
                                            if subtitle:
                                                console.print(f"  {i}. {t} [dim]({subtitle})[/dim]")
                                            else:
                                                console.print(f"  {i}. {t}")
                                        else:
                                            console.print(f"  {i}. {item}")
                                    if len(state.search_results) > 10:
                                        console.print(f"  ... and {len(state.search_results) - 10} more")
                                    both_all = "both" if len(state.search_results) == 2 else "all"
                                    console.print(f"  [cyan]0. All (observe {both_all})[/cyan]")
                                    choice = Prompt.ask("Select contract to observe (0=all)")
                                    if choice == "0":
                                        # Add all contracts
                                        for item in state.search_results:
                                            t = item.get("ticker") if isinstance(item, dict) else item
                                            await send_command("add_ticker", t)
                                        console.print(f"[green]Added {len(state.search_results)} contracts[/green]")
                                        ticker = None  # Already added
                                    elif choice.isdigit():
                                        idx = int(choice) - 1
                                        if 0 <= idx < len(state.search_results):
                                            item = state.search_results[idx]
                                            ticker = item.get("ticker") if isinstance(item, dict) else item
                            else:
                                console.print(f"[yellow]No contracts found matching: {ticker_part}[/yellow]")
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
                        console.print(f"  [cyan]0. Remove all[/cyan]")
                        choice = Prompt.ask("Select contract to remove (0=all)")
                        
                        ticker = None
                        if choice == "0":
                            # Remove all
                            for t in watched:
                                await send_command("remove_ticker", t)
                            state.markets.clear()
                            console.print(f"[green]Removed {len(watched)} contracts[/green]")
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
