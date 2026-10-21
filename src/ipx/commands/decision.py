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


def _media_data_url(path: str, kind: str, limit: int) -> str:
    mime = _MEDIA_MIMES.get(Path(path).suffix.lower())
    if not mime or not mime.startswith(kind + "/"):
        raise IpxError(f"Unsupported {kind} extension: {path}")
    source = Path(path)
    size = source.stat().st_size
    if not 0 < size <= limit:
        raise IpxError(f"{kind} file must be nonempty and at most {limit} bytes: {path}")
    with source.open("rb") as stream:
        data = stream.read(limit + 1)
    if len(data) != size:
        raise IpxError(f"{kind} file changed or exceeds its size limit: {path}")
    return f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"


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
            # --file preserves existing media, subject to the same API limits.
            turbo = body.get("model") in ("vllm-sr-d3-turbo", "vllm-sr/d3-turbo", "vllm-sr/d3", "d3-turbo")
            image_limit = 16 * 1024 * 1024 if turbo else 8_000_000
            image_count_limit = 16 if turbo else 4
            current_images = body.get("images", [])
            current_videos = body.get("videos", [])
            current_assets = body.get("assets", {})
            if not isinstance(current_images, list) or not isinstance(current_videos, list) or not isinstance(current_assets, dict):
                raise IpxError("images/videos must be arrays and assets must be an object.")
            added_assets = {}
            if assets_file:
                with open(assets_file, encoding="utf-8") as stream:
                    added_assets = json.load(stream)
                if not isinstance(added_assets, dict):
                    raise IpxError("Assets JSON must be an object.")
            if len(current_images) + len(image_paths) + len({**current_assets, **added_assets}) > image_count_limit:
                raise IpxError(f"This model supports at most {image_count_limit} images including named assets.")
            if len(current_videos) + len(video_paths) > 1:
                raise IpxError("This model supports at most one video.")
            for path in image_paths:
                if not 0 < Path(path).stat().st_size <= image_limit:
                    raise IpxError(f"image file must be nonempty and at most {image_limit} bytes: {path}")
            for path in video_paths:
                if not 0 < Path(path).stat().st_size <= 32_000_000:
                    raise IpxError(f"video file must be nonempty and at most 32000000 bytes: {path}")
            total = len(json.dumps(body).encode("utf-8")) + len(json.dumps(added_assets).encode("utf-8"))
            total += sum(4 * ((Path(p).stat().st_size + 2) // 3) + 64 for p in (*image_paths, *video_paths))
            if total > 48 * 1024 * 1024:
                raise IpxError("Decision request exceeds the 48 MiB combined JSON limit.")
            if image_paths or video_paths:
                for field, files, kind in (("images", image_paths, "image"), ("videos", video_paths, "video")):
                    if files:
                        existing = body.setdefault(field, [])
                        if not isinstance(existing, list):
                            raise IpxError(f"{field} in the request must be an array.")
                        existing.extend(_media_data_url(path, kind, image_limit if kind == "image" else 32_000_000) for path in files)
            if assets_file:
                body.setdefault("assets", {}).update(added_assets)
            if len(json.dumps({"state": body.get("state"), "questions": body.get("questions")}).encode("utf-8")) > 1_000_000:
                raise IpxError("Decision state and questions exceed 1 MB.")
            if len(json.dumps(body).encode("utf-8")) > 48 * 1024 * 1024:
                raise IpxError("Decision request exceeds the 48 MiB combined JSON limit.")
            if not isinstance(body.get("state"), (str, dict, list)) or not body["state"]:
                raise IpxError("Provide a non-empty state.")
        except (OSError, ValueError) as e:
            raise IpxError("Could not read decision JSON.", str(e))
        result = app.api.request("POST", "/v1/systemone", json=body, timeout=120).json()
        if output:
            write_atomic(output, (json.dumps(result, indent=2) + "\n").encode("utf-8"))
        app.out(result, lambda data: ui.console.print_json(data=data))
