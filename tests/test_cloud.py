import base64
import json
import os

import pytest

from conftest import sign_in

JPEG = b"\xff\xd8\xff\xe0" + b"0" * 64
MP4 = b"\x00\x00\x00\x18ftypmp42" + b"0" * 64
MP3 = b"ID3" + b"0" * 64


def sse(*frames):
    out = ""
    for f in frames:
        if isinstance(f, tuple):
            out += f"event: {f[0]}\ndata: {json.dumps(f[1])}\n\n"
        elif f == "[DONE]":
            out += "data: [DONE]\n\n"
        else:
            out += f"data: {json.dumps(f)}\n\n"
    return out


def chunk(text):
    return {"choices": [{"delta": {"content": text}}]}


@pytest.fixture(autouse=True)
def fast_poll(monkeypatch):
    monkeypatch.setenv("IPX_POLL_INTERVAL", "0.01")


def test_gen_text_streams_to_stdout(ipx):
    sign_in(ipx)
    ipx.fake.route("POST", "/gen/chat/completions", body=sse({"router_metadata": {"model_name": "Llama 3.3 70B"}}, chunk("Hello"), chunk(", world"), "[DONE]"),
                   headers={"Content-Type": "text/event-stream"})
    r = ipx("gen", "text", "say hi")
    assert r.exit_code == 0, r.output
    assert "Hello, world" in r.output
    body = ipx.fake.last("/gen/chat/completions")["json"]
    assert body["model"] == "lightning" and body["stream"] is True and body["messages"][-1] == {"role": "user", "content": "say hi"}
    assert ipx.fake.last("/gen/chat/completions")["headers"]["authorization"] == "Bearer tok_abc"


def test_p2g_text_json_includes_credits(ipx):
    sign_in(ipx)
    usage = ("payg-usage", {"payg_input_tokens": 5, "payg_output_tokens": 7, "payg_total_tokens": 12, "payg_credits_charged": 0.0001})
    ipx.fake.route("POST", "/v1/chat/completions", body=sse(chunk("ok"), usage, "[DONE]"), headers={"Content-Type": "text/event-stream"})
    r = ipx("--json", "p2g", "text", "ping", "-m", "gpt-x", "--system", "be brief")
    out = json.loads(r.output)
    assert out["text"] == "ok" and out["usage"]["credits"] == 0.0001 and out["usage"]["tokens"] == 12
    body = ipx.fake.last("/v1/chat/completions")["json"]
    assert body["model"] == "gpt-x" and body["messages"][0] == {"role": "system", "content": "be brief"}


def test_text_requires_login(ipx):
    from ipx.errors import NotSignedIn
    with pytest.raises(NotSignedIn):
        ipx("gen", "text", "hi")


def test_text_reads_stdin(ipx):
    sign_in(ipx)
    ipx.fake.route("POST", "/gen/chat/completions", body=sse(chunk("done"), "[DONE]"), headers={"Content-Type": "text/event-stream"})
    ipx("gen", "text", "-", input="from a pipe\n")
    assert ipx.fake.last("/gen/chat/completions")["json"]["messages"][-1]["content"] == "from a pipe"


def test_image_saved_with_credits(ipx, tmp_path):
    sign_in(ipx)
    ipx.fake.route("POST", "/v1/images/generations", body={"data": [{"b64_json": base64.b64encode(JPEG).decode()}], "usage": {"payg_credits_charged": 0.02}})
    out = tmp_path / "pic.jpg"
    r = ipx("p2g", "image", "a cat", "-o", str(out), "--mode", "realistic")
    assert r.exit_code == 0, r.output
    assert out.read_bytes() == JPEG
    assert "0.02 credits" in r.output
    body = ipx.fake.last("/v1/images/generations")["json"]
    assert body["prompt"] == "a cat" and body["mode"] == "realistic" and body["response_format"] == "b64_json"


