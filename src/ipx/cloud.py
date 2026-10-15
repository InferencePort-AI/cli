"""Cloud generation primitives shared by the P2G (/v1, credit-billed) and Lightning (/gen, subscription) tiers."""

from __future__ import annotations

import base64
import mimetypes
import os
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, Iterator, List, Optional, Tuple

from ipx import ui
from ipx.app import App
from ipx.chat import Event, parse_openai_sse
from ipx.errors import IpxError
from ipx.http import FileSpec, Response, guess_type, poll


@dataclass(frozen=True)
class Tier:
    key: str          # "p2g" | "gen"
    prefix: str       # URL prefix on the API
    title: str
    billing: str      # human description of what pays for requests
    default_model_setting: str


P2G = Tier("p2g", "/v1", "Pay-2-Go", "credits from your wallet", "default_p2g_model")
GEN = Tier("gen", "/gen", "Lightning", "your subscription quotas", "default_gen_model")

MODEL_KINDS = {"text": "text", "image": "images", "video": "videos", "audio": "audio", "3d": "3d"}
MIME_EXT = {
    "image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp", "image/gif": ".gif",
    "video/mp4": ".mp4", "video/webm": ".webm",
    "audio/mpeg": ".mp3", "audio/mp3": ".mp3", "audio/wav": ".wav", "audio/x-wav": ".wav", "audio/ogg": ".ogg",
    "audio/flac": ".flac", "audio/opus": ".opus", "audio/aac": ".aac", "audio/mp4": ".m4a",
    "model/gltf-binary": ".glb", "application/zip": ".zip", "application/json": ".json",
}


# --------------------------------------------------------------------------- files


def sniff_ext(data: bytes, fallback: str = ".bin") -> str:
    if data[:3] == b"\xff\xd8\xff":
        return ".jpg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return ".png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    if data[:4] == b"RIFF" and data[8:12] == b"WAVE":
        return ".wav"
    if data[4:8] == b"ftyp":
        return ".mp4"
    if data[:3] == b"ID3" or data[:2] in (b"\xff\xfb", b"\xff\xf3", b"\xff\xf2"):
        return ".mp3"
    if data[:4] == b"OggS":
        return ".ogg"
    if data[:4] == b"fLaC":
        return ".flac"
    if data[:4] == b"glTF":
        return ".glb"
    if data[:2] == b"PK":
        return ".zip"
    return fallback


def out_path(app: App, kind: str, explicit: Optional[str], ext: str, index: int = 0, total: int = 1) -> str:
    """Decide where to write generated media."""
    if explicit:
        explicit = os.path.expanduser(explicit)
        if os.path.isdir(explicit) or explicit.endswith(("/", "\\")):
            os.makedirs(explicit, exist_ok=True)
            base = os.path.join(explicit, f"{kind}-{datetime.now():%Y%m%d-%H%M%S}")
        else:
            root, e = os.path.splitext(explicit)
            base = root
            ext = e or ext
            os.makedirs(os.path.dirname(os.path.abspath(explicit)), exist_ok=True)
    else:
        folder = os.path.expanduser(app.cfg.get("output_dir") or ".")
        os.makedirs(folder, exist_ok=True)
        base = os.path.join(folder, f"{kind}-{datetime.now():%Y%m%d-%H%M%S}")
    if total > 1:
        base += f"-{index + 1}"
    path = base + ext
    n = 1
    while os.path.exists(path) and not explicit:
        path = f"{base}-{n}{ext}"
        n += 1
    return path


def save_bytes(app: App, kind: str, data: bytes, explicit: Optional[str], ext: Optional[str] = None,
               index: int = 0, total: int = 1) -> str:
    path = out_path(app, kind, explicit, ext or sniff_ext(data), index, total)
    with open(path, "wb") as fh:
        fh.write(data)
    return path


def ref_to_url(ref: str) -> str:
    """Turn a local path into a data URI; pass URLs and data URIs through."""
    if ref.startswith(("http://", "https://", "data:")):
        return ref
    path = os.path.expanduser(ref)
    if not os.path.isfile(path):
        raise IpxError(f"Reference image not found: {ref}", "Pass an existing file path or an https:// URL.")
    mime = mimetypes.guess_type(path)[0] or "image/png"
    with open(path, "rb") as fh:
        return f"data:{mime};base64,{base64.b64encode(fh.read()).decode()}"


def read_file_spec(path: str, field: str = "file") -> Dict[str, FileSpec]:
    path = os.path.expanduser(path)
    if not os.path.isfile(path):
        raise IpxError(f"File not found: {path}")
    with open(path, "rb") as fh:
        return {field: (os.path.basename(path), fh.read(), guess_type(path))}


