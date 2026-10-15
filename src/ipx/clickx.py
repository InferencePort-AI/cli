"""Click subclasses that render beautiful, consistent --help with rich."""

from __future__ import annotations

import copy
from typing import Any, Dict, List, Optional

import click
from rich import box
from rich.console import Group
from rich.padding import Padding
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from ipx import ui


def _opt_row(p: click.Parameter, ctx: click.Context) -> Optional[tuple]:
    if isinstance(p, click.Option):
        if p.hidden:
            return None
        names = ", ".join(p.opts + p.secondary_opts)
        if p.metavar:
            names += f" {p.metavar}"
        elif not p.is_flag and not p.count:
            tn = p.type.name
            names += " " + ("TEXT" if tn in ("text", "string") else "VALUE" if tn == "choice" else tn.upper())
        help_ = p.help or ""
        extras: List[str] = []
        if p.default not in (None, False, (), "") and not p.is_flag and p.show_default:
            extras.append(f"default: {p.default}")
        if p.required:
            extras.append("required")
        if isinstance(p.type, click.Choice):
            extras.append("|".join(p.type.choices))
        return names, help_, extras
    return None


class _HelpMixin:
    section: Optional[str] = None
    examples: List[str] = []

    def _render_help(self, ctx: click.Context) -> None:
        cmd: click.Command = self  # type: ignore[assignment]
        path = ctx.command_path
        parts: List[Any] = []
        if ctx.parent is None:
            parts.append(ui.banner())
            parts.append(Text(""))
        desc = (cmd.help or "").strip()
        if desc:
            first, _, rest = desc.partition("\n\n")
            parts.append(Text(first.replace("\n", " "), style="white"))
            if rest:
                parts.append(Text(rest.replace("\n", " "), style="muted"))
            parts.append(Text(""))
        usage = Text()
        usage.append("USAGE  ", style="bold " + ui.ACCENT)
        usage.append(path, style="bold white")
        if isinstance(cmd, click.Group):
            usage.append(" <command>", style=ui.BRAND)
        for p in cmd.params:
            if isinstance(p, click.Argument):
                nm = p.name.upper() + ("..." if p.nargs == -1 else "")
                usage.append(f" {nm}" if p.required else f" [{nm}]", style=ui.VIOLET)
        if any(isinstance(p, click.Option) and not p.hidden for p in cmd.params):
            usage.append(" [options]", style="muted")
        parts.append(usage)
        parts.append(Text(""))

        if isinstance(cmd, click.Group):
            groups: Dict[str, List[click.Command]] = {}
            for name in cmd.list_commands(ctx):
                sub = cmd.get_command(ctx, name)
                if sub is None or sub.hidden:
                    continue
                groups.setdefault(getattr(sub, "section", None) or "Commands", []).append(sub)
            for title, subs in groups.items():
                table = Table.grid(padding=(0, 3))
                table.add_column(style="cmd", no_wrap=True)
                table.add_column(style="white")
                for sub in subs:
                    short = (sub.short_help or (sub.help or "").strip().split("\n")[0]).rstrip(".")
                    label = sub.name
                    if isinstance(sub, click.Group):
                        label += " …"
                    table.add_row(label, short)
                parts.append(Text(title.upper(), style="bold " + ui.ACCENT))
                parts.append(Padding(table, (0, 0, 1, 2)))

        args = [p for p in cmd.params if isinstance(p, click.Argument)]
        if args:
            table = Table.grid(padding=(0, 3))
            table.add_column(style=ui.VIOLET, no_wrap=True)
            table.add_column(style="muted")
            for a in args:
                table.add_row(a.name.upper(), getattr(a, "help", "") or ("required" if a.required else "optional"))
            parts.append(Text("ARGUMENTS", style="bold " + ui.ACCENT))
            parts.append(Padding(table, (0, 0, 1, 2)))

        rows = [r for r in (_opt_row(p, ctx) for p in cmd.get_params(ctx)) if r]
        if rows:
            table = Table.grid(padding=(0, 3))
            table.add_column(style="key", no_wrap=True)
            table.add_column(style="white")
            table.add_column(style="muted")
            for names, help_, extras in rows:
                table.add_row(names, help_, f"[{', '.join(extras)}]" if extras else "")
            parts.append(Text("OPTIONS", style="bold " + ui.ACCENT))
            parts.append(Padding(table, (0, 0, 1, 2)))

        if self.examples:
            ex = Text()
            for line in self.examples:
                ex.append("$ ", style="muted")
                ex.append(line + "\n", style="white")
            parts.append(Text("EXAMPLES", style="bold " + ui.ACCENT))
            parts.append(Padding(ex, (0, 0, 1, 2)))

        if isinstance(cmd, click.Group):
            parts.append(Text(f"Run `{path} <command> --help` for details on a command.", style="muted"))
        ui.console.print(Group(*parts))

    def get_help(self, ctx: click.Context) -> str:  # type: ignore[override]
        self._render_help(ctx)
        ctx.exit(0)
        return ""


class IpxCommand(_HelpMixin, click.Command):
    def __init__(self, *a: Any, section: Optional[str] = None, examples: Optional[List[str]] = None, **kw: Any):
        super().__init__(*a, **kw)
        self.section = section
        self.examples = examples or []


class IpxGroup(_HelpMixin, click.Group):
    command_class = IpxCommand

    def __init__(self, *a: Any, section: Optional[str] = None, examples: Optional[List[str]] = None, **kw: Any):
        kw.setdefault("no_args_is_help", True)
        super().__init__(*a, **kw)
        self.section = section
        self.examples = examples or []

    def list_commands(self, ctx: click.Context) -> List[str]:
        return list(self.commands)  # definition order, not alphabetical

    def group(self, *a: Any, **kw: Any):  # type: ignore[override]
        kw.setdefault("cls", IpxGroup)
        return super().group(*a, **kw)

    def alias(self, name: str, target: str) -> None:
        """Register a hidden alias of an existing subcommand."""
        cmd = copy.copy(self.commands[target])
        cmd.hidden = True
        self.commands[name] = cmd


IpxGroup.group_class = IpxGroup  # subgroups created with @group.group() are IpxGroup too
