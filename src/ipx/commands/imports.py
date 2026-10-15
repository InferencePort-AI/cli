"""ipx import - add Hugging Face Spaces and websites to the desktop app's library."""

from __future__ import annotations

import colorsys
import html
import json
import random
import re
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

import click

from ipx import ui
from ipx.app import App, pass_app
from ipx.clickx import IpxGroup
from ipx.config import desktop_data_dir
from ipx.errors import ApiError, IpxError
from ipx.http import raw_request

EMOJIS = ["🌐", "🚀", "🧠", "🎨", "📚", "🛠️", "💡", "🔭", "🧪", "🎧", "🗺️", "⚡"]


def sanitize(s: str) -> str:
    return re.sub(r"[^a-zA-Z0-9]", "_", s)


def folder(kind: str) -> Path:
    d = desktop_data_dir() / ("spaces" if kind == "space" else "websites")
    d.mkdir(parents=True, exist_ok=True)
    return d


def readable_color() -> str:
    h, s, v = random.random(), random.uniform(0.45, 0.7), random.uniform(0.75, 0.95)
    r, g, b = (int(c * 255) for c in colorsys.hsv_to_rgb(h, s, v))
    return f"#{r:02x}{g:02x}{b:02x}"


def read_imports(kind: Optional[str] = None) -> List[Dict[str, Any]]:
    out = []
    for k in ("space", "website"):
        if kind and k != kind:
            continue
        for p in sorted(folder(k).glob("*.import")):
            try:
                d = json.loads(p.read_text(encoding="utf-8"))
                if isinstance(d, dict):
                    d["_file"] = str(p)
                    out.append(d)
            except (OSError, ValueError):
                continue
    return out


def fetch_title(url: str) -> Optional[str]:
    try:
        r = raw_request("GET", url, headers={"Accept": "text/html"}, timeout=8)
        m = re.search(r"<title[^>]*>(.*?)</title>", r.text[:200_000], re.I | re.S)  # type: ignore[union-attr]
        return html.unescape(re.sub(r"\s+", " ", m.group(1)).strip()) if m else None
    except IpxError:
        return None


@click.group("import", cls=IpxGroup, section="Local", short_help="Add Spaces and websites to the desktop app",
             examples=["ipx import space black-forest-labs/FLUX.1-schnell", "ipx import website https://excalidraw.com --title Excalidraw",
                       "ipx import list", "ipx import rm https://excalidraw.com"])
def group() -> None:
    """Add Hugging Face Spaces and websites to the InferencePort desktop app.

    Imports are written to the desktop app's library folder, so they show up under "Installed" next time the app
    loads that page (restart the app if it is already open). They work without the app being installed, too: the
    folder is created and picked up whenever you install it.
    """


@group.command("space", examples=["ipx import space stabilityai/stable-diffusion"])
@click.argument("repo", metavar="USER/REPO")
@pass_app
def space(app: App, repo: str) -> None:
    """Import a Hugging Face Space."""
    repo = re.sub(r"^https?://huggingface\.co/spaces/", "", repo).strip("/")
    if repo.count("/") != 1:
        raise IpxError("Use the form user/space-name.", "Example: black-forest-labs/FLUX.1-schnell")
    author, name = repo.split("/")
    try:
        with ui.status("Reading Space metadata…"):
            api = raw_request("GET", f"https://huggingface.co/api/spaces/{author}/{name}", timeout=15).json() or {}  # type: ignore[union-attr]
    except ApiError as e:
        if e.status == 404:
            raise IpxError(f"Space {repo} was not found on Hugging Face.")
        raise
    card = api.get("cardData") or {}
    cfg = {
        "type": "space",
        "title": re.sub(r"\b\w", lambda m: m.group(0).upper(), re.sub(r"[-_]", " ", name)),
        "author": author,
        "emoji": card.get("emoji") or "🧠",
        "background": f"linear-gradient(to right, {card.get('colorFrom') or '#3178c6'}, {card.get('colorTo') or '#90caf9'})",
        "sdk": api.get("sdk") or "unknown",
        "short_description": card.get("short_description") or "",
    }
    path = folder("space") / f"{sanitize(author)}____{sanitize(cfg['title'])}.import"
    path.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    app.out({**cfg, "file": str(path)}, lambda _: ui.ok(f"Imported Space [bold]{repo}[/bold] → {path.name}"))