def credits_from(resp: Optional[Response], body: Any = None) -> Optional[float]:
    if resp is not None:
        h = resp.headers.get("x-payg-credits-charged")
        if h:
            try:
                return float(h)
            except ValueError:
                pass
    if isinstance(body, dict):
        u = body.get("usage") or {}
        if isinstance(u, dict) and u.get("payg_credits_charged") is not None:
            return float(u["payg_credits_charged"])
    return None


def report(path_or_paths: Any, *, kind: str, credits: Optional[float], model: Optional[str] = None) -> None:
    paths = path_or_paths if isinstance(path_or_paths, list) else [path_or_paths]
    for p in paths:
        size = ui.fmt_bytes(os.path.getsize(p))
        ui.ok(f"Saved {kind} → [bold]{p}[/bold] [muted]({size})[/muted]")
    bits = []
    if model:
        bits.append(model)
    if credits is not None:
        bits.append(f"{ui.fmt_credits(credits)} credits charged")
    if bits:
        ui.dim("  " + " · ".join(bits))


# --------------------------------------------------------------------------- chat


def chat_backend(app: App, tier: Tier, default_model: Optional[str] = None, **extra: Any):
    def backend(messages: List[Dict[str, Any]], model: Optional[str]) -> Iterator[Event]:
        body = {"messages": messages, "stream": True, "model": model or default_model or "lightning"}
        body.update(extra)
        s = app.api.request("POST", f"{tier.prefix}/chat/completions", json=body, stream=True, timeout=600)
        try:
            yield from parse_openai_sse(s)
        finally:
            s.close()

    return backend


def list_models(app: App, tier: Tier, kind: Optional[str] = None) -> List[Dict[str, Any]]:
    path = f"{tier.prefix}/models" if not kind else f"{tier.prefix}/{MODEL_KINDS[kind]}/models"
    data = app.api.get(path) or {}
    items = data.get("data") if isinstance(data, dict) else data
    return [m for m in (items or []) if isinstance(m, dict)]


def model_ids(app: App, tier: Tier, kind: str = "text") -> List[str]:
    return [str(m.get("id")) for m in list_models(app, tier, kind) if m.get("id")]


# --------------------------------------------------------------------------- image


def generate_images(app: App, tier: Tier, prompt: str, *, n: int, mode: Optional[str], refs: List[str],
                    model: Optional[str]) -> Tuple[List[bytes], Optional[float], Dict[str, Any]]:
    body: Dict[str, Any] = {"prompt": prompt, "n": n, "response_format": "b64_json"}
    if mode:
        body["mode"] = mode
    if model:
        body["model"] = model
    if refs:
        body["image_urls"] = [ref_to_url(r) for r in refs]
    resp = app.api.request("POST", f"{tier.prefix}/images/generations", json=body, timeout=300)
    payload = resp.json() or {}
    images: List[bytes] = []
    for item in payload.get("data") or []:
        if item.get("b64_json"):
            images.append(base64.b64decode(item["b64_json"]))
        elif item.get("url"):
            r = app.api.request("GET", item["url"], headers={"Accept": "*/*"}, auth=False)
            images.append(r.body)
    if not images:
        raise IpxError("The service returned no images.", str(payload)[:300])
    return images, credits_from(resp, payload), payload


# --------------------------------------------------------------------------- video (async)


def _interval(default: float) -> float:
    """Polling interval; IPX_POLL_INTERVAL overrides it (used by tests)."""
    try:
        return float(os.environ.get("IPX_POLL_INTERVAL") or default)
    except ValueError:
        return default


def _progress_fraction(v: Any) -> float:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return 0.0
    return f / 100.0 if f > 1.0 else f


def create_video(app: App, tier: Tier, prompt: str, *, model: Optional[str], size: Optional[str],
                 seconds: Optional[int], ref: Optional[str]) -> Dict[str, Any]:
    body: Dict[str, Any] = {"prompt": prompt}
    if model:
        body["model"] = model
    if size:
        body["size"] = size
    if seconds:
        body["seconds"] = seconds
    if ref:
        body["input_reference"] = {"image_url": ref_to_url(ref)}
    return app.api.post(f"{tier.prefix}/videos", body, timeout=120) or {}


def get_video(app: App, tier: Tier, video_id: str) -> Dict[str, Any]:
    return app.api.get(f"{tier.prefix}/videos/{video_id}") or {}


