import json

from ipx.pricing import price_of, rows

CATALOG = [
    {"id": "lightning", "name": "Lightning", "output_modalities": ["text"],
     "pricing": {"prompt": "0.00000075", "completion": "0.00000300"}},
    {"id": "free-llm", "name": "Free", "output_modalities": ["text"], "pricing": {"prompt": "0", "completion": "0"}},
    {"id": "img", "name": "Img", "output_modalities": ["image"], "pricing": {"image": "0.02", "prompt": "0"}},
    {"id": "wan", "type": "video", "name": "Wan", "pricing": {"request": "0.01"}},
    {"id": "tts", "type": "audio", "name": "TTS", "billing_unit": "input_chars", "pricing": {"request": "0.015"}},
    {"id": "emb", "type": "embedding", "name": "Emb", "pricing": {"prompt": "0.0000002"}},
    {"id": "tri", "type": "3d", "name": "Tri", "pricing": {"request": "0.5"}},
    {"id": "broken", "pricing": "nope"},
]


def test_price_shapes():
    t = price_of(CATALOG[0])
    assert (t.kind, t.unit) == ("text", "1M tokens")
    assert round(t.input, 6) == 0.75 and round(t.output, 6) == 3.0
    assert "$0.75 in / $3.00 out per 1M tokens" == t.label()
    assert price_of(CATALOG[2]).label() == "$0.02 per image"
    assert price_of(CATALOG[3]).label() == "$0.01 per second"
    assert price_of(CATALOG[4]).unit == "1k chars"
    assert price_of(CATALOG[5]).label() == "$0.20 per 1M tokens"
    assert price_of(CATALOG[6]).label() == "$0.50 per model"
    assert price_of(CATALOG[1]).label() == "free"
    assert price_of(CATALOG[7]).free


def test_rows_filter_and_sort():
    assert [r["id"] for r in rows(CATALOG, kind="text")] == ["broken", "free-llm", "lightning"]
    assert {r["id"] for r in rows(CATALOG, free=True)} == {"free-llm", "broken"}
    assert [r["id"] for r in rows(CATALOG, search="WAN")] == ["wan"]
    assert [r["id"] for r in rows(CATALOG, kind="text", max_price=0.5)] == ["broken", "free-llm"]


def test_command_is_public_and_json(ipx):
    ipx.fake.route("GET", "/v1/models", body={"data": CATALOG})
    r = ipx("--json", "p2g", "prices", "--type", "image")
    assert r.exit_code == 0, r.output
    data = json.loads(r.output)
    assert [d["id"] for d in data] == ["img"] and data[0]["unit"] == "image"
    assert "authorization" not in ipx.fake.last("/v1/models")["headers"]


def test_command_table(ipx):
    ipx.fake.route("GET", "/v1/models", body={"data": CATALOG})
    r = ipx("p2g", "prices")
    assert r.exit_code == 0, r.output
    assert "lightning" in r.output and "per second" in r.output
