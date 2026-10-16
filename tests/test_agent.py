
import pytest
from conftest import sign_in

from ipx.errors import IpxError

AGENT = {"id": "agent/acme-bot/support", "name": "Support", "backing_model": "lightning", "visibility": "private", "payer": "caller",
         "is_owner": True, "role": "owner", "owner_username": "acme-bot"}


def test_list_and_registry(ipx):
    sign_in(ipx)
    ipx.fake.route("GET", "/v1/agents/models", body={"data": [AGENT, {**AGENT, "id": "agent/x/y", "is_owner": False, "role": "user"}]})
    ipx.fake.route("GET", "/v1/agents/registry", body={"data": [{**AGENT, "visibility": "public"}]})
    assert "acme-bot/support" in ipx("agent", "list").output
    assert "x/y" not in ipx("agent", "list", "--mine").output
    ipx("agent", "registry", "-q", "bot")
    assert ipx.fake.last("/v1/agents/registry")["query"] == "q=bot&limit=50"


def test_create_builds_body(ipx, tmp_path):
    sign_in(ipx)
    prompt = tmp_path / "p.md"
    prompt.write_text("Be kind.")
    ipx.fake.route("POST", "/v1/agents", status=201, body={"model_id": "agent/acme-bot/support"})
    r = ipx("agent", "create", "Support", "--name", "Support", "-m", "lightning", "--system-file", str(prompt),
            "--visibility", "public", "--payer", "owner", "--mcp", "docs=https://mcp.example.com/mcp")
    assert ipx.fake.last("/v1/agents")["json"] == {
        "name": "Support", "model": "lightning", "system_prompt": "Be kind.", "visibility": "public", "payer": "owner",
        "mcp_servers": [{"name": "docs", "url": "https://mcp.example.com/mcp", "transport": "auto", "auth_type": "none"}], "agent_id": "support"}
    assert "agent/acme-bot/support" in r.output


def test_bad_mcp_pair_rejected(ipx):
    sign_in(ipx)
    ipx.fake.route("POST", "/v1/agents", status=201, body={"model_id": "agent/u/x1"})  # would succeed if validation were missing
    with pytest.raises(IpxError) as e:
        ipx("agent", "create", "x1", "--name", "X", "-m", "lightning", "--mcp", "nourl")
    assert "NAME=URL" in (e.value.hint or "") and not ipx.fake.calls


def test_bare_id_resolves_username_and_update_only_sends_changes(ipx):
    sign_in(ipx)
    ipx.fake.route("GET", "/v1/agents/profile", body={"username": "acme-bot"})
    ipx.fake.route("PATCH", "/v1/agents/acme-bot/support", body={"model_id": "agent/acme-bot/support"})
    ipx("agent", "update", "support", "--name", "Helper")
    assert ipx.fake.last("/v1/agents/acme-bot/support")["json"] == {"name": "Helper"}
    with pytest.raises(IpxError):
        ipx("agent", "update", "support")


def test_agent_calls_ignore_org_header_and_delete_confirms(ipx):
    sign_in(ipx)
    from ipx.config import Config
    Config().set_org("acme")
    ipx.fake.route("GET", "/v1/agents/acme-bot/support", body={"agent": {**AGENT, "system_prompt": "Be kind.", "members": [{"user_id": "u2", "email": "b@x.io", "role": "editor"}]}})
    ipx.fake.route("DELETE", "/v1/agents/acme-bot/support", body={"deleted": True})
    r = ipx("agent", "get", "agent/acme-bot/support")
    assert "Be kind." in r.output and "b@x.io" in r.output
    assert "x-inferenceport-org" not in ipx.fake.last("/v1/agents/acme-bot/support")["headers"]
    ipx("-y", "agent", "delete", "acme-bot/support")
    assert ipx.fake.last("/v1/agents/acme-bot/support")["method"] == "DELETE"


def test_invites_members_profile_settings(ipx):
    sign_in(ipx)
    ipx.fake.route("POST", "/v1/agents/invite/acme-bot/support", body={"invite_url": "https://console.inferenceport.ai/?agent_invite=tok", "role_label": "Editor"})
    ipx.fake.route("POST", "/v1/agents/invite/tok/accept", body={"accepted": True})
    ipx.fake.route("PATCH", "/v1/agents/acme-bot/support/members/u2", body={"member": {}})
    ipx.fake.route("PUT", "/v1/agents/profile", body={"username": "acme-bot"})
    ipx.fake.route("PUT", "/v1/agents/settings", body={"settings": {"include_in_models": True}})
    r = ipx("agent", "invite", "create", "acme-bot/support", "dev@acme.com", "--role", "editor")
    assert ipx.fake.last("/v1/agents/invite/acme-bot/support")["json"] == {"email": "dev@acme.com", "role": "editor"} and "agent_invite=tok" in r.output
    ipx("agent", "invite", "accept", "https://console.inferenceport.ai/?agent_invite=tok")
    ipx("agent", "members", "role", "acme-bot/support", "u2", "administrator")
    assert ipx.fake.last("/v1/agents/acme-bot/support/members/u2")["json"] == {"role": "administrator"}
    ipx("agent", "profile", "set", "acme-bot")
    ipx("agent", "settings", "set", "--include-in-models")
    assert ipx.fake.last("/v1/agents/settings")["json"] == {"include_in_models": True}


def test_chat_uses_agent_model_and_org_billing(ipx):
    sign_in(ipx)
    from ipx.config import Config
    Config().set_org("acme")
    sse = 'data: {"choices":[{"delta":{"content":"Hi there"}}]}\n\ndata: [DONE]\n\n'
    ipx.fake.route("POST", "/v1/chat/completions", body=sse, headers={"Content-Type": "text/event-stream"})
    r = ipx("agent", "ask", "acme-bot/support", "hello")
    call = ipx.fake.last("/v1/chat/completions")
    assert call["json"]["model"] == "agent/acme-bot/support" and call["headers"]["x-inferenceport-org"] == "acme"
    assert "Hi there" in r.output
