"""Builds the `ipx p2g` and `ipx gen` command groups from one definition.

Both tiers expose the same generation surface (chat, text, image, video, audio, models); they differ in what pays
for a request (wallet credits vs. subscription quota) and in a few extras (3D and deployments are P2G only).
"""

from __future__ import annotations

import sys
from typing import Any, Dict, Optional

import click
from rich.text import Text

from ipx import chat as chatmod
from ipx import cloud, ui
from ipx.app import App, pass_app
from ipx.clickx import IpxGroup
from ipx.cloud import GEN, P2G, Tier
from ipx.errors import IpxError

VIDEO_SIZES = ["1280x720", "720x1280", "1792x1024", "1024x1792"]
VIDEO_SECONDS = [4, 8, 12, 20, 30]


def _read_prompt(prompt: Optional[str], what: str = "prompt") -> str:
    if prompt and prompt != "-":
        return prompt
    if not sys.stdin.isatty():
        data = sys.stdin.read().strip()
        if data:
            return data
    raise IpxError(f"No {what} given.", "Pass it as an argument or pipe it in: echo \"…\" | ipx … -")


def _model_default(app: App, tier: Tier) -> Optional[str]:
    return app.cfg.get(tier.default_model_setting)


def build_group(tier: Tier) -> click.Group:
    cmd = tier.key
    section = "Cloud"
    pay = tier.billing

    @click.group(cmd, cls=IpxGroup, section=section,
                 short_help=f"{tier.title}: chat, image, video, audio{', 3D, embeddings, realtime' if tier is P2G else ''} ({'credits' if tier is P2G else 'subscription'})")
    @click.pass_context
    def group(ctx: click.Context) -> None:
        if tier is GEN and ctx.invoked_subcommand:
            from ipx import policy

            policy.assert_cloud(ctx.obj)  # organizations can restrict members to local models

    group.help = (
        f"{tier.title} cloud generation, billed against {pay}.\n\n"
        + ("Credit-metered API for production use: chat with any model, generate images, video, audio and 3D, create "
           "embeddings, evaluate typed decisions, run realtime voice sessions, manage "
           "keys and deployments. Needs a funded wallet (see `ipx p2g balance`). Set an organization context to bill "
           "your organization's wallet."
           if tier is P2G else
           "Lightning is the subscription-backed API behind the desktop app's cloud mode: chat through the smart "
           "router and generate images, video and audio within your plan's daily quotas (see `ipx usage get`).")
    )

    # -- chat ------------------------------------------------------------------
    @group.command("chat", examples=[f"ipx {cmd} chat", f"ipx {cmd} chat -m lightning --system \"You are terse.\""])
    @click.option("-m", "--model", help="Model id (default: lightning, the auto-router).")
    @click.option("--system", help="System prompt.")
    @pass_app
    def chat_(app: App, model: Optional[str], system: Optional[str]) -> None:
        """Interactive chat in your terminal with streaming Markdown."""
        app.require_login()
        model = model or _model_default(app, tier)
        backend = cloud.chat_backend(app, tier)
        note = f"Billed to organization {app.org}." if app.org else None
        chatmod.run_repl(
            title=f"{tier.title} chat", subtitle=f"billed to {'org ' + app.org if app.org else pay}",
            backend=backend, model=model or "lightning", system=system,
            list_models=lambda: cloud.model_ids(app, tier, "text"), footer_note=note)

    # -- text ------------------------------------------------------------------
    @group.command("text", examples=[f"ipx {cmd} text \"Summarize the Rust borrow checker\"",
                                     f"cat notes.md | ipx {cmd} text \"Turn these notes into a changelog\" --stdin",
                                     f"echo \"why is the sky blue\" | ipx {cmd} text -"])
    @click.argument("prompt", required=False)
    @click.option("-m", "--model", help="Model id.")
    @click.option("--system", help="System prompt.")
    @click.option("-f", "--file", "files", multiple=True, type=click.Path(exists=True, dir_okay=False),
                  help="Include a text file's contents (repeatable).")
    @click.option("--stdin", "use_stdin", is_flag=True, help="Append standard input to the prompt.")
    @click.option("--no-stream", is_flag=True, help="Wait for the whole answer before printing.")
    @pass_app
    def text_(app: App, prompt: Optional[str], model: Optional[str], system: Optional[str], files, use_stdin: bool, no_stream: bool) -> None:
        """One-off text generation. Pipe-friendly: the answer goes to stdout."""
        app.require_login()
        parts = []
        for f in files:
            with open(f, "r", encoding="utf-8", errors="replace") as fh:
                parts.append(f"--- {f} ---\n{fh.read()}")
        if use_stdin:
            parts.append(sys.stdin.read().strip())
        body = _read_prompt(prompt) if (prompt is None or prompt == "-") and not (parts and prompt is None) else prompt
        full = "\n\n".join([p for p in parts if p] + ([body] if body else []))
        if not full.strip():
            raise IpxError("No prompt given.", "Pass it as an argument or pipe it in with `-`.")
        chatmod.one_shot(backend=cloud.chat_backend(app, tier), prompt=full, model=model or _model_default(app, tier),
                         system=system, as_json=app.json, stream_out=not no_stream)

    # -- image -----------------------------------------------------------------
    @group.command("image", examples=[f"ipx {cmd} image \"a lighthouse at dawn, oil painting\" -o lighthouse.jpg",
                                      f"ipx {cmd} image \"same scene at night\" --ref lighthouse.jpg --mode fantasy"])
    @click.argument("prompt", required=False)
    @click.option("-n", "--count", type=click.IntRange(1, 4), default=1, help="Number of images (1-4).")
    @click.option("--mode", type=click.Choice(["fantasy", "realistic"]), help="Artistic or photorealistic style.")
    @click.option("-m", "--model", help="Model id (usually chosen automatically).")
    @click.option("--ref", "refs", multiple=True, help="Reference image path/URL for image-to-image (up to 2).")
    @click.option("-o", "--output", help="Output file or folder.")
    @pass_app
    def image(app: App, prompt: Optional[str], count: int, mode: Optional[str], model: Optional[str], refs, output: Optional[str]) -> None:
        """Generate images from a prompt."""
        app.require_login()
        if len(refs) > 2:
            raise IpxError("At most 2 reference images are supported.")
        prompt = _read_prompt(prompt)
        with ui.status("Generating image…"):
            images, credits, payload = cloud.generate_images(app, tier, prompt, n=count, mode=mode, refs=list(refs), model=model)
        paths = [cloud.save_bytes(app, "image", img, output, index=i, total=len(images)) for i, img in enumerate(images)]
        app.out({"files": paths, "credits_charged": credits}, lambda _: cloud.report(paths, kind="image", credits=credits))

    # -- video -----------------------------------------------------------------
    @group.command("video", examples=[f"ipx {cmd} video \"a drone flying over a canyon at sunset\" --seconds 8 -o canyon.mp4",
                                      f"ipx {cmd} video \"a slow zoom\" --no-wait      # prints a job id",
                                      f"ipx {cmd} video --status vid_a1b2c3d4e5f6",
                                      f"ipx {cmd} video --download vid_a1b2c3d4e5f6 -o clip.mp4"])
    @click.argument("prompt", required=False)
    @click.option("-m", "--model", help="Video model (default wan-fast).")
    @click.option("--size", type=click.Choice(VIDEO_SIZES), help="Output dimensions (default 1280x720).")
    @click.option("--seconds", type=click.Choice([str(s) for s in VIDEO_SECONDS]), help="Duration (default 8).")
    @click.option("--ref", help="Reference image (path/URL) for image-to-video.")
    @click.option("-o", "--output", help="Output file.")
    @click.option("--no-wait", is_flag=True, help="Submit and print the job id without waiting.")
    @click.option("--status", "status_id", metavar="ID", help="Show the status of a video job.")
    @click.option("--download", "download_id", metavar="ID", help="Download a finished video job.")
    @pass_app
    def video(app: App, prompt, model, size, seconds, ref, output, no_wait, status_id, download_id) -> None:
        """Generate video asynchronously with live progress."""
        app.require_login()
        if status_id:
            j = cloud.get_video(app, tier, status_id)
            app.out(j, lambda d: ui.console.print(ui.kv_panel(f"Video {status_id}", [
                ("Status", d.get("status")), ("Progress", f"{int(cloud._progress_fraction(d.get('progress')) * 100)}%"),
                ("Model", d.get("model")), ("Seconds", d.get("seconds")), ("Error", d.get("error"))])))
            return
        if download_id:
            dest = cloud.out_path(app, "video", output, ".mp4")
            n = cloud.download_video(app, tier, download_id, dest)
            app.out({"file": dest, "bytes": n}, lambda _: cloud.report(dest, kind="video", credits=None))
            return
        prompt = _read_prompt(prompt)
        with ui.status("Submitting video job…"):
            job = cloud.create_video(app, tier, prompt, model=model, size=size, seconds=int(seconds) if seconds else None, ref=ref)
        if no_wait:
            app.out(job, lambda d: (ui.ok(f"Video job queued: [bold]{d.get('id')}[/bold]"),
                                    ui.dim(f"Check it with `ipx {cmd} video --status {d.get('id')}`")))
            return
        ui.info(f"Job [bold]{job.get('id')}[/bold] queued. This can take a few minutes — Ctrl+C to stop waiting (the job keeps running).")
        final = cloud.wait_video(app, tier, job)
        dest = cloud.out_path(app, "video", output, ".mp4")
        cloud.download_video(app, tier, job["id"], dest)
        app.out({"file": dest, "id": job["id"]}, lambda _: cloud.report(dest, kind="video", credits=cloud.credits_from(None, final), model=final.get("model")))

    # -- audio -----------------------------------------------------------------
    @group.group("audio", cls=IpxGroup, short_help="Speech, sound effects, transcription and audio tools")
    def audio() -> None:
        """Audio generation and processing: text-to-speech, sound effects/music, transcription, voice tools."""

    @audio.command("speech", examples=[f"ipx {cmd} audio speech \"Welcome to InferencePort\" -o hello.mp3"])
    @click.argument("text", required=False)
    @click.option("-m", "--model", help="Speech model.")
    @click.option("--voice", help="Voice id/name.")
    @click.option("--format", "fmt", type=click.Choice(["mp3", "wav", "opus", "flac", "aac"]), help="Audio format.")
    @click.option("-o", "--output", help="Output file.")
    @pass_app
    def speech(app: App, text, model, voice, fmt, output) -> None:
        """Text-to-speech."""
        app.require_login()
        body: Dict[str, Any] = {"input": _read_prompt(text, "text")}
        for k, v in (("model", model), ("voice", voice), ("response_format", fmt)):
            if v:
                body[k] = v
        with ui.status("Synthesizing speech…"):
            r = app.api.request("POST", f"{tier.prefix}/audio/speech", json=body, timeout=300)
        paths = cloud.save_audio_response(app, "speech", r, output)
        app.out({"files": paths}, lambda _: cloud.report(paths, kind="audio", credits=cloud.credits_from(r)))

    @audio.command("sfx", examples=[f"ipx {cmd} audio sfx \"gentle rain and distant thunder\" --seconds 10 -o rain.mp3"])
    @click.argument("prompt", required=False)
    @click.option("--seconds", type=click.IntRange(1, 90), help="Duration in seconds.")
    @click.option("-m", "--model", help="Audio model.")
    @click.option("-o", "--output", help="Output file.")
    @pass_app
    def sfx(app: App, prompt, seconds, model, output) -> None:
        """Generate sound effects or music from a prompt."""
        app.require_login()
        body: Dict[str, Any] = {"prompt": _read_prompt(prompt)}
        if seconds:
            body["duration_seconds"] = seconds
        if model:
            body["model"] = model
        with ui.status("Generating audio…"):
            r = app.api.request("POST", f"{tier.prefix}/audio/generations", json=body, timeout=300)
        paths = cloud.save_audio_response(app, "audio", r, output)
        app.out({"files": paths}, lambda _: cloud.report(paths, kind="audio", credits=cloud.credits_from(r)))

    @audio.command("timestamps", short_help="Text-to-speech with word timestamps")
    @click.argument("text", required=False)
    @click.option("-m", "--model", help="Speech model.")
    @click.option("--voice", help="Voice id/name.")
    @click.option("-o", "--output", help="Output audio file (alignment JSON is saved beside it).")
    @pass_app
    def timestamps(app: App, text, model, voice, output) -> None:
        """Text-to-speech that also returns word-level timing."""
        app.require_login()
        body: Dict[str, Any] = {"input": _read_prompt(text, "text")}
        if model:
            body["model"] = model
        if voice:
            body["voice"] = voice
        with ui.status("Synthesizing speech with timestamps…"):
            r = app.api.request("POST", f"{tier.prefix}/audio/speech/with-timestamps", json=body, timeout=300)
        paths = cloud.save_audio_response(app, "speech", r, output)
        app.out({"files": paths}, lambda _: cloud.report(paths, kind="audio", credits=cloud.credits_from(r)))

    def _file_audio_cmd(name: str, endpoint: str, summary: str, kind: str) -> None:
        @audio.command(name, short_help=summary)
        @click.argument("file", type=click.Path(exists=True, dir_okay=False))
        @click.option("-m", "--model", help="Model id.")
        @click.option("--field", "fields", multiple=True, metavar="KEY=VALUE", help="Extra form field (repeatable).")
        @click.option("-o", "--output", help="Output file.")
        @pass_app
        def _cmd(app: App, file: str, model, fields, output) -> None:
            app.require_login()
            form: Dict[str, Any] = dict(f.split("=", 1) for f in fields if "=" in f)
            if model:
                form["model"] = model
            with ui.status(f"{summary}…"):
                r = app.api.request("POST", f"{tier.prefix}/audio/{endpoint}", form=form, files=cloud.read_file_spec(file), timeout=600)
            paths = cloud.save_audio_response(app, kind, r, output)
            app.out({"files": paths}, lambda _: cloud.report(paths, kind="audio", credits=cloud.credits_from(r)))

        _cmd.__doc__ = f"{summary} for an audio FILE (max 50 MB)."

    _file_audio_cmd("voice-change", "voice-changer", "Convert a recording to another voice", "voice")
    _file_audio_cmd("isolate", "voice-isolator", "Remove background noise and isolate the voice", "isolated")
    _file_audio_cmd("stems", "stem-separation", "Separate a track into stems", "stems")

    @audio.command("transcribe", examples=[f"ipx {cmd} audio transcribe meeting.mp3"])
    @click.argument("file", type=click.Path(exists=True, dir_okay=False))
    @click.option("-m", "--model", help="Transcription model.")
    @click.option("--language", help="Spoken language code (e.g. en).")
    @click.option("-o", "--output", help="Save the transcript to a file.")
    @pass_app
    def transcribe(app: App, file, model, language, output) -> None:
        """Speech-to-text for an audio FILE (max 50 MB)."""
        app.require_login()
        form: Dict[str, Any] = {}
        if model:
            form["model"] = model
        if language:
            form["language"] = language
        with ui.status("Transcribing…"):
            r = app.api.request("POST", f"{tier.prefix}/audio/transcriptions", form=form, files=cloud.read_file_spec(file), timeout=600)
        body = r.json() if r.content_type == "application/json" else {"text": r.text}
        text = (body or {}).get("text", r.text) if isinstance(body, dict) else str(body)
        if output:
            with open(output, "w", encoding="utf-8") as fh:
                fh.write(text)
            ui.ok(f"Saved transcript → [bold]{output}[/bold]")
        elif app.json:
            ui.emit_json(body)
        else:
            sys.stdout.write(text + "\n")

    # -- 3d ----------------------------------------------------------------------
    if tier is P2G:
        from ipx.commands import modalities
        from ipx.commands.decision import add_decision
        add_decision(group)
        modalities.add_embed(group)
        modalities.add_realtime(group)

        @group.command("3d", examples=["ipx p2g 3d --image chair.jpg --model tripoSR -o chair.glb",
                                       "ipx p2g 3d --image logo.png --model trellis2 --resolution high"])
        @click.option("--image", required=True, help="Input image (path or URL).")
        @click.option("-m", "--model", type=click.Choice(["tripoSR", "sf3d", "trellis2", "asset-harvester"]), default="tripoSR", show_default=True)
        @click.option("--prompt", help="Text prompt (required for asset-harvester).")
        @click.option("--resolution", type=click.Choice(["low", "medium", "high"]), help="Trellis 2 resolution.")
        @click.option("-o", "--output", help="Output file stem (e.g. chair.glb).")
        @click.option("--no-wait", is_flag=True, help="Submit and print the job id.")
        @click.option("--job", "job_id", metavar="ID", help="Fetch the result of an earlier 3D job.")
        @pass_app
        def three_d(app: App, image, model, prompt, resolution, output, no_wait, job_id) -> None:
            """Image-to-3D (GLB / Gaussian splat). Early access."""
            app.require_login()
            if model == "asset-harvester" and not prompt:
                raise IpxError("asset-harvester needs --prompt.")
            if job_id:
                j = app.api.request("GET", f"/v1/3d/jobs/{job_id}").json() or {}
                if j.get("status") != "completed":
                    app.out(j, lambda d: ui.info(f"Job {job_id} is {d.get('status')}."))
                    return
                import base64
                saved = []
                for item in j.get("data") or []:
                    for key, ext in cloud.OUT3D.items():
                        if item.get(key):
                            stem = output.rsplit(".", 1)[0] + ext if output else None
                            saved.append(cloud.save_bytes(app, "3d", base64.b64decode(item[key]), stem, ext))
                app.out({"files": saved}, lambda _: cloud.report(saved, kind="3D asset", credits=None))
                return
            final, saved = cloud.generate_3d(app, model=model, image=image, prompt=prompt, resolution=resolution,
                                             explicit=output, wait=not no_wait)
            if no_wait:
                app.out(final, lambda d: (ui.ok(f"3D job queued: [bold]{d.get('job_id')}[/bold]"),
                                          ui.dim(f"Fetch it later: ipx p2g 3d --image {image} --job {d.get('job_id')}")))
                return
            app.out({"files": saved}, lambda _: cloud.report(saved, kind="3D asset", credits=cloud.credits_from(None, final), model=model))
    else:
        @group.command("3d", short_help="3D generation (P2G only)")
        def three_d_info() -> None:
            """3D generation is only available through the Pay-2-Go API."""
            raise IpxError("3D generation is not part of Lightning subscriptions.",
                           "Use `ipx p2g 3d --image <file>` (billed from your credit wallet).")

        @group.command("embed", short_help="Embeddings (P2G only)")
        def embed_info() -> None:
            """Embeddings are only available through the Pay-2-Go API."""
            raise IpxError("Embeddings are not part of Lightning subscriptions.",
                           "Use `ipx p2g embed \"text\"` (billed per token from your credit wallet).")

        @group.command("realtime", short_help="Realtime voice (P2G only)")
        def realtime_info() -> None:
            """Realtime voice and transcription are only available through the Pay-2-Go API."""
            raise IpxError("Realtime sessions are not part of Lightning subscriptions.",
                           "Use `ipx p2g realtime chat` or `ipx p2g realtime transcribe` (billed from your credit wallet).")

    # -- models ------------------------------------------------------------------
    @group.command("models", examples=[f"ipx {cmd} models", f"ipx {cmd} models --type image"])
    @click.option("-t", "--type", "kind", type=click.Choice(list(cloud.MODEL_KINDS)), help="Only this modality.")
    @click.option("-s", "--search", help="Filter by text in id or name.")
    @pass_app
    def models(app: App, kind: Optional[str], search: Optional[str]) -> None:
        """List the cloud models available to you."""
        app.require_login()
        if kind in cloud.P2G_ONLY_KINDS and tier is not P2G:
            raise IpxError(f"{kind.title()} models are only available through the Pay-2-Go API.",
                           f"Use `ipx p2g models --type {kind}`.")
        items = cloud.list_models(app, tier, kind)
        if search:
            s = search.lower()
            items = [m for m in items if s in str(m.get("id", "")).lower() or s in str(m.get("name", "")).lower()]

        def render(_: Any) -> None:
            rows = []
            for m in items:
                mods = "/".join(m.get("input_modalities") or []) + (" → " + "/".join(m["output_modalities"]) if m.get("output_modalities") else "")
                ctx = m.get("context_length")
                price = m.get("pricing") or {}
                p = price.get("prompt") if isinstance(price, dict) else None
                rows.append((m.get("id"), m.get("name"), mods.strip(), ui.fmt_num(ctx) if ctx else None,
                             f"${float(p) * 1_000_000:.2f}/M" if p not in (None, "", "0") and _isnum(p) and float(p) > 0 else None))
            ui.console.print(ui.make_table(["ID", "Name", "Modalities", "Context", "Input price"], rows,
                                           title=f"{tier.title} models ({len(rows)})", right=["Context", "Input price"]))

        app.out(items, render)

    if tier is P2G:
        _add_p2g_extras(group)
    else:
        _add_gen_extras(group)
    return group


