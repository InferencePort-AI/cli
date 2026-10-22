"""ipx server - host your local models for approved users (the desktop app's "Host (Proxy)" server)."""

from __future__ import annotations

import json
import os
import secrets
import signal
import socket
import subprocess
import sys
import threading
import time
from typing import Any, Dict, List, Optional

import click
from rich.text import Text

from ipx import policy, ui
from ipx.app import App, pass_app
from ipx.clickx import IpxGroup
from ipx.config import DEFAULT_SERVER_PORT, config_dir
from ipx.errors import IpxError
from ipx.local import ollama_version
from ipx.proxy import ProxyServer, Verifier, log_path

PID_FILE = "server.pid"


# --------------------------------------------------------------------------- process helpers


def pid_path():
    return config_dir() / PID_FILE


def read_pid() -> Optional[Dict[str, Any]]:
    try:
        d = json.loads(pid_path().read_text(encoding="utf-8"))
        return d if isinstance(d, dict) and d.get("pid") else None
    except (OSError, ValueError):
        return None


def pid_alive(pid: int) -> bool:
    if sys.platform == "win32":
        import ctypes

        k32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        h = k32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return False
        code = ctypes.c_ulong()
        ok_ = k32.GetExitCodeProcess(h, ctypes.byref(code))
        k32.CloseHandle(h)
        return bool(ok_) and code.value == 259  # STILL_ACTIVE
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def healthy(d: Dict[str, Any]) -> bool:
    """Does the recorded server answer /__health? Guards against a recycled PID."""
    import http.client

    host = d.get("host") or "127.0.0.1"
    host = "127.0.0.1" if host in ("0.0.0.0", "::", "") else host
    try:
        conn = http.client.HTTPConnection(host, int(d["port"]), timeout=1.5)
        conn.request("GET", "/__health")
        ok_ = conn.getresponse().status in (204, 429)
        conn.close()
        return ok_
    except (OSError, ValueError, KeyError):
        return False


def running() -> Optional[Dict[str, Any]]:
    """The live server described by the pid file, or None. A stale pid file (crash, reused PID) is removed and
    never acted on, so `stop` can only ever signal a process that really is our server."""
    d = read_pid()
    if d and pid_alive(int(d["pid"])) and healthy(d):
        return d
    if d:
        try:
            pid_path().unlink()
        except OSError:
            pass
    return None


def kill(pid: int) -> None:
    if sys.platform == "win32":
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True)
    else:
        os.kill(pid, signal.SIGTERM)


def lan_ips() -> List[str]:
    ips = set()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if not ip.startswith("127."):
                ips.add(ip)
    except OSError:
        pass
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.0.2.1", 9))
            ips.add(s.getsockname()[0])
    except OSError:
        pass
    return sorted(ips)


def self_command() -> List[str]:
    """How to re-invoke this program (Nuitka binary, or `python -m ipx`)."""
    if "__compiled__" in globals() or getattr(sys, "frozen", False):
        import shutil

        argv0 = sys.argv[0]
        if os.path.dirname(argv0):
            return [os.path.abspath(argv0)]
        return [shutil.which(argv0) or sys.executable]  # launched through PATH
    return [sys.executable, "-m", "ipx"]


def parse_users(allow: List[str]) -> List[Dict[str, str]]:
    users = []
    for a in allow:
        email, _, role = a.partition(":")
        email, role = email.strip(), (role.strip().lower() or "member")
        if "@" not in email:
            raise IpxError(f"“{a}” is not an email address.", "Use --allow user@example.com or user@example.com:admin")
        if role not in ("admin", "member"):
            raise IpxError(f"Unknown role “{role}” for {email}.", "Roles are admin and member.")
        users.append({"email": email, "role": role})
    return users


def monitor_host_policy(app, srv, users, keys, stopped):
    """Revoke an active listener on the desktop's five-minute policy cadence."""
    while not stopped.wait(5 * 60):
        try:
            settings = policy.host_settings(app)
            if settings["managed"] and settings["access"] != "manual":
                allowed = {u["email"]: "admin" if u.get("role") in ("owner", "admin") else u.get("role", "member")
                           for u in settings["users"]}
                if keys or any(allowed.get(u["email"]) != u["role"] for u in users):
                    raise IpxError("Organization server membership changed.")
        except IpxError as err:
            srv.log.line("WARN", f"Hosting revoked: {err.message}")
            srv.shutdown()
            return


# --------------------------------------------------------------------------- commands


@click.group("server", cls=IpxGroup, section="Local", short_help="Host your local models for others",
             examples=["ipx server start --allow teammate@acme.com", "ipx server start --generate-key --detach",
                       "ipx server status", "ipx server logs -f", "ipx server stop"])
