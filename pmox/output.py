"""Rendering helpers: human-friendly Rich tables, plus plain JSON for machine/AI use."""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, List, Optional

from rich.console import Console
from rich.table import Table

console = Console()
err_console = Console(stderr=True)

_GLYPH_FALLBACKS = {"✓": "OK", "→": "->", "·": "-", "…": "..."}


def glyph(char: str) -> str:
    """``char`` when the stdout encoding can render it, else an ASCII stand-in.

    Human-mode niceties (checkmarks, arrows) must degrade on legacy Windows
    consoles instead of printing ``?``; JSON output is \\u-escaped and immune.
    """
    encoding = getattr(sys.stdout, "encoding", None) or "ascii"
    try:
        char.encode(encoding)
    except (UnicodeEncodeError, LookupError):
        return _GLYPH_FALLBACKS.get(char, "?")
    return char


def human_bytes(value: Any) -> str:
    if value in (None, ""):
        return "-"
    try:
        n = float(value)
    except (TypeError, ValueError):
        return str(value)
    units = ["B", "KiB", "MiB", "GiB", "TiB", "PiB", "EiB"]
    negative = n < 0
    n = abs(n)
    i = 0
    while n >= 1024 and i < len(units) - 1:
        n /= 1024.0
        i += 1
    text = f"{int(n)} {units[i]}" if i == 0 else f"{n:.1f} {units[i]}"
    return f"-{text}" if negative else text


def human_uptime(value: Any) -> str:
    if not value:
        return "-"
    try:
        seconds = int(float(value))
    except (TypeError, ValueError):
        return str(value)
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, _ = divmod(rem, 60)
    parts: List[str] = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if minutes or not parts:
        parts.append(f"{minutes}m")
    return " ".join(parts)


def percent(value: Any, of: Any = None) -> str:
    if value is None:
        return "-"
    try:
        frac = float(value)
        if of is not None:
            denom = float(of)
            if denom == 0:
                return "-"
            frac = frac / denom
        return f"{frac * 100:.1f}%"
    except (TypeError, ValueError, ZeroDivisionError):
        return "-"


def fmt_epoch(value: Any) -> str:
    if not value:
        return "-"
    try:
        return datetime.fromtimestamp(int(value)).strftime("%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError, OSError, OverflowError):
        return str(value)


_STATUS_COLORS = {
    "running": "green",
    "online": "green",
    "active": "green",
    "available": "green",
    "stopped": "red",
    "offline": "red",
    "unavailable": "red",
    "error": "red",
    "paused": "yellow",
    "suspended": "yellow",
    "pending": "yellow",
}


def status_fmt(value: Any) -> str:
    if value is None:
        return "-"
    text = str(value)
    color = _STATUS_COLORS.get(text, "white")
    return f"[{color}]{text}[/{color}]"


@dataclass
class Column:
    header: str
    key: Optional[str] = None
    formatter: Optional[Callable[[Any], str]] = None
    row_formatter: Optional[Callable[[dict], str]] = None
    justify: str = "left"


def _cell(row: Any, col: Column) -> str:
    if col.row_formatter is not None:
        try:
            return col.row_formatter(row)
        except Exception:
            return "-"
    raw = row.get(col.key) if isinstance(row, dict) else getattr(row, col.key, None)
    if col.formatter is not None:
        return col.formatter(raw)
    return "-" if raw is None else str(raw)


def build_table(rows: List[dict], columns: List[Column], title: Optional[str] = None) -> Table:
    table = Table(title=title, header_style="bold cyan", title_style="bold")
    for col in columns:
        table.add_column(col.header, justify=col.justify, overflow="fold")
    for row in rows:
        table.add_row(*[_cell(row, col) for col in columns])
    return table


def build_kv_table(data: dict, title: Optional[str] = None) -> Table:
    table = Table(title=title, header_style="bold cyan", title_style="bold")
    table.add_column("Key", style="bold")
    table.add_column("Value", overflow="fold")
    for key in sorted(data.keys(), key=str):
        value = data[key]
        if isinstance(value, (dict, list)):
            value = json.dumps(value, default=str)
        table.add_row(str(key), str(value))
    return table


def emit(
    data: Any,
    columns: Optional[List[Column]] = None,
    json_output: bool = False,
    title: Optional[str] = None,
    console_: Optional[Console] = None,
) -> None:
    """Render ``data`` either as plain JSON (for machines/AI) or a Rich table/panel."""
    if json_output:
        print(json.dumps(data, default=str, indent=2))
        return
    out = console_ or console
    if isinstance(data, list):
        if columns:
            out.print(build_table(data, columns, title=title))
        elif data and isinstance(data[0], dict):
            cols = [Column(str(k), str(k)) for k in data[0].keys()]
            out.print(build_table(data, cols, title=title))
        elif data:
            for item in data:
                out.print(item)
        else:
            out.print("[dim](no results)[/dim]")
    elif isinstance(data, dict):
        out.print(build_kv_table(data, title=title))
    else:
        out.print(str(data))
