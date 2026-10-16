"""`ipx p2g embed` and `ipx p2g realtime ...` against a fake API and a real local WebSocket server."""

import asyncio
import base64
import json
import struct
import threading
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

    def __init__(self, script, reject=None):
        self.script = script
        self.reject = reject
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
            if self.reject:
                status, body = self.reject
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

    def make(script, reject=None):
        s = WsServer(script, reject)
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
    with pytest.raises(IpxError):
        ipx("gen", "models", "--type", "realtime")


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
    assert conn["frames"][0]["session"]["voice"] == "marin" and conn["frames"][0]["session"]["instructions"] == "Be brief"
    assert conn["frames"][1]["item"]["content"][0]["text"] == "Say hi"
    with wave.open(str(out), "rb") as w:
        assert w.getframerate() == 24000 and w.getnframes() == 4


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
    first = base64.b64decode(srv.conns[0]["frames"][1]["audio"])
    assert len(first) == rt.CHUNK_SAMPLES * 2


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


def test_realtime_out_of_credits_close_code(ipx, wsserver):
    from ipx.errors import ApiError

    async def script(ws, rec):
        await ws.send(json.dumps({"type": "error", "error": {"code": "insufficient_credits", "message": "no credits"}}))
        await ws.close(4402)

    sign_in(ipx)
    srv = wsserver(script)
    with pytest.raises(ApiError) as e:
        ipx("p2g", "realtime", "chat", "hi", env={"IPX_BASE_URL": srv.url})
    assert e.value.status == 402 or "credits" in str(e.value).lower()


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

    with pytest.raises(IpxError):
        ipx("gen", "realtime")


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


def test_transcribe_wav_file(ipx, wsserver, tmp_path):
    sign_in(ipx)
    srv = wsserver(transcribe_script)
    wav = make_wav(tmp_path / "talk.wav", seconds=1.0, rate=16000)
    r = ipx("p2g", "realtime", "transcribe", str(wav), "--idle", "0.5", env={"IPX_BASE_URL": srv.url})
    assert r.exit_code == 0, r.output
    assert "hello world" in r.output
    assert "1.0s of audio streamed" in r.output
    conn = srv.conns[0]
    assert conn["path"] == "/v1/realtime?model=gpt-live-transcribe"
    types = [f["type"] for f in conn["frames"]]
    assert types[0] == "session.update" and types[-1] == "input_audio_buffer.commit"
    assert types.count("input_audio_buffer.append") == 5


def test_transcribe_model_output_file_and_json(ipx, wsserver, tmp_path):
    sign_in(ipx)
    srv = wsserver(transcribe_script)
    wav = make_wav(tmp_path / "talk.wav", seconds=0.5, rate=24000)
    out = tmp_path / "t.txt"
    ipx("p2g", "realtime", "transcribe", str(wav), "-m", "scribe-v2-realtime", "--idle", "0.5", "-o", str(out), env={"IPX_BASE_URL": srv.url})
    assert out.read_text().strip() == "hello world"
    assert srv.conns[0]["path"].endswith("model=scribe-v2-realtime")
    r = ipx("--json", "p2g", "realtime", "transcribe", str(wav), "--idle", "0.5", env={"IPX_BASE_URL": srv.url})
    body = json.loads(r.output)
    assert body["text"] == "hello world" and body["audio_seconds"] == 0.5 and body["model"] == "gpt-live-transcribe"


def test_transcribe_raw_pcm_from_stdin(ipx, wsserver):
    sign_in(ipx)
    srv = wsserver(transcribe_script)
    raw = struct.pack("<h", 100) * 16000  # 1 s of 16 kHz PCM16
    r = ipx("p2g", "realtime", "transcribe", "-", "--rate", "16000", "--idle", "0.5", env={"IPX_BASE_URL": srv.url}, input=raw)
    assert r.exit_code == 0, r.output
    assert "hello world" in r.output
    appended = [f for f in srv.conns[0]["frames"] if f["type"] == "input_audio_buffer.append"]
    total = sum(len(base64.b64decode(f["audio"])) for f in appended)
    assert abs(total / 2 - 24000) <= 2  # resampled to 24 kHz


def test_transcribe_falls_back_to_partial_text(ipx, wsserver, tmp_path):
    async def script(ws, rec):
        async for raw in ws:
            ev = json.loads(raw)
            if ev["type"] == "input_audio_buffer.commit":
                await ws.send(json.dumps({"type": "conversation.item.input_audio_transcription.delta", "delta": "only partial"}))

    sign_in(ipx)
    srv = wsserver(script)
    wav = make_wav(tmp_path / "talk.wav", seconds=0.3, rate=24000)
    r = ipx("p2g", "realtime", "transcribe", str(wav), "--idle", "0.5", env={"IPX_BASE_URL": srv.url})
    assert "only partial" in r.output


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
