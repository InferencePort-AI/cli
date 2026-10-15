"""Terminal presentation: theme, banner, tables, key/value panels, progress and consistent messaging."""

from __future__ import annotations

import json as _json
import os
import sys
from contextlib import contextmanager
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from rich import box
from rich.console import Console, Group
from rich.panel import Panel
from rich.progress import (
    BarColumn,
    DownloadColumn,
    Progress,
    SpinnerColumn,
    TaskProgressColumn,
    TextColumn,
    TimeElapsedColumn,
    TimeRemainingColumn,
    TransferSpeedColumn,
)
from rich.table import Table
from rich.text import Text
from rich.theme import Theme

BRAND = "#4F7CFF"
ACCENT = "#22D3EE"
VIOLET = "#A78BFA"
OK = "#34D399"
WARN = "#FBBF24"
BAD = "#F87171"
MUTED = "#94A3B8"
DIM = "#64748B"

THEME = Theme(
    {
        "brand": f"bold {BRAND}",
        "accent": ACCENT,
        "violet": VIOLET,
        "ok": f"bold {OK}",
        "warn": f"bold {WARN}",
        "bad": f"bold {BAD}",
        "muted": MUTED,
        "dim": DIM,
        "key": f"bold {ACCENT}",
        "cmd": f"bold {BRAND}",
        "user": f"bold {ACCENT}",
        "bot": f"bold {VIOLET}",
        "markdown.code": f"bold {ACCENT}",
        "markdown.link": f"underline {BRAND}",
    }
)

_ART = [
    "  ██╗██████╗ ██╗  ██╗",
    "  ██║██╔══██╗╚██╗██╔╝",
    "  ██║██████╔╝ ╚███╔╝ ",
    "  ██║██╔═══╝  ██╔██╗ ",
    "  ██║██║     ██╔╝ ██╗",
    "  ╚═╝╚═╝     ╚═╝  ╚═╝",
]


def _make_console(**kw: Any) -> Console:
    force_color = os.environ.get("FORCE_COLOR")
    return Console(
        theme=THEME,
        highlight=False,
        force_terminal=True if force_color else None,
        no_color=bool(os.environ.get("NO_COLOR")),
        **kw,
    )


console = _make_console()
err = _make_console(stderr=True)

# Set by the root command; commands consult it to switch to machine-readable output.
STATE: Dict[str, Any] = {"json": False, "quiet": False, "yes": False}


def configure(*, json_out: bool = False, quiet: bool = False, yes: bool = False, no_color: bool = False) -> None:
    global console, err
    STATE.update(json=json_out, quiet=quiet, yes=yes)
    if no_color:
        os.environ["NO_COLOR"] = "1"
        console = _make_console()
        err = _make_console(stderr=True)


def is_tty() -> bool:
    try:
        return sys.stdin.isatty() and sys.stdout.isatty()
    except Exception:  # pragma: no cover
        return False


# --------------------------------------------------------------------------- banner


def _lerp(a: Tuple[int, int, int], b: Tuple[int, int, int], t: float) -> str:
    return "#%02x%02x%02x" % tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))


def gradient(text: str, start: Tuple[int, int, int] = (0x4F, 0x7C, 0xFF),
             end: Tuple[int, int, int] = (0xA7, 0x8B, 0xFA)) -> Text:
    out = Text()
    n = max(len(text) - 1, 1)
    for i, ch in enumerate(text):
        out.append(ch, style=_lerp(start, end, i / n))
    return out


def banner(subtitle: Optional[str] = None) -> Group:
    from ipx import __version__

    rows = []
    width = max(len(r) for r in _ART)
    for i, line in enumerate(_ART):
        t = Text()
        for j, ch in enumerate(line.ljust(width)):
            frac = (j / width) * 0.7 + (i / len(_ART)) * 0.3
            t.append(ch, style=_lerp((0x4F, 0x7C, 0xFF), (0xA7, 0x8B, 0xFA), frac) if ch != " " else "")
        rows.append(t)
    tag = Text()
    tag.append("  InferencePort AI", style="bold white")
    tag.append(f"  ·  v{__version__}", style="muted")
    if subtitle:
        tag.append(f"  ·  {subtitle}", style="muted")
    return Group(*rows, tag)


def print_banner(subtitle: Optional[str] = None) -> None:
    if STATE["json"] or STATE["quiet"]:
        return
    console.print()
    console.print(banner(subtitle))
    console.print()


# --------------------------------------------------------------------------- messages


def ok(msg: str) -> None:
    if not STATE["quiet"]:
        console.print(f"[ok]✔[/ok] {msg}")


def info(msg: str) -> None:
    if not STATE["quiet"]:
        console.print(f"[accent]ℹ[/accent] {msg}")


def warn(msg: str) -> None:
    err.print(f"[warn]▲[/warn] {msg}")


def fail(msg: str, hint: Optional[str] = None) -> None:
    err.print(f"[bad]✖ {msg}[/bad]")
    if hint:
        err.print(f"  [muted]→ {hint}[/muted]")


def dim(msg: str) -> None:
    if not STATE["quiet"]:
        console.print(f"[muted]{msg}[/muted]")


def heading(title: str, sub: Optional[str] = None) -> None:
    if STATE["json"]:
        return
    t = Text()
    t.append("▍", style=BRAND)
    t.append(f" {title}", style="bold white")
    if sub:
        t.append(f"  {sub}", style="muted")
    console.print(t)


# --------------------------------------------------------------------------- data output


def emit_json(data: Any) -> None:
    sys.stdout.write(_json.dumps(data, indent=2, default=str, ensure_ascii=False) + "\n")


