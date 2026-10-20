"""Typed System One decisions on the unified Pay-2-Go API."""

import base64
import json
import sys
from pathlib import Path

import click

from ipx import ui
from ipx.app import App, pass_app
from ipx.commands.modalities import write_atomic
from ipx.errors import IpxError

_MEDIA_MIMES = {
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
    ".webp": "image/webp", ".mp4": "video/mp4", ".webm": "video/webm",
    ".mov": "video/quicktime", ".mkv": "video/x-matroska",
}


def _media_data_url(path: str, kind: str) -> str:
    mime = _MEDIA_MIMES.get(Path(path).suffix.lower())
    if not mime or not mime.startswith(kind + "/"):
        raise IpxError(f"Unsupported {kind} extension: {path}")
    return f"data:{mime};base64,{base64.b64encode(Path(path).read_bytes()).decode('ascii')}"


def add_decision(group):
    @group.command("decision", short_help="Classify state with System One questions")
    @click.argument("state", required=False)
    @click.option("-m", "--model", default="microsoft/decision-1", show_default=True)
    @click.option("-q", "--questions", type=click.Path(exists=True, dir_okay=False), help="JSON file containing the named questions object.")
    @click.option("-f", "--file", "request_file", type=click.Path(dir_okay=False), help="Full System One request JSON. Use - for stdin.")
    @click.option("-o", "--output", help="Save the full response as JSON.")
    @click.option("--image", "image_paths", multiple=True, type=click.Path(exists=True, dir_okay=False),
                  help="Attach an image (PNG/JPEG/WebP). Repeat up to the model's limit.")
    @click.option("--video", "video_paths", multiple=True, type=click.Path(exists=True, dir_okay=False),
                  help="Attach a video (MP4/WebM/MOV/MKV).")
    @click.option("--assets", "assets_file", type=click.Path(exists=True, dir_okay=False),
                  help="JSON file containing d3-turbo named assets.")
    @pass_app
    def decision(app: App, state, model, questions, request_file, output, image_paths, video_paths, assets_file):
        """Evaluate typed choice/noul/score questions. Pass STATE and --questions, or --file with a full request.

        Without STATE, reads the state from stdin. --file - reads a full request from stdin.\n        --image/--video attach base64 data URLs; --assets supplies named turbo assets.
        Provider authentication stays on the server; this command uses your InferencePort credentials.
        """
        app.require_login()
        try:
            if request_file:
                if state or questions:
                    raise IpxError("Use either --file or STATE with --questions.")
                if request_file == "-":
                    body = json.load(sys.stdin)
                else:
                    with open(request_file, encoding="utf-8") as f:
                        body = json.load(f)
                if not isinstance(body, dict):
                    raise IpxError("Request JSON must be an object.")
                body.setdefault("model", model)
                # An explicit -m always overrides the model in a request file.
                if click.get_current_context().get_parameter_source("model") == click.core.ParameterSource.COMMANDLINE:
                    body["model"] = model
            else:
                if not questions:
                    raise IpxError("Pass --questions FILE or --file REQUEST.json.")
                with open(questions, encoding="utf-8") as f:
                    named = json.load(f)
                if not isinstance(named, dict) or not named:
                    raise IpxError("Questions JSON must be a non-empty object.")
                if state is None or state == "-":
                    state = sys.stdin.read() if not sys.stdin.isatty() else ""
                body = {"model": model, "state": state, "questions": named}
            # --file preserves any existing images/videos/assets; flags add to them.
            if image_paths or video_paths:
                for field, files, kind in (("images", image_paths, "image"), ("videos", video_paths, "video")):
                    if files:
                        existing = body.setdefault(field, [])
                        if not isinstance(existing, list):
                            raise IpxError(f"{field} in the request must be an array.")
                        existing.extend(_media_data_url(path, kind) for path in files)
            if assets_file:
                with open(assets_file, encoding="utf-8") as stream:
                    named_assets = json.load(stream)
                if not isinstance(named_assets, dict):
                    raise IpxError("Assets JSON must be an object.")
                existing_assets = body.setdefault("assets", {})
                if not isinstance(existing_assets, dict):
                    raise IpxError("assets in the request must be an object.")
                existing_assets.update(named_assets)
            if not isinstance(body.get("state"), (str, dict, list)) or not body["state"]:
                raise IpxError("Provide a non-empty state.")
        except (OSError, ValueError) as e:
            raise IpxError("Could not read decision JSON.", str(e))
        result = app.api.request("POST", "/v1/systemone", json=body, timeout=120).json()
        if output:
            write_atomic(output, (json.dumps(result, indent=2) + "\n").encode("utf-8"))
        app.out(result, lambda data: ui.console.print_json(data=data))