def test_image_reference_file_becomes_data_uri(ipx, tmp_path):
    sign_in(ipx)
    ipx.fake.route("POST", "/gen/images/generations", body={"data": [{"b64_json": base64.b64encode(JPEG).decode()}]})
    ref = tmp_path / "ref.png"
    ref.write_bytes(b"\x89PNG\r\n\x1a\n123")
    ipx("gen", "image", "variation", "--ref", str(ref), "-o", str(tmp_path / "o.jpg"))
    assert ipx.fake.last("/gen/images/generations")["json"]["image_urls"][0].startswith("data:image/png;base64,")


def test_image_multi_names_files(ipx, tmp_path):
    sign_in(ipx)
    b64 = base64.b64encode(JPEG).decode()
    ipx.fake.route("POST", "/gen/images/generations", body={"data": [{"b64_json": b64}, {"b64_json": b64}]})
    ipx("gen", "image", "two", "-n", "2", "-o", str(tmp_path / "set.jpg"))
    assert (tmp_path / "set-1.jpg").exists() and (tmp_path / "set-2.jpg").exists()


def test_video_async_flow(ipx, tmp_path):
    sign_in(ipx)
    state = {"polls": 0}
    ipx.fake.route("POST", "/v1/videos", status=201, body={"id": "vid_1", "status": "pending", "progress": 0.0})

    def poll(rec):
        state["polls"] += 1
        if state["polls"] < 3:
            return 200, {}, {"id": "vid_1", "status": "processing", "progress": 0.5}
        return 200, {}, {"id": "vid_1", "status": "completed", "progress": 1.0, "model": "wan-fast"}

    ipx.fake.route("GET", "/v1/videos/vid_1", poll)
    ipx.fake.route("GET", "/v1/videos/vid_1/content", body=MP4, headers={"Content-Type": "video/mp4"})
    out = tmp_path / "clip.mp4"
    r = ipx("p2g", "video", "a drone shot", "--seconds", "8", "--size", "1280x720", "-o", str(out))
    assert r.exit_code == 0, r.output
    assert out.read_bytes() == MP4
    sent = ipx.fake.last("/v1/videos")["json"]
    assert sent == {"prompt": "a drone shot", "size": "1280x720", "seconds": 8}
    assert state["polls"] == 3


def test_video_failed_job(ipx):
    sign_in(ipx)
    from ipx.errors import IpxError
    ipx.fake.route("POST", "/gen/videos", status=201, body={"id": "v2", "status": "pending"})
    ipx.fake.route("GET", "/gen/videos/v2", body={"id": "v2", "status": "failed", "error": {"message": "upstream exploded"}})
    with pytest.raises(IpxError) as e:
        ipx("gen", "video", "boom")
    assert "upstream exploded" in e.value.message


def test_video_no_wait_prints_id(ipx):
    sign_in(ipx)
    ipx.fake.route("POST", "/gen/videos", status=201, body={"id": "vid_9", "status": "pending"})
    r = ipx("gen", "video", "x", "--no-wait")
    assert "vid_9" in r.output


def test_3d_flow_saves_glb(ipx, tmp_path):
    sign_in(ipx)
    glb = b"glTF" + b"\x00" * 20
    ipx.fake.route("POST", "/v1/3d/generations", status=202, body={"job_id": "j1", "status": "pending"})
    ipx.fake.route("GET", "/v1/3d/jobs/j1", body={"job_id": "j1", "status": "completed", "usage": {"payg_credits_charged": 0.02},
                                                  "data": [{"model_glb_b64_bytes": base64.b64encode(glb).decode()}]})
    img = tmp_path / "chair.jpg"
    img.write_bytes(JPEG)
    r = ipx("p2g", "3d", "--image", str(img), "-m", "tripoSR", "-o", str(tmp_path / "chair.glb"))
    assert r.exit_code == 0, r.output
    assert (tmp_path / "chair.glb").read_bytes() == glb
    body = ipx.fake.last("/v1/3d/generations")["json"]
    assert body["model"] == "tripoSR" and body["image_urls"][0].startswith("data:image/jpeg")


def test_gen_3d_points_to_p2g(ipx):
    sign_in(ipx)
    from ipx.errors import IpxError
    with pytest.raises(IpxError) as e:
        ipx("gen", "3d")
    assert "p2g" in e.value.hint


