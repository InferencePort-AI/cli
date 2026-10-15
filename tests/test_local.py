import json
import os

import pytest

from conftest import sign_in
from ipx.errors import IpxError, LocalUnavailable


def ndjson(*objs):
    return "".join(json.dumps(o) + "\n" for o in objs)


def test_model_list(ipx):
    ipx.ollama.route("GET", "/api/tags", body={"models": [{"name": "llama3.2:latest", "size": 2_000_000_000, "modified_at": "2026-05-01T00:00:00Z",
                                                          "details": {"parameter_size": "3.2B", "quantization_level": "Q4_K_M", "family": "llama"}}]})
    r = ipx("model", "list")
    assert "llama3.2:latest" in r.output and "3.2B" in r.output and "1.9 GB" in r.output


def test_model_pull_progress_and_success(ipx):
    ipx.ollama.route("POST", "/api/pull", body=ndjson({"status": "pulling manifest"},
                                                    {"status": "pulling abc123def456", "digest": "sha256:abc123def456", "total": 1000, "completed": 400},
                                                    {"status": "pulling abc123def456", "digest": "sha256:abc123def456", "total": 1000, "completed": 1000},
                                                    {"status": "success"}))
    r = ipx("model", "pull", "tiny:1b")
    assert r.exit_code == 0 and "Pulled" in r.output
    assert ipx.ollama.last("/api/pull")["json"] == {"model": "tiny:1b", "stream": True}


def test_pull_error_event_is_raised(ipx):
    ipx.ollama.route("POST", "/api/pull", body=ndjson({"error": "pull model manifest: file does not exist"}))
    with pytest.raises(IpxError) as e:
        ipx("model", "pull", "nope")
    assert "does not exist" in e.value.message


def test_rm_requires_yes_or_confirm(ipx):
    ipx.ollama.route("DELETE", "/api/delete", body={})
    ipx("--yes", "model", "rm", "tiny:1b")
    assert ipx.ollama.last("/api/delete")["json"] == {"model": "tiny:1b"}


def test_local_text_streams(ipx):
    ipx.ollama.route("POST", "/api/chat", body=ndjson({"message": {"role": "assistant", "content": "Hel"}, "done": False},
                                                     {"message": {"role": "assistant", "content": "lo"}, "done": False},
                                                     {"message": {"role": "assistant", "content": ""}, "done": True, "prompt_eval_count": 3, "eval_count": 2}))
    r = ipx("local", "text", "tiny:1b", "say hi")
    assert "Hello" in r.output
    body = ipx.ollama.last("/api/chat")["json"]
    assert body["model"] == "tiny:1b" and body["messages"] == [{"role": "user", "content": "say hi"}]


def test_ollama_down_is_friendly(home, monkeypatch):
    monkeypatch.setenv("IPX_OLLAMA_URL", "http://127.0.0.1:1")
    from ipx.local import Ollama
    with pytest.raises(LocalUnavailable) as e:
        Ollama("http://127.0.0.1:1").list()
    assert "ollama.com" in e.value.hint


def test_import_gguf_builds_modelfile(ipx, tmp_path):
    ipx.ollama.route("POST", "/api/create", body=ndjson({"status": "success"}))
    g = tmp_path / "m.gguf"
    g.write_bytes(b"GGUF")
    ipx("model", "import", "gguf", str(g), "--name", "mine", "--system", "Be brief.")
    body = ipx.ollama.last("/api/create")["json"]
    assert body["model"] == "mine" and f"FROM {os.path.abspath(g)}" in body["modelfile"] and 'SYSTEM """Be brief."""' in body["modelfile"]


def test_import_hf_pulls_hf_ref(ipx):
    ipx.ollama.route("POST", "/api/pull", body=ndjson({"status": "success"}))
    ipx("model", "import", "hf", "bartowski/X-GGUF:Q4_K_M")
    assert ipx.ollama.last("/api/pull")["json"]["model"] == "hf.co/bartowski/X-GGUF:Q4_K_M"


def test_models_alias(ipx):
    ipx.ollama.route("GET", "/api/tags", body={"models": []})
    r = ipx("models", "list")
    assert "No local models" in r.output


# ----------------------------------------------------------------- organization policy gates


def policy_for(ipx, **policy):
    sign_in(ipx)
    from ipx.config import Config
    Config().set_org("acme")
    entry = {"slug": "acme", "name": "Acme", "role": "member", "managed": True, "suspended": False, "can_host_server": True, "server_users": [],
             "policy": {"pull_mode": "any", "allow_model_import": True, "server_hosting": "any", "server_access": "manual", "lightning": "allow", **policy}}
    ipx.fake.route("GET", "/v1/orgs/me/desktop-policy", body={"orgs": [entry]})


def test_pull_blocked_by_org(ipx):
    policy_for(ipx, pull_mode="blocked")
    with pytest.raises(IpxError) as e:
        ipx("model", "pull", "llama3.2")
    assert "does not allow pulling models" in e.value.message


def test_pull_allowlist(ipx):
    policy_for(ipx, pull_mode="allowlist", pull_models=["llama3.2", "qwen2.5:7b"])
    ipx.ollama.route("POST", "/api/pull", body=ndjson({"status": "success"}))
    ipx("model", "pull", "llama3.2:1b")  # same base model name is approved
    with pytest.raises(IpxError) as e:
        ipx("model", "pull", "mistral")
    assert "approved model list" in e.value.message


def test_import_blocked_by_org(ipx, tmp_path):
    policy_for(ipx, allow_model_import=False)
    with pytest.raises(IpxError):
        ipx("model", "import", "hf", "a/b")


def test_policy_fails_closed_when_unverifiable(ipx):
    sign_in(ipx)
    from ipx.config import Config
    Config().set_org("acme")  # no route registered: policy fetch 404s and there is no cache
    with pytest.raises(IpxError) as e:
        ipx("model", "pull", "llama3.2")
    assert "Could not verify" in e.value.message


def test_personal_flag_bypasses_org_policy(ipx):
    policy_for(ipx, pull_mode="blocked")
    ipx.ollama.route("POST", "/api/pull", body=ndjson({"status": "success"}))
    ipx("--personal", "model", "pull", "llama3.2")


def test_no_policy_without_context(ipx):
    sign_in(ipx)
    ipx.ollama.route("POST", "/api/pull", body=ndjson({"status": "success"}))
    ipx("model", "pull", "anything")
    assert not any(c["path"] == "/v1/orgs/me/desktop-policy" for c in ipx.fake.calls)