def group() -> None:
    """Share your local models with approved people over your network.

    The host server proxies Ollama behind InferencePort sign-in (allowed emails) or server API keys, on port 52458 by
    default — compatible with the desktop app's "Host (Proxy)" mode. With an organization context, the org's
    hosting policy and member list are applied.
    """


@group.command("start")
@click.option("--port", type=click.IntRange(1, 65535), default=DEFAULT_SERVER_PORT, show_default=True)
@click.option("--host", default="0.0.0.0", show_default=True, help="Interface to bind (use 127.0.0.1 for this machine only).")
@click.option("--allow", "allow", multiple=True, metavar="EMAIL[:ROLE]", help="Allow a user (role admin|member, default member). Repeatable.")
@click.option("--api-key", "api_keys", multiple=True, help="Accept this server API key (repeatable).")
@click.option("--generate-key", is_flag=True, help="Create a random server API key and print it once.")
@click.option("--detach", "-d", is_flag=True, help="Run in the background; manage with `ipx server stop|status|logs`.")
@click.option("--verbose", is_flag=True, help="Echo the log to this terminal.")
@pass_app
def start(app: App, port: int, host: str, allow, api_keys, generate_key: bool, detach: bool, verbose: bool) -> None:
    """Start the host server (foreground by default)."""
    if running():
        d = running()
        raise IpxError(f"A server is already running (pid {d['pid']}, port {d.get('port')}).", "Stop it first with `ipx server stop`.")
    if not ollama_version(app.cfg.ollama_url):
        raise IpxError("Local Ollama is not running, so there is nothing to host.", "Start it (`ollama serve`) and try again.")

    settings = policy.host_settings(app)  # raises if the organization forbids hosting
    users = parse_users(list(allow))
    if settings["managed"] and settings["access"] != "manual":
        merged = {}
        for u in settings["users"]:
            merged[u["email"].lower()] = {"email": u["email"].lower(), "role": "admin" if u.get("role") in ("owner", "admin") else (u.get("role") or "member")}
        users = list(merged.values())
    keys = list(api_keys) + [k for k in os.environ.get("IPX_SERVER_KEYS", "").split(",") if k]
    if settings["managed"] and settings["access"] != "manual":
        keys = []
        if generate_key:
            raise IpxError("Shared server API keys are disabled by organization membership access.")
    new_key = None
    if generate_key:
        new_key = "ipxs_" + secrets.token_urlsafe(24)
        keys.append(new_key)
    if not users and not keys:
        raise IpxError("Nobody would be able to connect.", "Add people with --allow EMAIL, or create a key with --generate-key.")
    if users and app.creds.auth_type != "jwt":
        raise IpxError("Verifying users requires a signed-in session (not an API key).",
                       "Run `ipx auth login` with email or OAuth, or host with --generate-key / --api-key only.")

    if detach:
        env = dict(os.environ, IPX_SERVER_KEYS=",".join(keys), IPX_DETACHED="1")
        args = self_command() + ["--org", app.org] * bool(app.org) + ["--personal"] * bool(app.personal) + ["server", "start", "--port", str(port), "--host", host]
        for u in users:
            args += ["--allow", f"{u['email']}:{u['role']}"]
        log = open(config_dir() / "server-process.log", "ab")
        kw: Dict[str, Any] = dict(stdin=subprocess.DEVNULL, stdout=log, stderr=log, env=env)
        if sys.platform == "win32":
            kw["creationflags"] = 0x00000008 | 0x00000200 | 0x08000000  # DETACHED | NEW_GROUP | NO_WINDOW
        else:
            kw["start_new_session"] = True
        try:
            proc = subprocess.Popen(args, **kw)
        finally:
            log.close()
        for _ in range(60):
            time.sleep(0.25)
            if proc.poll() is not None:
                raise IpxError("The server exited right after starting.", f"See `ipx server logs` ({log_path()}).")
            if running():
                break
        else:
            raise IpxError("The server did not become ready in time.", f"See `ipx server logs` ({log_path()}).")
        _banner(app, host, port, users, keys, new_key, background=True)
        return

    srv = ProxyServer(host=host, port=port, ollama_url=app.cfg.ollama_url, users=users, api_keys=keys,
                      verifier=Verifier(app.cfg, app.creds) if users else None, echo=verbose)
    stopped = threading.Event()

    def advertise() -> None:  # only once the port is bound, so a pid file always means "listening"
        pid_path().write_text(json.dumps({"pid": os.getpid(), "port": port, "host": host, "started": time.time(), "org": app.org}), encoding="utf-8")
        threading.Thread(target=monitor_host_policy, args=(app, srv, users, keys, stopped), daemon=True).start()

    try:
        if not os.environ.get("IPX_DETACHED"):
            _banner(app, host, port, users, keys, new_key, background=False)
        srv.serve(on_ready=advertise)
    except OSError as e:
        raise IpxError(f"Could not listen on {host}:{port}: {e.strerror or e}", "Is another server (or the desktop app host mode) using this port?")
    except KeyboardInterrupt:
        ui.console.print("\n[muted]Stopping server…[/muted]")
    finally:
        stopped.set()
        try:
            pid_path().unlink()
        except OSError:
            pass