def test_audio_speech_and_sfx_save_bytes(ipx, tmp_path):
    sign_in(ipx)
    ipx.fake.route("POST", "/v1/audio/speech", body=MP3, headers={"Content-Type": "audio/mpeg", "X-Payg-Credits-Charged": "0.01"})
    ipx.fake.route("POST", "/gen/audio/generations", body=MP3, headers={"Content-Type": "audio/mpeg"})
    r = ipx("p2g", "audio", "speech", "hello there", "--voice", "alloy", "-o", str(tmp_path / "hello.mp3"))
    assert (tmp_path / "hello.mp3").read_bytes() == MP3 and "0.01 credits" in r.output
    assert ipx.fake.last("/v1/audio/speech")["json"] == {"input": "hello there", "voice": "alloy"}
    ipx("gen", "audio", "sfx", "rain", "--seconds", "12", "-o", str(tmp_path / "rain.mp3"))
    assert ipx.fake.last("/gen/audio/generations")["json"] == {"prompt": "rain", "duration_seconds": 12}


def test_audio_transcribe_sends_multipart(ipx, tmp_path):
    sign_in(ipx)
    ipx.fake.route("POST", "/gen/audio/transcriptions", body={"text": "hello transcript"})
    f = tmp_path / "a.mp3"
    f.write_bytes(MP3)
    r = ipx("gen", "audio", "transcribe", str(f), "--language", "en")
    assert "hello transcript" in r.output
    call = ipx.fake.last("/gen/audio/transcriptions")
    assert call["headers"]["content-type"].startswith("multipart/form-data; boundary=")
    assert b'name="file"; filename="a.mp3"' in call["raw"] and b'name="language"' in call["raw"]


def test_audio_response_json_with_base64(ipx, tmp_path):
    sign_in(ipx)
    ipx.fake.route("POST", "/gen/audio/speech/with-timestamps", body={"audio_base64": base64.b64encode(MP3).decode(), "alignment": {"words": [1]}})
    ipx("gen", "audio", "timestamps", "hi", "-o", str(tmp_path / "t.mp3"))
    assert (tmp_path / "t.mp3").read_bytes() == MP3
    assert json.loads((tmp_path / "t.json").read_text())["alignment"] == {"words": [1]}


def test_p2g_balance_renders_wallet(ipx):
    sign_in(ipx)
    ipx.fake.route("GET", "/v1/me", body={"identity": {"email": "me@example.com"}, "wallet": {"balance_credits": "42.5", "total_purchased": "100", "total_spent": "57.5"},
                                          "usage_summary": {"text": {"total_tokens": 1500, "total_credits": "0.01"}}})
    r = ipx("p2g", "balance")
    assert "42.5" in r.output and "57.5" in r.output


def test_error_mapping_402(ipx):
    sign_in(ipx)
    from ipx.errors import ApiError
    ipx.fake.route("POST", "/v1/images/generations", status=402, body={"detail": "Insufficient credits"})
    with pytest.raises(ApiError) as e:
        ipx("p2g", "image", "x")
    assert e.value.status == 402 and "ipx p2g balance" in e.value.hint


def test_models_table(ipx):
    sign_in(ipx)
    ipx.fake.route("GET", "/gen/models", body={"data": [{"id": "lightning", "name": "Lightning", "context_length": 128000,
                                                      "input_modalities": ["text", "image"], "output_modalities": ["text"], "pricing": {"prompt": "0.00000075"}}]})
    r = ipx("gen", "models")
    assert "lightning" in r.output and "128,000" in r.output and "$0.75/M" in r.output


def test_usage_get_combines_sources(ipx):
    sign_in(ipx)
    ipx.fake.route("GET", "/usage", body={"plan_name": "Light", "usage": {"cloudChatDaily": {"limit": 100, "used": 25}, "imagesDaily": {"limit": None, "used": 3}}})
    ipx.fake.route("GET", "/v1/me", body={"wallet": {"balance_credits": "3.5", "total_spent": "1"}})
    r = ipx("usage", "get")
    assert "25 / 100" in r.output and "unlimited" in r.output and "3.5" in r.output
    data = json.loads(ipx("--json", "usage", "get").output)
    assert set(data) == {"quota", "wallet"}