def kv_panel(title: str, rows: Sequence[Tuple[str, Any]], *, subtitle: Optional[str] = None) -> Panel:
    table = Table.grid(padding=(0, 2))
    table.add_column(style="key", justify="right", no_wrap=True)
    table.add_column(overflow="fold")
    for k, v in rows:
        if v is None or v == "":
            v = "—"
        table.add_row(str(k), v if isinstance(v, (Text, Table)) else str(v))
    return Panel(
        table,
        title=f"[brand]{title}[/brand]",
        subtitle=f"[muted]{subtitle}[/muted]" if subtitle else None,
        title_align="left",
        border_style=DIM,
        box=box.ROUNDED,
        padding=(1, 2),
    )


def make_table(columns: Iterable[str], rows: Iterable[Sequence[Any]], *, title: Optional[str] = None,
               right: Iterable[str] = ()) -> Table:
    table = Table(
        title=title,
        title_justify="left",
        title_style="brand",
        box=box.SIMPLE_HEAD,
        header_style=f"bold {ACCENT}",
        border_style=DIM,
        pad_edge=False,
        expand=False,
    )
    right = set(right)
    for c in columns:
        table.add_column(c, justify="right" if c in right else "left", overflow="fold")
    for r in rows:
        table.add_row(*[("—" if x is None or x == "" else (x if isinstance(x, Text) else str(x))) for x in r])
    return table


def bar(used: float, limit: Optional[float], width: int = 28) -> Text:
    """A coloured usage meter. limit=None means unlimited."""
    t = Text()
    if limit is None:
        t.append("━" * width, style=ACCENT)
        t.append("  ∞ unlimited", style="muted")
        return t
    limit = max(float(limit), 0.0)
    frac = 0.0 if limit == 0 else min(float(used) / limit, 1.0)
    filled = int(round(frac * width))
    colour = OK if frac < 0.6 else (WARN if frac < 0.85 else BAD)
    t.append("━" * filled, style=colour)
    t.append("━" * (width - filled), style=DIM)
    t.append(f"  {fmt_num(used)} / {fmt_num(limit)}", style="white")
    t.append(f"  {int(frac * 100)}%", style="muted")
    return t


def fmt_num(n: Any) -> str:
    try:
        f = float(n)
    except (TypeError, ValueError):
        return str(n)
    if f == int(f):
        return f"{int(f):,}"
    return f"{f:,.4f}".rstrip("0").rstrip(".")


def fmt_credits(n: Any) -> str:
    try:
        f = float(n)
    except (TypeError, ValueError):
        return str(n)
    if f == int(f):
        return f"{int(f):,}"
    return f"{f:,.4f}".rstrip("0").rstrip(".")


def fmt_bytes(n: float) -> str:
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


# --------------------------------------------------------------------------- progress / spinners


@contextmanager
def status(message: str):
    """Spinner for a blocking step (no-op in JSON/quiet mode)."""
    if STATE["json"] or STATE["quiet"] or not console.is_terminal:
        yield None
        return
    with console.status(f"[accent]{message}[/accent]", spinner="dots12", spinner_style=BRAND) as s:
        yield s


def job_progress() -> Progress:
    return Progress(
        SpinnerColumn("dots12", style=BRAND),
        TextColumn("[accent]{task.description}"),
        BarColumn(bar_width=34, complete_style=BRAND, finished_style=OK, pulse_style=VIOLET),
        TaskProgressColumn(),
        TimeElapsedColumn(),
        console=console,
        transient=False,
        disable=STATE["json"] or STATE["quiet"],
    )


def download_progress() -> Progress:
    return Progress(
        SpinnerColumn("dots12", style=BRAND),
        TextColumn("[accent]{task.description}"),
        BarColumn(bar_width=34, complete_style=BRAND, finished_style=OK),
        DownloadColumn(),
        TransferSpeedColumn(),
        TimeRemainingColumn(),
        console=console,
        disable=STATE["json"] or STATE["quiet"],
    )


def confirm(question: str, *, default: bool = False) -> bool:
    if STATE["yes"]:
        return True
    if not is_tty():
        from ipx.errors import IpxError

        raise IpxError(f"Needs confirmation: {question}", "Re-run with --yes to confirm non-interactively.")
    from rich.prompt import Confirm

    return Confirm.ask(f"[warn]?[/warn] {question}", default=default, console=console)


def ask(question: str, *, default: Optional[str] = None, password: bool = False) -> str:
    if not is_tty():
        from ipx.errors import IpxError

        raise IpxError(f"Cannot prompt for “{question}” without a terminal.", "Pass the value as an option instead.")
    from rich.prompt import Prompt

    return Prompt.ask(f"[accent]?[/accent] {question}", default=default, password=password, console=console) or ""


def choose(question: str, options: List[Tuple[str, str]], default: int = 0) -> str:
    """Numbered picker. options = [(value, label)]. Returns the chosen value."""
    from ipx.errors import IpxError

    if not is_tty():
        raise IpxError(f"Cannot ask “{question}” without a terminal.")
    console.print(f"[accent]?[/accent] {question}")
    for i, (_, label) in enumerate(options, 1):
        mark = "[brand]❯[/brand]" if i - 1 == default else " "
        console.print(f"  {mark} [key]{i}[/key] {label}")
    from rich.prompt import IntPrompt

    n = IntPrompt.ask("  Choose", choices=[str(i) for i in range(1, len(options) + 1)],
                      default=default + 1, console=console, show_choices=False)
    return options[int(n) - 1][0]


def render_error(e: BaseException) -> None:
    from ipx.errors import IpxError

    if isinstance(e, IpxError):
        fail(e.message, e.hint)
    else:
        fail(f"{type(e).__name__}: {e}")
