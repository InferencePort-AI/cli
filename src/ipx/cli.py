"""Root command: global options, command registration and top-level error handling."""

from __future__ import annotations

import sys
from typing import Optional

import click

from ipx import __version__, ui
from ipx.app import App
from ipx.clickx import IpxGroup
from ipx.errors import IpxError


@click.group(cls=IpxGroup, context_settings={"help_option_names": ["-h", "--help"], "max_content_width": 110},
             help="""The InferencePort AI command line.

Chat with cloud and local models, generate images, video, audio and 3D, manage organizations, host a model
server and keep your models and imports in sync with the desktop app — all from one binary.""",
             examples=[
                 "ipx auth login",
                 "ipx gen chat",
                 "ipx p2g image \"a lighthouse at dawn\" -o lighthouse.jpg",
                 "ipx model pull llama3.2 && ipx local chat llama3.2",
                 "ipx auth context acme && ipx org members list",
                 "ipx usage get",
             ])
@click.option("--json", "json_out", is_flag=True, envvar="IPX_JSON", help="Machine-readable JSON output.")
@click.option("-O", "--org", help="Run this command in an organization (slug), overriding the saved context.")
@click.option("--personal", is_flag=True, help="Ignore the organization context for this command.")
@click.option("--base-url", envvar="IPX_BASE_URL", help="API endpoint (default https://api.inferenceport.ai).")
@click.option("-q", "--quiet", is_flag=True, help="Only print results and errors.")
@click.option("-y", "--yes", is_flag=True, help="Assume yes for confirmation prompts.")
@click.option("--no-color", is_flag=True, help="Disable colour output.")
@click.version_option(__version__, "-V", "--version", prog_name="ipx", message="ipx %(version)s")
@click.pass_context
def root(ctx: click.Context, json_out: bool, org: Optional[str], personal: bool, base_url: Optional[str],
         quiet: bool, yes: bool, no_color: bool) -> None:
    ui.configure(json_out=json_out, quiet=quiet, yes=yes, no_color=no_color)
    ctx.obj = App(json_out=json_out, org=org, personal=personal, base_url=base_url)


def _register() -> None:
    from ipx.commands import (
        auth, config_cmd, gen, imports, local, misc, model, org, p2g, server, usage,
    )

    for mod in (auth, org, usage, p2g, gen, model, local, server, imports, config_cmd, misc):
        for cmd in mod.COMMANDS if hasattr(mod, "COMMANDS") else [mod.group]:
            root.add_command(cmd)


def _prepare_streams() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except Exception:
            pass


def main(argv: Optional[list] = None) -> None:
    _prepare_streams()
    _register()
    try:
        root.main(args=argv, standalone_mode=False, prog_name="ipx")
    except click.exceptions.Exit as e:
        sys.exit(e.exit_code)
    except click.Abort:
        ui.err.print("\n[muted]Cancelled.[/muted]")
        sys.exit(130)
    except click.UsageError as e:
        hint = f"Try `{e.ctx.command_path} --help`." if e.ctx else None
        ui.fail(e.format_message(), hint)
        sys.exit(2)
    except click.ClickException as e:
        ui.fail(e.format_message())
        sys.exit(e.exit_code)
    except IpxError as e:
        ui.render_error(e)
        sys.exit(e.code)
    except KeyboardInterrupt:
        ui.err.print("\n[muted]Cancelled.[/muted]")
        sys.exit(130)
    except BrokenPipeError:
        sys.exit(0)


if __name__ == "__main__":
    main()
