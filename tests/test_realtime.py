"""`ipx p2g embed` and `ipx p2g realtime ...` against a fake API and a real local WebSocket server."""

import asyncio
import base64
import json
import struct
import threading
import time
import wave

import pytest
from conftest import sign_in

websockets = pytest.importorskip("websockets")
from websockets.asyncio.server import serve  # noqa: E402
from websockets.http11 import Response  # noqa: E402

from ipx import realtime as rt  # noqa: E402

# --------------------------------------------------------------------------- helpers


def make_wav(path, seconds=1.0, rate=16000, channels=1):
    frames = b"".join(struct.pack("<h", int(8000 * ((i % 50) / 50 - 0.5))) for i in range(int(rate * seconds))) * 1
    if channels == 2:
        frames = b"".join(frames[i:i + 2] * 2 for i in range(0, len(frames), 2))
    with wave.open(str(path), "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(frames)
    return path


class WsServer:
    """A real WebSocket server on a background thread; ``script`` decides how each connection behaves."""

    def __init__(self, script, reject=None, catalog=None):
        self.script = script
        self.reject = reject
        self.catalog = catalog
        self.conns = []
        self.loop = asyncio.new_event_loop()
        self._ready = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()
        self._ready.wait(10)

    def _run(self):
        asyncio.set_event_loop(self.loop)

        async def handler(ws):
            rec = {"path": ws.request.path, "headers": {k.lower(): v for k, v in ws.request.headers.items()}, "subprotocol": ws.subprotocol, "frames": []}
            self.conns.append(rec)
            await self.script(ws, rec)

        def process_request(connection, request):
            if self.catalog is not None and request.path.startswith("/v1/realtime/models"):
                body = json.dumps({"object": "list", "data": self.catalog}).encode()
                return Response(200, "OK", websockets.Headers({"Content-Type": "application/json", "Content-Length": str(len(body))}), body)
            rejected = self.reject(request) if callable(self.reject) else self.reject
            if rejected:
                status, body = rejected
                return Response(status, "Rejected", websockets.Headers({"Content-Type": "application/json"}), json.dumps(body).encode())
            return None

        async def main():
            self.server = await serve(handler, "127.0.0.1", 0, subprotocols=["realtime"], process_request=process_request, max_size=None)
            self.port = self.server.sockets[0].getsockname()[1]
            self._ready.set()
            await self.server.serve_forever()

        try:
            self.loop.run_until_complete(main())
        except Exception:
            pass

    @property
    def url(self):
        return f"http://127.0.0.1:{self.port}"

    def close(self):
        self.loop.call_soon_threadsafe(self.server.close)
        self.thread.join(5)


@pytest.fixture()
def wsserver():
    made = []

    def make(script, reject=None, catalog=None):
        s = WsServer(script, reject, catalog)
        made.append(s)
        return s

    yield make
    for s in made:
        s.close()


def response_done(inp=3, out=5):
    return json.dumps({"type": "response.done", "response": {"usage": {"input_tokens": inp, "output_tokens": out}}})


async def chat_script(ws, rec):
    async for raw in ws:
        ev = json.loads(raw)
        rec["frames"].append(ev)
        if ev["type"] == "response.create":
            await ws.send(json.dumps({"type": "response.output_text.delta", "delta": "Hi "}))
            await ws.send(json.dumps({"type": "response.output_text.delta", "delta": "there"}))
            pcm = struct.pack("<4h", 1, 2, 3, 4)
            await ws.send(json.dumps({"type": "response.output_audio.delta", "delta": base64.b64encode(pcm).decode()}))
            await ws.send(response_done())


# --------------------------------------------------------------------------- embed


def embed_body(n, dims=3):
    return {"object": "list", "model": "text-embedding-3-small",
            "data": [{"object": "embedding", "index": i, "embedding": [0.1 * (i + 1)] * dims} for i in range(n)],
            "usage": {"prompt_tokens": 7, "total_tokens": 7, "payg_credits_charged": 0.0001}}


def test_embed_single_text(ipx):
    sign_in(ipx)
    ipx.fake.route("POST", "/v1/embeddings", body=embed_body(1))
    r = ipx("p2g", "embed", "hello world")
    assert r.exit_code == 0, r.output
    sent = ipx.fake.last("/v1/embeddings")
    assert sent["json"] == {"model": "text-embedding-3-small", "input": "hello world"}
    assert sent["headers"]["authorization"] == "Bearer tok_abc"
    assert "7 tokens" in r.output and "0.0001 credits charged" in r.output


def test_embed_many_with_options_and_json(ipx):
    sign_in(ipx)
    ipx.fake.route("POST", "/v1/embeddings", body=embed_body(2))
    r = ipx("--json", "p2g", "embed", "a", "b", "-m", "text-embedding-3-large", "--dimensions", "256")
    assert json.loads(r.output)["usage"]["payg_credits_charged"] == 0.0001
    assert ipx.fake.last("/v1/embeddings")["json"] == {"model": "text-embedding-3-large", "input": ["a", "b"], "dimensions": 256}


def test_embed_file_lines_and_output(ipx, tmp_path):
    sign_in(ipx)
    ipx.fake.route("POST", "/v1/embeddings", body=embed_body(3))
    src = tmp_path / "docs.txt"
    src.write_text("one\n\ntwo\nthree\n")
    out = tmp_path / "vec.json"
    r = ipx("p2g", "embed", "-f", str(src), "--lines", "-o", str(out))
    assert r.exit_code == 0, r.output
    assert ipx.fake.last("/v1/embeddings")["json"]["input"] == ["one", "two", "three"]
    assert json.loads(out.read_text())["data"][2]["index"] == 2


def test_embed_reads_stdin(ipx):
    sign_in(ipx)
    ipx.fake.route("POST", "/v1/embeddings", body=embed_body(2))
    ipx("p2g", "embed", "--lines", "--stdin", input="alpha\nbeta\n")
    assert ipx.fake.last("/v1/embeddings")["json"]["input"] == ["alpha", "beta"]


def test_embed_count_mismatch_is_an_error(ipx):
    from ipx.errors import IpxError

    sign_in(ipx)
    ipx.fake.route("POST", "/v1/embeddings", body=embed_body(1))
    with pytest.raises(IpxError) as e:
        ipx("p2g", "embed", "a", "b")
    assert "unexpected number" in str(e.value)


def test_embed_requires_input(ipx):
    from ipx.errors import IpxError

    sign_in(ipx)
    with pytest.raises(IpxError):
        ipx("p2g", "embed")


def test_embed_surfaces_insufficient_credits(ipx):
    from ipx.errors import ApiError

    sign_in(ipx)
    ipx.fake.route("POST", "/v1/embeddings", status=402, body={"detail": "Insufficient credits"})
    with pytest.raises(ApiError) as e:
        ipx("p2g", "embed", "x")
    assert e.value.status == 402


def test_embed_is_p2g_only(ipx):
    from ipx.errors import IpxError

    sign_in(ipx)
    with pytest.raises(IpxError) as e:
        ipx("gen", "embed")
    assert "Pay-2-Go" in str(e.value.hint) or "p2g" in str(e.value.hint)


def test_models_type_embedding_uses_catalog_route(ipx):
    sign_in(ipx)
    ipx.fake.route("GET", "/v1/embeddings/models", body={"data": [{"id": "text-embedding-3-small", "name": "Text Embedding 3 Small", "input_modalities": ["text"], "output_modalities": ["embedding"]}]})
    r = ipx("--json", "p2g", "models", "--type", "embedding")
    assert r.exit_code == 0 and json.loads(r.output)[0]["id"] == "text-embedding-3-small"


def test_gen_models_rejects_p2g_only_types(ipx):
    from ipx.errors import IpxError

    sign_in(ipx)
    with pytest.raises(IpxError) as e:
        ipx("gen", "models", "--type", "realtime")
    assert "Pay-2-Go" in e.value.message and "ipx p2g models --type realtime" in e.value.hint
    assert ipx.fake.calls == []   # rejected locally, no request made


# --------------------------------------------------------------------------- audio helpers


def test_resample_halves_sample_count():
    pcm = struct.pack("<8h", *range(8))
    out = rt.resample_pcm16(pcm, 48000, 24000)
    assert len(out) == 8  # 4 samples
    assert struct.unpack("<4h", out) == (0, 2, 4, 6)
    assert rt.resample_pcm16(pcm, 24000) == pcm


def test_wav_is_converted_to_24k_mono(tmp_path):
    p = make_wav(tmp_path / "a.wav", seconds=1.0, rate=16000, channels=2)
    pcm = rt.pcm16_from_wav(str(p))
    assert abs(len(pcm) / 2 - 24000) <= 2
    # The source signal is a repeating 50-sample ramp; the left channel must come through as a smooth ramp, not silence.
    samples = struct.unpack(f"<{len(pcm) // 2}h", pcm)
    assert max(samples) > 3000 and min(samples) < -3000 and len(set(samples)) > 40


def test_non_wav_is_a_friendly_error(tmp_path):
    from ipx.errors import IpxError

    bad = tmp_path / "a.mp3"
    bad.write_bytes(b"ID3" + b"0" * 100)
    with pytest.raises(IpxError) as e:
        rt.pcm16_from_wav(str(bad))
    assert "ffmpeg" in e.value.hint


def test_text_of_event_shapes():
    assert rt.text_of({"type": "response.output_text.delta", "delta": "a"}) == ("delta", "a")
    assert rt.text_of({"type": "response.output_audio_transcript.delta", "delta": "b"}) == ("delta", "b")
    assert rt.text_of({"type": "conversation.item.input_audio_transcription.completed", "transcript": "c"}) == ("final", "c")
    assert rt.text_of({"type": "response.output_audio.delta", "delta": "AAAA"}) == ("", "")
    assert rt.text_of({"type": "response.audio.delta", "delta": "AAAA"}) == ("", "")
    assert rt.text_of({"type": "session.updated"}) == ("", "")


# --------------------------------------------------------------------------- realtime chat


def test_realtime_chat_one_shot(ipx, wsserver, tmp_path):
    sign_in(ipx)
    srv = wsserver(chat_script)
    out = tmp_path / "reply.wav"
    r = ipx("p2g", "realtime", "chat", "Say hi", "--voice", "marin", "--instructions", "Be brief", "--audio-out", str(out),
            env={"IPX_BASE_URL": srv.url})
    assert r.exit_code == 0, r.output
    assert "Hi there" in r.output
    assert "3 input / 5 output tokens" in r.output
    conn = srv.conns[0]
    assert conn["path"] == "/v1/realtime?model=gpt-realtime-2.1"
    assert conn["headers"]["authorization"] == "Bearer tok_abc"
    assert conn["subprotocol"] == "realtime"
    types = [f["type"] for f in conn["frames"]]
    assert types == ["session.update", "conversation.item.create", "response.create"]
    session = conn["frames"][0]["session"]
    # GA Realtime shape; an audio reply was requested, so the model is asked to speak, with the chosen voice.
    assert session["type"] == "realtime" and session["output_modalities"] == ["audio"]
    assert session["audio"]["output"]["voice"] == "marin" and session["instructions"] == "Be brief"
    assert "input_audio_transcription" not in session and "transcription" not in session["audio"]["input"]
    assert conn["frames"][1]["item"]["content"][0]["text"] == "Say hi"
    with wave.open(str(out), "rb") as w:
        assert w.getframerate() == 24000 and w.getnframes() == 4
        assert w.readframes(4) == struct.pack("<4h", 1, 2, 3, 4)   # the exact samples the server sent


def test_realtime_chat_sends_org_header(ipx, wsserver):
    sign_in(ipx)
    srv = wsserver(chat_script)
    ipx("--org", "acme", "p2g", "realtime", "chat", "hi", env={"IPX_BASE_URL": srv.url})
    assert srv.conns[0]["headers"]["x-inferenceport-org"] == "acme"


def test_realtime_chat_audio_in(ipx, wsserver, tmp_path):
    sign_in(ipx)
    srv = wsserver(chat_script)
    wav = make_wav(tmp_path / "q.wav", seconds=1.0, rate=24000)
    r = ipx("p2g", "realtime", "chat", "--audio-in", str(wav), env={"IPX_BASE_URL": srv.url})
    assert r.exit_code == 0, r.output
    types = [f["type"] for f in srv.conns[0]["frames"]]
    assert types.count("input_audio_buffer.append") == 5  # 1 s at 200 ms per chunk
    assert types[-2:] == ["input_audio_buffer.commit", "response.create"]
    sent = b"".join(base64.b64decode(f["audio"]) for f in srv.conns[0]["frames"] if f["type"] == "input_audio_buffer.append")
    with wave.open(str(wav), "rb") as w:
        assert sent == w.readframes(w.getnframes())   # 24 kHz mono in -> byte-identical audio on the wire


def test_realtime_chat_multiple_stdin_lines(ipx, wsserver):
    sign_in(ipx)
    srv = wsserver(chat_script)
    r = ipx("p2g", "realtime", "chat", input="first\nsecond\n", env={"IPX_BASE_URL": srv.url})
    assert r.exit_code == 0, r.output
    assert r.output.count("Hi there") == 2
    assert [f["type"] for f in srv.conns[0]["frames"]].count("response.create") == 2


def test_realtime_chat_server_error_event(ipx, wsserver):
    from ipx.errors import IpxError

    async def script(ws, rec):
        async for raw in ws:
            if json.loads(raw)["type"] == "response.create":
                await ws.send(json.dumps({"type": "error", "error": {"message": "bad session"}}))

    sign_in(ipx)
    srv = wsserver(script)
    with pytest.raises(IpxError) as e:
        ipx("p2g", "realtime", "chat", "hi", env={"IPX_BASE_URL": srv.url})
    assert "bad session" in str(e.value)


@pytest.mark.parametrize("code,status", [(4402, 402), (4401, 401), (4403, 403), (4404, 404), (4408, 408), (4429, 429), (1011, 502)])
def test_close_codes_map_to_statuses_and_hints(ipx, wsserver, code, status):
    from ipx.errors import ApiError

    async def script(ws, rec):
        await ws.recv()
        await ws.close(code)        # a bare close: no error event first

    sign_in(ipx)
    srv = wsserver(script)
    with pytest.raises(ApiError) as e:
        ipx("p2g", "realtime", "chat", "hi", env={"IPX_BASE_URL": srv.url})
    assert e.value.status == status
    assert e.value.message == rt.CLOSE_HINTS[code][0]


def test_error_event_with_code_maps_to_status(ipx, wsserver):
    from ipx.errors import ApiError

    async def script(ws, rec):
        await ws.recv()
        await ws.send(json.dumps({"type": "error", "error": {"code": "insufficient_credits", "message": "no credits"}}))

    sign_in(ipx)
    srv = wsserver(script)
    with pytest.raises(ApiError) as e:
        ipx("p2g", "realtime", "chat", "hi", env={"IPX_BASE_URL": srv.url})
    assert e.value.status == 402 and "balance" in e.value.hint


def test_realtime_handshake_rejection_maps_to_api_error(ipx, wsserver):
    from ipx.errors import ApiError

    sign_in(ipx)
    srv = wsserver(chat_script, reject=(401, {"detail": "Invalid authorization token"}))
    with pytest.raises(ApiError) as e:
        ipx("p2g", "realtime", "chat", "hi", env={"IPX_BASE_URL": srv.url})
    assert e.value.status == 401


def test_realtime_requires_login(ipx):
    from ipx.errors import NotSignedIn

    with pytest.raises(NotSignedIn):
        ipx("p2g", "realtime", "chat", "hi")


def test_realtime_is_p2g_only(ipx):
    from ipx.errors import IpxError

    with pytest.raises(IpxError) as e:
        ipx("gen", "realtime")
    assert "Pay-2-Go" in str(e.value.hint) or "p2g" in str(e.value.hint)
    assert "not part of Lightning" in e.value.message


def test_realtime_bad_session_json(ipx):
    from ipx.errors import IpxError

    sign_in(ipx)
    with pytest.raises(IpxError):
        ipx("p2g", "realtime", "chat", "hi", "--session", "{nope")


# --------------------------------------------------------------------------- transcribe


async def transcribe_script(ws, rec):
    commits = 0
    async for raw in ws:
        ev = json.loads(raw)
        rec["frames"].append(ev)
        if ev["type"] == "input_audio_buffer.append" and len([f for f in rec["frames"] if f["type"] == "input_audio_buffer.append"]) == 2:
            await ws.send(json.dumps({"type": "conversation.item.input_audio_transcription.delta", "item_id": "i1", "delta": "hel"}))
        if ev["type"] == "input_audio_buffer.commit":
            commits += 1
            await ws.send(json.dumps({"type": "conversation.item.input_audio_transcription.completed", "item_id": "i1", "transcript": "hello world"}))


def test_transcribe_manual_commit_model(ipx, wsserver, tmp_path):
    sign_in(ipx)
    srv = wsserver(transcribe_script)
    wav = make_wav(tmp_path / "talk.wav", seconds=1.0, rate=16000)
    r = ipx("p2g", "realtime", "transcribe", str(wav), "-m", "scribe-v2-realtime", "--idle", "0.5", env={"IPX_BASE_URL": srv.url})
    assert r.exit_code == 0, r.output
    assert "hello world" in r.output
    assert "1.0s of audio streamed" in r.output
    conn = srv.conns[0]
    assert conn["path"] == "/v1/realtime?model=scribe-v2-realtime"
    types = [f["type"] for f in conn["frames"]]
    assert types[0] == "session.update" and types[-1] == "input_audio_buffer.commit"
    pcm = b"".join(base64.b64decode(f["audio"]) for f in conn["frames"] if f["type"] == "input_audio_buffer.append")
    assert abs(len(pcm) / 2 - 24000) <= 2          # 1 s of 16 kHz audio, sent as 24 kHz
    session = conn["frames"][0]["session"]   # catalog-less fallback: GA transcription session, manual commits
    assert session["type"] == "transcription" and session["audio"]["input"]["turn_detection"] is None


def test_transcribe_vad_model_gets_a_silent_tail_instead_of_a_commit(ipx, wsserver, tmp_path):
    sign_in(ipx)
    srv = wsserver(transcribe_script)
    wav = make_wav(tmp_path / "talk.wav", seconds=1.0, rate=24000)
    r = ipx("p2g", "realtime", "transcribe", str(wav), "--idle", "0.5", "--wait", "1", env={"IPX_BASE_URL": srv.url})
    assert r.exit_code == 0, r.output
    conn = srv.conns[0]
    assert conn["path"] == "/v1/realtime?model=gpt-live-transcribe"
    session = conn["frames"][0]["session"]
    assert session["type"] == "transcription" and session["audio"]["input"]["turn_detection"] == {"type": "server_vad"}
    assert session["audio"]["input"]["transcription"] == {"model": "gpt-live-transcribe"}
    appends = [f for f in conn["frames"] if f["type"] == "input_audio_buffer.append"]
    assert "input_audio_buffer.commit" not in [f["type"] for f in conn["frames"]]
    tail = b"".join(base64.b64decode(f["audio"]) for f in appends)[-int(24000 * 1.5) * 2:]
    assert tail == bytes(len(tail))   # the last 1.5 s are silence so the server's VAD can end the utterance
    assert "2.5s of audio streamed" in r.output


def test_transcribe_waits_for_the_transcript_not_just_a_quiet_moment(ipx, wsserver, tmp_path):
    async def slow(ws, rec):
        async for raw in ws:
            if json.loads(raw)["type"] == "input_audio_buffer.commit":
                await asyncio.sleep(1.2)   # longer than --idle
                await ws.send(json.dumps({"type": "conversation.item.input_audio_transcription.completed", "transcript": "late words"}))

    sign_in(ipx)
    srv = wsserver(slow)
    wav = make_wav(tmp_path / "talk.wav", seconds=0.3, rate=24000)
    r = ipx("p2g", "realtime", "transcribe", str(wav), "-m", "scribe-v2-realtime", "--idle", "0.5", env={"IPX_BASE_URL": srv.url})
    assert "late words" in r.output


def test_transcribe_model_output_file_and_json(ipx, wsserver, tmp_path):
    sign_in(ipx)
    srv = wsserver(transcribe_script)
    wav = make_wav(tmp_path / "talk.wav", seconds=0.5, rate=24000)
    out = tmp_path / "t.txt"
    ipx("p2g", "realtime", "transcribe", str(wav), "-m", "scribe-v2-realtime", "--idle", "0.5", "-o", str(out), env={"IPX_BASE_URL": srv.url})
    assert out.read_text().strip() == "hello world"
    assert srv.conns[0]["path"].endswith("model=scribe-v2-realtime")
    r = ipx("--json", "p2g", "realtime", "transcribe", str(wav), "-m", "scribe-v2-realtime", "--idle", "0.5", env={"IPX_BASE_URL": srv.url})
    body = json.loads(r.output)   # stdout is exactly one JSON document
    assert body["text"] == "hello world" and body["audio_seconds"] == 0.5 and body["model"] == "scribe-v2-realtime"


def test_transcribe_raw_pcm_from_stdin(ipx, wsserver):
    sign_in(ipx)
    srv = wsserver(transcribe_script)
    raw = struct.pack("<h", 100) * 16000  # 1 s of 16 kHz PCM16
    r = ipx("p2g", "realtime", "transcribe", "-", "--rate", "16000", "-m", "scribe-v2-realtime", "--idle", "0.5",
            env={"IPX_BASE_URL": srv.url}, input=raw)
    assert r.exit_code == 0, r.output
    assert "hello world" in r.output
    appended = [f for f in srv.conns[0]["frames"] if f["type"] == "input_audio_buffer.append"]
    pcm = b"".join(base64.b64decode(f["audio"]) for f in appended)
    assert abs(len(pcm) / 2 - 24000) <= 2  # resampled to 24 kHz
    assert set(struct.unpack(f"<{len(pcm) // 2}h", pcm)) == {100}   # a constant signal stays constant


def test_transcribe_falls_back_to_partial_text(ipx, wsserver, tmp_path):
    async def script(ws, rec):
        async for raw in ws:
            ev = json.loads(raw)
            if ev["type"] == "input_audio_buffer.commit":
                await ws.send(json.dumps({"type": "conversation.item.input_audio_transcription.delta", "delta": "only partial"}))

    sign_in(ipx)
    srv = wsserver(script)
    wav = make_wav(tmp_path / "talk.wav", seconds=0.3, rate=24000)
    r = ipx("p2g", "realtime", "transcribe", str(wav), "-m", "scribe-v2-realtime", "--idle", "0.5", env={"IPX_BASE_URL": srv.url})
    assert "only partial" in r.output


def test_transcribe_error_event_fails_the_command(ipx, wsserver, tmp_path):
    from ipx.errors import ApiError

    async def script(ws, rec):
        async for raw in ws:
            if json.loads(raw)["type"] == "input_audio_buffer.commit":
                await ws.send(json.dumps({"type": "error", "error": {"code": "rate_limited", "message": "slow down"}}))
                await asyncio.sleep(5)   # the server does NOT close the socket

    sign_in(ipx)
    srv = wsserver(script)
    wav = make_wav(tmp_path / "talk.wav", seconds=0.3, rate=24000)
    with pytest.raises(ApiError) as e:
        ipx("p2g", "realtime", "transcribe", str(wav), "-m", "scribe-v2-realtime", env={"IPX_BASE_URL": srv.url})
    assert e.value.status == 429 and "slow down" in e.value.message


@pytest.mark.parametrize("how", ["abort", 1011, 1008])
def test_transcribe_abnormal_close_is_an_error_not_a_partial_success(ipx, wsserver, tmp_path, how):
    from ipx.errors import IpxError

    async def script(ws, rec):
        async for raw in ws:
            if json.loads(raw)["type"] == "input_audio_buffer.commit":
                if how == "abort":
                    ws.transport.abort()          # drop the TCP connection without a close frame
                else:
                    await ws.close(how, "Realtime input transcription is not supported yet.")

    sign_in(ipx)
    srv = wsserver(script)
    wav = make_wav(tmp_path / "talk.wav", seconds=0.3, rate=24000)
    with pytest.raises(IpxError) as e:
        ipx("p2g", "realtime", "transcribe", str(wav), "-m", "scribe-v2-realtime", env={"IPX_BASE_URL": srv.url})
    if how == 1008:
        assert "not supported yet" in e.value.message    # the server's reason is passed on
    else:
        assert "lost" in e.value.message or "closed" in e.value.message.lower() or "unavailable" in e.value.message.lower()


def test_transcribe_missing_file_is_a_clean_error(ipx):
    from ipx.errors import IpxError

    sign_in(ipx)
    with pytest.raises(IpxError) as e:
        ipx("p2g", "realtime", "transcribe", "/nonexistent/nope.wav")
    assert "File not found" in e.value.message


def test_transcribe_rejects_conversational_models_and_vice_versa(ipx):
    from ipx.errors import IpxError

    sign_in(ipx)
    with pytest.raises(IpxError) as e:
        ipx("p2g", "realtime", "transcribe", "x.wav", "-m", "gpt-realtime-2.1")
    assert "conversational" in e.value.message
    with pytest.raises(IpxError) as e:
        ipx("p2g", "realtime", "chat", "hi", "-m", "scribe-realtime")   # an alias of a transcription model
    assert "transcription model" in e.value.message


def test_session_defaults_come_from_the_catalog(ipx, wsserver):
    sign_in(ipx)
    custom = {"type": "realtime", "output_modalities": ["audio"], "marker": "from-catalog",
              "audio": {"input": {"format": {"type": "audio/pcm", "rate": 24000}}}}
    srv = wsserver(chat_script, catalog=[{"id": "gpt-realtime-2.1", "session_kind": "conversation", "default_session": custom}])
    ipx("p2g", "realtime", "chat", "hi", "--instructions", "Be brief", env={"IPX_BASE_URL": srv.url})
    session = srv.conns[0]["frames"][0]["session"]
    assert session["marker"] == "from-catalog"                      # the catalog entry is the base
    assert session["output_modalities"] == ["text"] and session["instructions"] == "Be brief"   # text unless audio is wanted


def test_catalog_marks_a_transcription_model_for_chat(ipx, wsserver):
    from ipx.errors import IpxError

    sign_in(ipx)
    srv = wsserver(chat_script, catalog=[{"id": "some-new-transcriber", "session_kind": "transcription"}])
    with pytest.raises(IpxError) as e:
        ipx("p2g", "realtime", "chat", "hi", "-m", "some-new-transcriber", env={"IPX_BASE_URL": srv.url})
    assert "transcription model" in e.value.message


def test_text_chat_does_not_ask_for_audio(ipx, wsserver):
    sign_in(ipx)
    srv = wsserver(chat_script)
    ipx("p2g", "realtime", "chat", "hi", env={"IPX_BASE_URL": srv.url})
    assert srv.conns[0]["frames"][0]["session"]["output_modalities"] == ["text"]


def test_transcribe_close_code_is_reported(ipx, wsserver, tmp_path):
    from ipx.errors import IpxError

    async def script(ws, rec):
        await ws.recv()
        await ws.close(4403)

    sign_in(ipx)
    srv = wsserver(script)
    wav = make_wav(tmp_path / "talk.wav", seconds=3.0, rate=24000)
    with pytest.raises(IpxError) as e:
        ipx("p2g", "realtime", "transcribe", str(wav), env={"IPX_BASE_URL": srv.url})
    assert "policy" in str(e.value).lower()


# --------------------------------------------------------------------------- websocket client


def test_ws_client_handles_fragmented_ping_and_large_frames(wsserver):
    from ipx import ws as wsmod

    big = "x" * 70000

    async def script(ws, rec):
        await ws.ping(b"hi")
        await ws.send(["frag", "ment", "ed"])  # fragmented text message
        await ws.send(big)
        await ws.send(b"\x00\x01\x02")
        echoed = await ws.recv()
        await ws.send("echo:" + echoed)
        await ws.close(1000, "bye")

    srv = wsserver(script)
    conn = wsmod.connect(srv.url.replace("http", "ws") + "/x", headers={"X-Test": "1"}, subprotocol="realtime")
    assert conn.subprotocol == "realtime"
    assert conn.recv_text(5) == "fragmented"
    assert conn.recv_text(5) == big
    assert conn.recv(5) == (wsmod.OP_BINARY, b"\x00\x01\x02")
    assert conn.recv(0.05) is None  # nothing pending: times out cleanly without corrupting state
    conn.send_text("ü" * 3000)  # masked 2-byte-length frame
    assert conn.recv_text(5) == "echo:" + "ü" * 3000
    with pytest.raises(wsmod.WebSocketClosed) as e:
        conn.recv(5)
    assert e.value.code == 1000 and e.value.reason == "bye"
    assert srv.conns[0]["headers"]["x-test"] == "1"


# --------------------------------------------------------------------------- review follow-ups


def test_chat_json_mode_prints_exactly_one_json_document(ipx, wsserver, tmp_path):
    sign_in(ipx)
    srv = wsserver(chat_script)
    out = tmp_path / "r.wav"
    r = ipx("--json", "p2g", "realtime", "chat", "Say hi", "--audio-out", str(out), env={"IPX_BASE_URL": srv.url})
    body = json.loads(r.output)        # no streamed text, no "Saved ..." line, no footer on stdout
    assert body["text"] == "Hi there" and body["audio_file"] == str(out)
    assert body["usage"] == {"input_tokens": 3, "output_tokens": 5}
    assert out.exists()


def test_realtime_url_keeps_a_path_prefix():
    assert rt.realtime_url("https://gw.example.com/ipx/", "m") == "wss://gw.example.com/ipx/v1/realtime?model=m"
    assert rt.realtime_url("http://127.0.0.1:8080", "a/b") == "ws://127.0.0.1:8080/v1/realtime?model=a%2Fb"
    assert rt.realtime_url("https://api.inferenceport.ai", "m") == "wss://api.inferenceport.ai/v1/realtime?model=m"


def test_handshake_401_refreshes_the_session_and_retries(ipx, wsserver):
    sign_in(ipx)   # access token tok_abc, refresh token rt_1
    ipx.fake.route("POST", "/sb/auth/v1/token", body={"access_token": "tok_new", "refresh_token": "rt_2", "expires_at": 4102444800,
                                                    "user": {"id": "u1", "email": "me@example.com"}})
    srv = wsserver(chat_script, reject=lambda req: (401, {"detail": "Invalid authorization token"})
                   if req.headers.get("Authorization") == "Bearer tok_abc" else None)
    r = ipx("p2g", "realtime", "chat", "hi", env={"IPX_BASE_URL": srv.url})
    assert r.exit_code == 0, r.output
    assert srv.conns[0]["headers"]["authorization"] == "Bearer tok_new"


def test_handshake_401_without_a_refresh_token_gets_a_login_hint(ipx, wsserver):
    from ipx.config import Credentials
    from ipx.errors import ApiError

    sign_in(ipx)
    c = Credentials()
    c.data["refresh_token"] = None
    c.save()
    srv = wsserver(chat_script, reject=(401, {"detail": "Invalid authorization token"}))
    with pytest.raises(ApiError) as e:
        ipx("p2g", "realtime", "chat", "hi", env={"IPX_BASE_URL": srv.url})
    assert "ipx auth login" in e.value.hint


def test_plain_websocket_is_only_allowed_for_loopback():
    from ipx import ws as wsmod
    from ipx.errors import IpxError

    with pytest.raises(IpxError) as e:
        wsmod.connect("ws://api.example.com/v1/realtime")
    assert "unencrypted" in e.value.message and "https" in e.value.hint


def test_send_failures_become_websocket_closed_errors():
    from ipx import ws as wsmod

    class Broken:
        def settimeout(self, t):
            pass

        def sendall(self, data):
            raise BrokenPipeError("pipe is broken")

    conn = wsmod.WebSocket(Broken())
    with pytest.raises(wsmod.WebSocketClosed) as e:
        conn.send_text("hello")
    assert e.value.code == 1006 and "dropped" in e.value.message


def test_unreadable_wav_is_a_clean_error(tmp_path):
    from ipx.errors import IpxError

    with pytest.raises(IpxError) as e:
        rt.pcm16_from_wav(str(tmp_path / "missing.wav"))
    assert "Could not read" in e.value.message and "ffmpeg" in e.value.hint
    with pytest.raises(IpxError):
        rt.pcm16_from_wav(str(tmp_path))      # a directory


def test_ping_traffic_does_not_extend_the_idle_timeout(wsserver):
    """A server that pings faster than the timeout must not keep recv() waiting forever."""
    from ipx import ws as wsmod

    async def script(ws, rec):
        for _ in range(60):
            await ws.ping()
            await asyncio.sleep(0.05)

    srv = wsserver(script)
    conn = wsmod.connect(srv.url.replace("http", "ws") + "/x", subprotocol="realtime")
    started = time.monotonic()
    assert conn.recv(0.4) is None
    assert time.monotonic() - started < 1.0       # one deadline, not one per ping
    conn.close()


def test_control_frames_are_answered_while_uploading_audio(ipx, wsserver, tmp_path):
    order = []

    async def script(ws, rec):
        pong = await ws.ping()
        asyncio.ensure_future(_note_pong(pong, order))
        async for raw in ws:
            ev = json.loads(raw)
            rec["frames"].append(ev)
            if ev["type"] == "input_audio_buffer.commit":
                order.append("commit")
            if ev["type"] == "response.create":
                await ws.send(response_done())

    async def _note_pong(pong, order):
        await pong
        order.append("pong")

    sign_in(ipx)
    srv = wsserver(script)
    wav = make_wav(tmp_path / "long.wav", seconds=1.4, rate=24000)   # 7 chunks
    ipx("p2g", "realtime", "chat", "--audio-in", str(wav), env={"IPX_BASE_URL": srv.url})
    assert order.index("pong") < order.index("commit")   # the pong went out mid-upload, not after it


def test_chat_keeps_audio_that_arrived_before_a_failure(ipx, wsserver, tmp_path):
    from ipx.errors import ApiError

    async def script(ws, rec):
        async for raw in ws:
            if json.loads(raw)["type"] == "response.create":
                pcm = struct.pack("<3h", 7, 8, 9)
                await ws.send(json.dumps({"type": "response.output_audio.delta", "delta": base64.b64encode(pcm).decode()}))
                await ws.close(4402)

    sign_in(ipx)
    srv = wsserver(script)
    out = tmp_path / "partial.wav"
    with pytest.raises(ApiError):
        ipx("p2g", "realtime", "chat", "hi", "--audio-out", str(out), env={"IPX_BASE_URL": srv.url})
    with wave.open(str(out), "rb") as w:
        assert w.readframes(w.getnframes()) == struct.pack("<3h", 7, 8, 9)


def test_chat_warns_when_no_audio_was_produced(ipx, wsserver, tmp_path):
    async def text_only(ws, rec):
        async for raw in ws:
            if json.loads(raw)["type"] == "response.create":
                await ws.send(json.dumps({"type": "response.output_text.delta", "delta": "silent"}))
                await ws.send(response_done())

    sign_in(ipx)
    srv = wsserver(text_only)
    out = tmp_path / "old.wav"
    out.write_bytes(b"stale")
    r = ipx("p2g", "realtime", "chat", "hi", "--audio-out", str(out), env={"IPX_BASE_URL": srv.url})
    flat = " ".join(r.output.split())          # the terminal may wrap the line
    assert "sent no audio" in flat and "was not written" in flat
    assert out.read_bytes() == b"stale"      # untouched, and the warning says so


def test_write_atomic_is_unique_per_writer_and_leaves_no_temp_files(tmp_path):
    from ipx.commands.modalities import write_atomic

    target = tmp_path / "out" / "t.txt"
    threads = [threading.Thread(target=write_atomic, args=(str(target), (f"writer-{i}-" * 2000).encode())) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    data = target.read_text()
    assert any(data == f"writer-{i}-" * 2000 for i in range(8))      # exactly one writer's complete output
    assert [p.name for p in target.parent.iterdir()] == ["t.txt"]    # no stray temp files


def test_credentials_are_written_with_unique_temp_files(home):
    from ipx.config import Credentials

    errors = []

    def writer(i):
        try:
            c = Credentials()
            for n in range(20):
                c.store_session({"access_token": f"tok-{i}-{n}", "refresh_token": "r", "expires_at": 4102444800,
                                 "user": {"id": "u", "email": "a@b.c"}})
        except Exception as e:  # pragma: no cover
            errors.append(e)

    threads = [threading.Thread(target=writer, args=(i,)) for i in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    saved = json.loads(Credentials().path.read_text())
    assert saved["access_token"].startswith("tok-") and saved["refresh_token"] == "r"
    assert [p.name for p in Credentials().path.parent.iterdir() if p.name.endswith(".tmp")] == []


def test_refresh_picks_up_a_login_another_process_already_refreshed(ipx):
    from ipx import auth
    from ipx.config import Config, Credentials

    sign_in(ipx)
    creds = Credentials()                      # this process still holds refresh token rt_1
    other = Credentials()
    other.store_session({"access_token": "tok_other", "refresh_token": "rt_9", "expires_at": 4102444800,
                         "user": {"id": "u1", "email": "me@example.com"}})
    ipx.fake.route("POST", "/sb/auth/v1/token", status=400, body={"error": "invalid_grant", "error_description": "Refresh Token Not Found"})
    assert auth.refresh(Config(), creds) is True
    assert creds.access_token == "tok_other"


class TestResampler:
    @pytest.mark.parametrize("src", [8000, 16000, 22050, 24000, 44100, 48000])
    @pytest.mark.parametrize("piece", [1, 7, 1000, 4801])
    def test_chunked_equals_one_shot(self, src, piece):
        import random

        rnd = random.Random(src + piece)
        data = struct.pack("<3001h", *[rnd.randint(-30000, 30000) for _ in range(3001)])
        one_shot = rt.resample_pcm16(data, src)
        r = rt.Resampler(src)
        pieces = [r.feed(data[i:i + piece]) for i in range(0, len(data), piece)]
        assert b"".join(pieces) + r.flush() == one_shot

    def test_odd_byte_boundaries_do_not_corrupt_samples(self):
        data = struct.pack("<4h", 100, -200, 300, -400)
        r = rt.Resampler(24000)
        assert r.feed(data[:3]) + r.feed(data[3:]) == data

    def test_length_follows_the_rate_ratio(self):
        out = rt.resample_pcm16(struct.pack("<4800h", *range(4800)), 48000)
        assert abs(len(out) / 2 - 2400) <= 1


def test_stdin_chunks_stream_before_end_of_input():
    """A live `arecord | ipx ...` pipe must produce audio chunks while the source is still running."""
    class Pipe:
        def __init__(self):
            self.parts = [struct.pack("<h", 5) * 5000, struct.pack("<h", 5) * 5000]
            self.served = 0

        def read1(self, n):
            if not self.parts:
                return b""
            self.served += 1
            return self.parts.pop(0)

    pipe = Pipe()
    chunks = rt.stdin_chunks(pipe, 24000)
    first = next(chunks)
    assert len(first) == rt.CHUNK_SAMPLES * 2 and pipe.served == 1     # yielded after the first read, not at EOF
    assert len(b"".join([first, *chunks])) == 10000 * 2


def test_embed_batches_more_than_32_inputs(ipx):
    sign_in(ipx)
    seen = []

    def handler(rec):
        inputs = rec["json"]["input"]
        seen.append(len(inputs))
        return 200, {}, {"object": "list", "model": rec["json"]["model"],
                         "data": [{"object": "embedding", "index": i, "embedding": [float(i)]} for i in range(len(inputs))],
                         "usage": {"prompt_tokens": len(inputs), "payg_credits_charged": 0.001}}

    ipx.fake.route("POST", "/v1/embeddings", handler)
    texts = [f"t{i}" for i in range(40)]
    r = ipx("--json", "p2g", "embed", *texts, "--input-type", "document")
    body = json.loads(r.output)
    assert seen == [32, 8]
    assert [row["index"] for row in body["data"]] == list(range(40))      # indices continue across batches
    assert body["usage"]["prompt_tokens"] == 40 and abs(body["usage"]["payg_credits_charged"] - 0.002) < 1e-9
    assert all(c["json"]["input_type"] == "document" for c in ipx.fake.calls if c["path"] == "/v1/embeddings")


def test_embed_dimension_floor(ipx):
    sign_in(ipx)
    r = ipx("p2g", "embed", "x", "--dimensions", "64")
    assert r.exit_code == 2 and "128" in r.output