@group.command("website", examples=["ipx import website https://huggingface.co --title \"Hugging Face\""])
@click.argument("url")
@click.option("--title", help="Display name (default: the page title).")
@click.option("--emoji", help="Card emoji (default: random).")
@pass_app
def website(app: App, url: str, title: Optional[str], emoji: Optional[str]) -> None:
    """Import a website as an app card."""
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    parsed = urlparse(url)
    if not parsed.netloc:
        raise IpxError("That does not look like a URL.", "Example: https://example.com")
    existing = [i for i in read_imports("website") if i.get("url") == url]
    if existing:
        raise IpxError(f"{url} is already imported.", "Remove it first with `ipx import rm`.")
    if not title:
        with ui.status("Fetching page title…"):
            title = fetch_title(url) or parsed.netloc
    cfg = {
        "type": "website", "title": title[:80], "author": "", "emoji": emoji or random.choice(EMOJIS),
        "background": f"linear-gradient(to right, {readable_color()}, {readable_color()})",
        "sdk": "unknown", "short_description": "", "url": url,
    }
    path = folder("website") / f"{uuid.uuid4()}.import"
    path.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    app.out({**cfg, "file": str(path)}, lambda _: ui.ok(f"Imported website [bold]{title}[/bold] ({url})"))


@group.command("file", short_help="Import a shared .import file")
@click.argument("path", type=click.Path(exists=True, dir_okay=False))
@pass_app
def file_(app: App, path: str) -> None:
    """Import an `.import` file someone shared from the desktop app."""
    try:
        cfg = json.loads(Path(path).read_text(encoding="utf-8"))
    except ValueError:
        raise IpxError("That file is not valid JSON.")
    if not isinstance(cfg, dict) or cfg.get("type") not in ("space", "website") or not cfg.get("title"):
        raise IpxError("Not an InferencePort import file.", "It must have a type of space or website, and a title.")
    if cfg["type"] == "website" and not str(cfg.get("url", "")).startswith(("http://", "https://")):
        raise IpxError("The website import has no valid URL.")
    name = (f"{sanitize(cfg.get('author', ''))}____{sanitize(cfg['title'])}.import" if cfg["type"] == "space" else f"{uuid.uuid4()}.import")
    dest = folder(cfg["type"]) / name
    dest.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    app.out({"file": str(dest)}, lambda _: ui.ok(f"Imported [bold]{cfg['title']}[/bold]"))


@group.command("list", short_help="What is imported")
@click.option("--type", "kind", type=click.Choice(["space", "website"]))
@pass_app
def list_(app: App, kind: Optional[str]) -> None:
    """List imported Spaces and websites."""
    items = read_imports(kind)

    def render(_: Any) -> None:
        if not items:
            ui.info("Nothing imported yet. Try `ipx import space user/repo` or `ipx import website <url>`.")
            return
        rows = [(i.get("emoji"), i.get("type"), i.get("title"), (f"{i['author']}/…" if i.get("type") == "space" and i.get("author") else i.get("url")), i.get("sdk") if i.get("type") == "space" else "")
                for i in items]
        ui.console.print(ui.make_table(["", "Type", "Title", "Source", "SDK"], rows, title=f"Imports ({len(items)})"))

    app.out([{k: v for k, v in i.items()} for i in items], render)


@group.command("rm", short_help="Remove an import")
@click.argument("target", metavar="URL|USER/REPO|TITLE")
@pass_app
def rm(app: App, target: str) -> None:
    """Remove an imported website (by URL or title) or Space (by user/repo or title)."""
    t = target.strip().lower()
    hits = []
    for i in read_imports():
        keys = {str(i.get("url", "")).lower(), str(i.get("title", "")).lower(), f"{i.get('author', '')}/{i.get('title', '')}".lower()}
        if i.get("type") == "space":
            keys.add(f"{i.get('author', '')}/{str(i.get('title', '')).replace(' ', '-')}".lower())
        if t in keys:
            hits.append(i)
    if not hits:
        raise IpxError(f"Nothing imported matches “{target}”.", "See `ipx import list`.")
    if len(hits) > 1 and not ui.STATE["yes"]:
        raise IpxError(f"{len(hits)} imports match “{target}”.", "Use the exact URL, or pass --yes to remove all of them.")
    for i in hits:
        Path(i["_file"]).unlink(missing_ok=True)
        ui.ok(f"Removed [bold]{i.get('title')}[/bold]")


COMMANDS = [group]