def wait_video(app: App, tier: Tier, job: Dict[str, Any], *, interval: float = 5.0, timeout: float = 1800.0) -> Dict[str, Any]:
    vid = job["id"]
    interval = _interval(interval)
    with ui.job_progress() as prog:
        task = prog.add_task(f"Rendering video {vid}", total=100)

        def tick(j: Dict[str, Any]) -> None:
            label = {"pending": "Queued", "processing": "Rendering"}.get(str(j.get("status")), str(j.get("status")))
            prog.update(task, completed=_progress_fraction(j.get("progress")) * 100, description=f"{label} · {vid}")

        final = poll(lambda: get_video(app, tier, vid), done=lambda j: j.get("status") in ("completed", "failed"),
                     interval=interval, timeout=timeout, on_tick=tick)
        if final.get("status") == "completed":
            prog.update(task, completed=100, description=f"Rendered · {vid}")
    if final.get("status") == "failed":
        err = final.get("error")
        msg = err.get("message") if isinstance(err, dict) else err
        raise IpxError(f"Video generation failed: {msg or 'unknown error'}", "Credits are refunded automatically on failure.")
    return final


def download_video(app: App, tier: Tier, video_id: str, dest: str) -> int:
    return app.api.download(f"{tier.prefix}/videos/{video_id}/content", dest, label="Downloading video")


# --------------------------------------------------------------------------- audio


def save_audio_response(app: App, kind: str, resp: Response, explicit: Optional[str]) -> List[str]:
    """Persist whatever an audio endpoint returned: raw bytes, or JSON carrying base64 audio."""
    ctype = resp.content_type
    if ctype == "application/json" or resp.body[:1] in (b"{", b"["):
        body = resp.json()
        b64 = None
        if isinstance(body, dict):
            for key in ("audio_base64", "audio_b64", "b64_json", "audio"):
                if isinstance(body.get(key), str):
                    b64 = body[key]
                    break
            if b64 is None and isinstance(body.get("data"), list) and body["data"]:
                first = body["data"][0]
                b64 = first.get("b64_json") or first.get("audio_base64") if isinstance(first, dict) else None
        if b64:
            paths = [save_bytes(app, kind, base64.b64decode(b64), explicit)]
            meta = {k: v for k, v in body.items() if not isinstance(v, str) or len(v) < 2000}
            if len(meta) > 1:
                import json

                side = os.path.splitext(paths[0])[0] + ".json"
                with open(side, "w", encoding="utf-8") as fh:
                    json.dump(meta, fh, indent=2)
                paths.append(side)
            return paths
        raise IpxError("The service returned JSON but no audio.", resp.text[:300])
    ext = MIME_EXT.get(ctype) or sniff_ext(resp.body, ".mp3")
    return [save_bytes(app, kind, resp.body, explicit, ext)]


# --------------------------------------------------------------------------- 3D (P2G only)

OUT3D = {"model_glb_b64_bytes": ".glb", "model_ply_b64_bytes": ".ply", "orbit_video_b64_bytes": ".mp4"}


def generate_3d(app: App, *, model: str, image: str, prompt: Optional[str], resolution: Optional[str],
                explicit: Optional[str], wait: bool = True) -> Tuple[Dict[str, Any], List[str]]:
    body: Dict[str, Any] = {"model": model, "image_urls": [ref_to_url(image)]}
    if prompt:
        body["prompt"] = prompt
    if resolution:
        body["resolution"] = resolution
    job = app.api.post("/v1/3d/generations", body, timeout=120) or {}
    if not wait:
        return job, []
    job_id = job.get("job_id")
    if not job_id:
        raise IpxError("The service did not return a job id.", str(job)[:300])
    with ui.job_progress() as prog:
        task = prog.add_task(f"3D job {job_id[:10]}…", total=None)

        def fetch() -> Dict[str, Any]:
            return app.api.request("GET", f"/v1/3d/jobs/{job_id}").json() or {}

        final = poll(fetch, done=lambda j: j.get("status") in ("completed", "failed"), interval=_interval(6.0), timeout=1800.0,
                     on_tick=lambda j: prog.update(task, description=f"{str(j.get('status', 'pending')).title()} · {job_id[:10]}…"))
        prog.update(task, total=1, completed=1)
    if final.get("status") == "failed":
        raise IpxError(f"3D generation failed: {final.get('error')}", "Credits are refunded automatically on failure.")
    saved: List[str] = []
    stem = os.path.splitext(os.path.expanduser(explicit))[0] if explicit else None
    for item in final.get("data") or []:
        for key, ext in OUT3D.items():
            if item.get(key):
                target = (stem + ext) if stem else None
                saved.append(save_bytes(app, "3d", base64.b64decode(item[key]), target, ext))
    return final, saved


def now() -> float:
    return time.time()