def _isnum(v: Any) -> bool:
    try:
        float(v)
        return True
    except (TypeError, ValueError):
        return False


# --------------------------------------------------------------------------- P2G extras


def _add_p2g_extras(group: click.Group) -> None:
    @group.command("prices", short_help="Compare model prices across modalities",
                   examples=["ipx p2g prices", "ipx p2g prices --type image --sort price", "ipx p2g prices --free",
                             "ipx p2g prices -s gemini --json"])
    @click.option("-t", "--type", "kind", type=click.Choice(list(cloud.MODEL_KINDS)), help="Only this modality.")
    @click.option("-s", "--search", help="Filter by text in id or name.")
    @click.option("--sort", "sort_by", type=click.Choice(["price", "name"]), default="price", show_default=True,
                  help="Order within each modality.")
    @click.option("--free", is_flag=True, help="Only free models.")
    @click.option("--max-price", type=float, help="Only models whose (input) rate is at most this many USD per unit.")
    @pass_app
    def prices(app: App, kind: Optional[str], search: Optional[str], sort_by: str, free: bool,
                max_price: Optional[float]) -> None:
        """Compare prices across every Pay-2-Go model.

        Rates are normalised per unit: per million tokens for text and embeddings, per image, per second of video or
        audio, per model for 3D. The catalog is public, so no sign-in is needed.
        """
        from ipx import pricing as pricing_mod

        data = app.api.get("/v1/models", auth=False) or {}
        items = data.get("data") if isinstance(data, dict) else data
        listed = pricing_mod.rows(items or [], kind=kind, search=search, free=free, max_price=max_price, sort=sort_by)

        def render(_: Any) -> None:
            if not listed:
                ui.info("No models match those filters.")
                return
            table_rows = [(r["kind"], r["id"], r["name"], r["price"]) for r in listed]
            ui.console.print(ui.make_table(["Type", "ID", "Name", "Price"], table_rows,
                                           title=f"Pay-2-Go pricing ({len(listed)} models)"))
            ui.dim("  Prices in USD. Check your wallet with `ipx p2g balance`.")

        app.out(listed, render)

    @group.command("balance", examples=["ipx p2g balance"])
    @pass_app
    def balance(app: App) -> None:
        """Show your wallet balance and spend by modality."""
        app.require_login()
        d = app.api.get("/v1/me") or {}

        def render(d: Dict[str, Any]) -> None:
            w, ident, u = d.get("wallet") or {}, d.get("identity") or {}, d.get("usage_summary") or {}
            bal = Text.assemble((ui.fmt_credits(w.get("balance_credits")), f"bold {ui.OK}"), (" credits", "muted"))
            who = Text(f"{ident.get('email')}", style="white")
            if app.org:
                who.append(f"  ·  org {app.org}", style=ui.VIOLET)
            ui.console.print(ui.kv_panel("Pay-2-Go wallet", [
                ("Account", who), ("Balance", bal),
                ("Purchased", ui.fmt_credits(w.get("total_purchased"))), ("Spent", ui.fmt_credits(w.get("total_spent")))]))
            rows = []
            for kind, unit in (("text", "total_tokens"), ("image", "total_images"), ("video", "total_seconds"), ("audio", "total_seconds")):
                x = u.get(kind) or {}
                rows.append((kind.title(), ui.fmt_num(x.get(unit, 0)) + " " + unit.split("_")[1], ui.fmt_credits(x.get("total_credits", 0))))
            ui.console.print(ui.make_table(["Modality", "Usage", "Credits"], rows, right=["Usage", "Credits"]))

        app.out(d, render)

    @group.command("ledger")
    @click.option("--limit", type=click.IntRange(1, 500), default=25, show_default=True)
    @pass_app
    def ledger(app: App, limit: int) -> None:
        """Recent credit ledger entries (purchases, usage, refunds)."""
        app.require_login()
        d = app.api.get("/v1/credits/ledger", params={"limit": limit}) or {}

        def render(d: Dict[str, Any]) -> None:
            rows = []
            for e in d.get("entries") or []:
                delta = float(e.get("delta_credits") or 0)
                rows.append(((e.get("created_at") or "")[:19].replace("T", " "), e.get("entry_type"), e.get("usage_kind"),
                             Text(("+" if delta > 0 else "") + ui.fmt_credits(delta), style=ui.OK if delta > 0 else ui.WARN), e.get("source")))
            ui.console.print(ui.make_table(["When", "Type", "Kind", "Credits", "Source"], rows, title="Credit ledger", right=["Credits"]))

        app.out(d, render)

    @group.command("pricing")
    @pass_app
    def pricing(app: App) -> None:
        """Credit prices and top-up packs (no sign-in needed)."""
        d = app.api.get("/v1/config", auth=False, org=False) or {}

        def render(d: Dict[str, Any]) -> None:
            p = d.get("pricing") or {}
            rows = [("Text", f"{p.get('textCreditPerMillionTokens')} credits / 1M tokens"), ("Image", f"{p.get('imageCreditPerImage')} credits / image"),
                    ("Video", f"{p.get('videoCreditPerSecond')} credits / second"), ("Audio", f"{p.get('audioCreditPerSecond')} credits / second"),
                    ("Speech", f"{p.get('audioCreditPer1kChars')} credits / 1k characters"), ("3D", f"from {p.get('model3dCreditPerModel')} credits / model")]
            lim = d.get("limits") or {}
            if lim.get("requestsPerMinutePerIdentity"):
                rows.append(("Rate limit", f"{lim['requestsPerMinutePerIdentity']} requests / minute"))
            ui.console.print(ui.kv_panel("P2G pricing", rows, subtitle="live values: console.inferenceport.ai"))
            packs = d.get("billing", {}).get("packs") or []
            if packs:
                ui.console.print(ui.make_table(["Pack", "Price", "Credits"], [(k.get("label"), f"${float(k.get('amountUsd', 0)):,.0f}", ui.fmt_credits(k.get("credits"))) for k in packs]))

        app.out(d, render)

    @group.group("keys", cls=IpxGroup, short_help="Manage your personal API keys")
    def keys() -> None:
        """Create, list and revoke personal Lightning API keys (ipa_live_…)."""

    @keys.command("list")
    @pass_app
    def keys_list(app: App) -> None:
        """List API keys."""
        app.require_login()
        d = app.api.get("/v1/lightning-api-keys", org=False) or {}
        rows = [(k.get("id"), k.get("name"), k.get("keyPrefix"), (k.get("lastUsedAt") or "never")[:10],
                 "revoked" if k.get("isRevoked") else ("expired" if k.get("isExpired") else "active")) for k in d.get("items") or []]
        app.out(d, lambda _: ui.console.print(ui.make_table(["ID", "Name", "Prefix", "Last used", "State"], rows, title="API keys")))

    @keys.command("create", examples=["ipx p2g keys create ci-bot --expires 2027-01-01"])
    @click.argument("name")
    @click.option("--expires", help="Expiry as an ISO-8601 date/time.")
    @pass_app
    def keys_create(app: App, name: str, expires: Optional[str]) -> None:
        """Create an API key. The secret is shown once."""
        app.require_login()
        body: Dict[str, Any] = {"name": name}
        if expires:
            body["expiresAt"] = expires if "T" in expires else expires + "T00:00:00Z"
        d = app.api.post("/v1/lightning-api-keys", body, org=False) or {}

        def render(d: Dict[str, Any]) -> None:
            ui.ok(f"Created key [bold]{name}[/bold]")
            ui.console.print(ui.kv_panel("Save this key now — it is shown only once", [("Key", Text(d.get("rawKey", ""), style="bold " + ui.WARN))]))

        app.out(d, render)

    @keys.command("revoke")
    @click.argument("key_id")
    @pass_app
    def keys_revoke(app: App, key_id: str) -> None:
        """Revoke an API key immediately."""
        app.require_login()
        if not ui.confirm(f"Revoke key {key_id}? Anything using it stops working."):
            return
        app.api.delete(f"/v1/lightning-api-keys/{key_id}", org=False)
        app.out({"revoked": key_id}, lambda _: ui.ok(f"Revoked [bold]{key_id}[/bold]"))

    @group.group("deploy", cls=IpxGroup, short_help="Dedicated model deployments")
    def deploy() -> None:
        """Deploy and manage dedicated model endpoints billed through your wallet."""

    @deploy.command("models")
    @pass_app
    def deploy_models(app: App) -> None:
        """Models that can be deployed."""
        app.require_login()
        d = app.api.get("/v1/deploy/models") or {}
        items = d.get("models") or d.get("data") or d.get("items") or (d if isinstance(d, list) else [])
        app.out(d, lambda _: ui.console.print(ui.make_table(["ID", "Name"], [(m.get("id"), m.get("name")) for m in items if isinstance(m, dict)], title="Deployable models")))

    @deploy.command("list")
    @pass_app
    def deploy_list(app: App) -> None:
        """Your deployments."""
        app.require_login()
        d = app.api.get("/v1/deploy") or {}
        items = d.get("deployments") or d.get("items") or (d if isinstance(d, list) else [])
        app.out(d, lambda _: ui.console.print(ui.make_table(["ID", "Model", "Status"], [(x.get("id"), x.get("model_id"), x.get("status")) for x in items if isinstance(x, dict)], title="Deployments")))

    @deploy.command("status")
    @click.argument("model_id")
    @pass_app
    def deploy_status(app: App, model_id: str) -> None:
        """Availability of a model for deployment."""
        app.require_login()
        d = app.api.get(f"/v1/deploy/model-status/{model_id}") or {}
        app.out(d, lambda x: ui.console.print(ui.kv_panel(model_id, [(k, v) for k, v in x.items() if not isinstance(v, (dict, list))])))

    @deploy.command("create")
    @click.argument("model_id")
    @pass_app
    def deploy_create(app: App, model_id: str) -> None:
        """Deploy a model."""
        app.require_login()
        d = app.api.post("/v1/deploy", {"model_id": model_id}) or {}
        app.out(d, lambda _: ui.ok(f"Deployment requested for [bold]{model_id}[/bold]"))

    for name, method, verb in (("get", "GET", ""), ("usage", "GET", "/usage"), ("cancel", "POST", "/cancel"), ("uncancel", "POST", "/uncancel"), ("delete", "DELETE", "")):
        def _make(name=name, method=method, verb=verb) -> None:
            @deploy.command(name, short_help=f"{name.title()} a deployment")
            @click.argument("deployment_id")
            @pass_app
            def _c(app: App, deployment_id: str) -> None:
                app.require_login()
                if name == "delete" and not ui.confirm(f"Delete deployment {deployment_id}?"):
                    return
                r = app.api.request(method, f"/v1/deploy/{deployment_id}{verb}", json={} if method == "POST" else None)
                d = r.json() if r.body else {}
                app.out(d, lambda x: ui.console.print(ui.kv_panel(f"Deployment {deployment_id}", [(k, v) for k, v in (x or {}).items() if not isinstance(v, (dict, list))])) if x else ui.ok(f"{name} done"))

        _make()


# --------------------------------------------------------------------------- Lightning extras


def _add_gen_extras(group: click.Group) -> None:
    @group.command("plan")
    @pass_app
    def plan(app: App) -> None:
        """Your plan, and the plans you could upgrade to."""
        app.require_login()
        sub = app.api.get("/subscription", org=False) or {}
        tiers = (app.api.get("/tiers", auth=False, org=False) or {})

        def render(_: Any) -> None:
            ui.console.print(ui.kv_panel("Your plan", [("Account", sub.get("email")), ("Plan", Text(str(sub.get("plan_name") or sub.get("plan_key")), style="bold " + ui.VIOLET)),
                                                    ("Member since", (sub.get("signed_up") or "")[:10])]))
            items = tiers.get("plans") or tiers.get("tiers") or (tiers if isinstance(tiers, list) else [])
            if items:
                ui.console.print(ui.make_table(["Plan", "Price / mo", "Upgrade"], [(t.get("name"), t.get("price"), t.get("url")) for t in items if isinstance(t, dict)], title="Upgrade options"))

        app.out({"subscription": sub, "tiers": tiers}, render)


p2g_group = build_group(P2G)
gen_group = build_group(GEN)
