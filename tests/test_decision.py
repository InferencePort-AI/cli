import json

import pytest
from conftest import sign_in

QUESTION = {"support": {"type": "noul", "instructions": "Is this support?"}}
REPLY = {"model": "microsoft/decision-1", "answers": {"support": {"type": "noul", "noul": 0.9}},
         "usage": {"input_tokens": 20, "payg_credits_charged": 0}}


def test_decision_file_and_output(ipx, tmp_path):
    sign_in(ipx)
    path = tmp_path / "request.json"
    path.write_text(json.dumps({"state": "Help", "questions": QUESTION}))
    output = tmp_path / "answer.json"
    ipx.fake.route("POST", "/v1/systemone", body=REPLY)
    result = ipx("--json", "p2g", "decision", "-f", str(path), "-o", str(output))
    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == REPLY == json.loads(output.read_text())
    assert ipx.fake.last("/v1/systemone")["json"]["model"] == "microsoft/decision-1"
    assert ipx.fake.last("/v1/systemone")["headers"]["authorization"] == "Bearer tok_abc"


def test_questions_and_stdin(ipx, tmp_path):
    sign_in(ipx)
    path = tmp_path / "questions.json"
    path.write_text(json.dumps(QUESTION))
    ipx.fake.route("POST", "/v1/systemone", body=REPLY)
    r = ipx("--json", "p2g", "decision", "-q", str(path), input="Help me")
    assert r.exit_code == 0
    assert ipx.fake.last("/v1/systemone")["json"]["state"] == "Help me"


def test_full_request_stdin_and_model_override(ipx):
    sign_in(ipx)
    ipx.fake.route("POST", "/v1/systemone", body=REPLY)
    r = ipx("--json", "p2g", "decision", "-f", "-", "-m", "vllm-sr/d3",
            input=json.dumps({"model": "old", "state": "Help", "questions": QUESTION}))
    assert r.exit_code == 0, r.output
    assert ipx.fake.last("/v1/systemone")["json"]["model"] == "vllm-sr/d3"


def test_decision_pricing():
    from ipx.pricing import price_of
    p = price_of({"type": "decision", "pricing": {"prompt": "0.000000042"}})
    assert p.input == pytest.approx(0.042)
    assert p.unit == "1M tokens"


def test_discovery_and_prices_commands(ipx):
    sign_in(ipx)
    model = {"id": "microsoft/decision-1", "type": "decision", "name": "Microsoft Decision 1",
             "pricing": {"prompt": "0.000000042"}, "output_modalities": ["decision"]}
    ipx.fake.route("GET", "/v1/decision/models", body={"data": [model]})
    ipx.fake.route("GET", "/v1/models", body={"data": [model]})
    r = ipx("--json", "p2g", "models", "--type", "decision")
    assert r.exit_code == 0, r.output
    assert json.loads(r.output)[0]["id"] == model["id"]
    r = ipx("--json", "p2g", "prices", "--type", "decision")
    assert r.exit_code == 0, r.output
    assert json.loads(r.output)[0]["kind"] == "decision"


def test_decision_image_and_video_files(ipx, tmp_path):
    import base64

    sign_in(ipx)
    questions = tmp_path / "questions.json"
    questions.write_text(json.dumps(QUESTION))
    photo = tmp_path / "rover.jpg"
    photo.write_bytes(b"fake-jpeg")
    clip = tmp_path / "launch.mp4"
    clip.write_bytes(b"fake-mp4")
    ipx.fake.route("POST", "/v1/systemone", body=REPLY)
    r = ipx("--json", "p2g", "decision", "Mars scene", "-q", str(questions),
            "-m", "vllm-sr-d3-flash", "--image", str(photo), "--video", str(clip))
    assert r.exit_code == 0, r.output
    sent = ipx.fake.last("/v1/systemone")["json"]
    assert sent["model"] == "vllm-sr-d3-flash"
    assert sent["images"] == ["data:image/jpeg;base64," + base64.b64encode(b"fake-jpeg").decode()]
    assert sent["videos"] == ["data:video/mp4;base64," + base64.b64encode(b"fake-mp4").decode()]


def test_decision_named_assets_with_full_request(ipx, tmp_path):
    sign_in(ipx)
    request_file = tmp_path / "request.json"
    request_file.write_text(json.dumps({"model": "vllm-sr-d3-turbo", "state": "Animal: <a>", "questions": QUESTION}))
    assets = tmp_path / "assets.json"
    assets.write_text(json.dumps({"<a>": {"type": "image", "data_base64": "YWJj"}}))
    ipx.fake.route("POST", "/v1/systemone", body=REPLY)
    r = ipx("--json", "p2g", "decision", "--file", str(request_file), "--assets", str(assets))
    assert r.exit_code == 0, r.output
    sent = ipx.fake.last("/v1/systemone")["json"]
    assert sent["model"] == "vllm-sr-d3-turbo"
    assert sent["assets"] == {"<a>": {"type": "image", "data_base64": "YWJj"}}


def test_decision_rejects_unsupported_media_extension(ipx, tmp_path):
    sign_in(ipx)
    questions = tmp_path / "questions.json"
    questions.write_text(json.dumps(QUESTION))
    invalid = tmp_path / "archive.zip"
    invalid.write_bytes(b"not media")
    from ipx.errors import IpxError

    with pytest.raises(IpxError, match="Unsupported image extension"):
        ipx("p2g", "decision", "State", "-q", str(questions), "--image", str(invalid))