def _banner(app: App, host: str, port: int, users, keys, new_key: Optional[str], background: bool) -> None:
    if app.json:
        ui.emit_json({"running": True, "port": port, "host": host, "users": [u["email"] for u in users], "background": background, "api_key": new_key})
        return
    urls = [f"http://localhost:{port}"] + ([f"http://{ip}:{port}" for ip in lan_ips()] if host in ("0.0.0.0", "::") else [])
    rows: List[Any] = [("Status", Text("● serving" + (" in the background" if background else " — press Ctrl+C to stop"), style="ok")),
                       ("Endpoints", Text("\n".join(urls)))]
    rows.append(("Allowed users", "\n".join(f"{u['email']} ({u['role']})" for u in users) or "none (API keys only)"))
    rows.append(("API keys", f"{len(keys)} configured"))
    if app.org:
        rows.append(("Organization", Text(app.org, style=ui.VIOLET)))
    ui.console.print(ui.kv_panel("InferencePort host server", rows, subtitle="clients: Authorization: Bearer <token>"))
    if new_key:
        ui.console.print(ui.kv_panel("Server API key — shown once", [("Key", Text(new_key, style="bold " + ui.WARN))]))
    if host in ("0.0.0.0", "::"):
        ui.warn("Listening on all interfaces. Only expose this port on networks you trust.")
    if background:
        ui.dim("Manage with `ipx server status`, `ipx server logs -f`, `ipx server stop`.")


@group.command("stop")
@pass_app
def stop(app: App) -> None:
    """Stop the running host server."""
    d = running()
    if not d:
        app.out({"running": False}, lambda _: ui.info("No server is running."))
        return
    kill(int(d["pid"]))
    for _ in range(20):
        if not pid_alive(int(d["pid"])):
            break
        time.sleep(0.2)
    try:
        pid_path().unlink()
    except OSError:
        pass
    app.out({"stopped": True}, lambda _: ui.ok(f"Stopped server (pid {d['pid']})."))


@group.command("status")
@pass_app
def status(app: App) -> None:
    """Is the server running, and is it healthy?"""
    d = running()
    info = {"running": bool(d), **(d or {}), "healthy": True if d else None}

    def render(_: Any) -> None:
        if not d:
            ui.console.print(ui.kv_panel("Host server", [("Status", Text("○ not running", style="muted")), ("Start", "ipx server start --allow you@example.com")]))
            return
        up = int(time.time() - float(d.get("started", time.time())))
        ui.console.print(ui.kv_panel("Host server", [
            ("Status", Text("● running", style="ok")),
            ("PID", d["pid"]), ("Port", d.get("port")), ("Uptime", f"{up // 3600}h {up % 3600 // 60}m {up % 60}s"),
            ("Organization", d.get("org") or "personal"), ("Log", str(log_path()))]))

    app.out(info, render)


@group.command("logs")
@click.option("-n", "--lines", type=int, default=40, show_default=True)
@click.option("-f", "--follow", is_flag=True, help="Keep streaming new lines.")
def logs(lines: int, follow: bool) -> None:
    """Show the server log."""
    p = log_path()
    if not p.exists():
        ui.info("No log yet. Start the server first.")
        return
    with open(p, "r", encoding="utf-8", errors="replace") as fh:
        for ln in fh.readlines()[-lines:]:
            _print_log(ln.rstrip("\n"))
        offset = fh.tell()
        identity = p.stat().st_ino
    if follow:
        try:
            while True:
                try:
                    stat = p.stat()
                    if stat.st_ino != identity or stat.st_size < offset:
                        offset = 0
                    identity = stat.st_ino
                    # Reopen each poll: an open Windows reader prevents rename.
                    with open(p, "r", encoding="utf-8", errors="replace") as fh:
                        fh.seek(offset)
                        for ln in fh:
                            _print_log(ln.rstrip("\n"))
                        offset = fh.tell()
                except FileNotFoundError:
                    pass
                time.sleep(0.4)
        except KeyboardInterrupt:
            pass



def _print_log(line: str) -> None:
    style = "bad" if "[ERROR]" in line else "warn" if "[WARN]" in line else "muted"
    ui.console.print(Text(line, style=style), soft_wrap=True)


COMMANDS = [group]
