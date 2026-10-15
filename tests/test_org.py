
import pytest
from conftest import sign_in

from ipx.errors import ApiError, IpxError, NeedsOrg


def with_org(ipx, slug="acme"):
    sign_in(ipx)
    from ipx.config import Config
    Config().set_org(slug)


def test_org_commands_need_context(ipx):
    sign_in(ipx)
    with pytest.raises(NeedsOrg):
        ipx("org", "members", "list")


def test_org_list_marks_context(ipx):
    with_org(ipx)
    ipx.fake.route("GET", "/v1/orgs", body={"orgs": [{"slug": "acme", "name": "Acme", "role": "owner", "plan": "team", "member_count": 4},
                                                    {"slug": "beta", "name": "Beta", "role": "member", "plan": "free", "member_count": 1}]})
    r = ipx("org", "list")
    assert "● acme" in r.output and "beta" in r.output and "active context" in r.output


def test_create_org_sets_context(ipx):
    sign_in(ipx)
    ipx.fake.route("POST", "/v1/orgs", status=201, body={"org": {"slug": "newco"}})
    ipx("org", "create", "--name", "New Co", "--slug", "NewCo")
    assert ipx.fake.last("/v1/orgs")["json"] == {"name": "New Co", "slug": "newco"}
    from ipx.config import Config
    assert Config().org == "newco"


def test_members_list_and_role(ipx):
    with_org(ipx)
    ipx.fake.route("GET", "/v1/orgs/acme/members", body={"members": [{"email": "a@acme.com", "role": "owner", "user_id": "u1"}, {"email": "b@acme.com", "role": "member", "user_id": "u2"}]})
    ipx.fake.route("PATCH", "/v1/orgs/acme/members/u2", body={"ok": True})
    r = ipx("org", "members", "list")
    assert "a@acme.com" in r.output and "b@acme.com" in r.output
    ipx("org", "members", "role", "u2", "admin")
    assert ipx.fake.last("/v1/orgs/acme/members/u2")["json"] == {"role": "admin"}
    assert ipx.fake.last("/v1/orgs/acme/members/u2")["headers"]["x-inferenceport-org"] == "acme"


def test_invite_body(ipx):
    with_org(ipx)
    ipx.fake.route("POST", "/v1/orgs/acme/invitations", status=201, body={"invite_url": "https://console.inferenceport.ai/?org_invite=tok"})
    r = ipx("org", "invite", "create", "dev@acme.com", "--role", "auditor", "--team", "t1", "--no-email")
    assert ipx.fake.last("/v1/orgs/acme/invitations")["json"] == {"email": "dev@acme.com", "role": "auditor", "send_email": False, "team_ids": ["t1"]}
    assert "org_invite=tok" in r.output


def test_org_override_flag_beats_context(ipx):
    with_org(ipx, "acme")
    ipx.fake.route("GET", "/v1/orgs/other/teams", body={"teams": []})
    ipx("-O", "other", "org", "teams", "list")
    assert any(c["path"] == "/v1/orgs/other/teams" for c in ipx.fake.calls)


def test_key_create_shows_secret_once_and_restrictions(ipx):
    with_org(ipx)
    ipx.fake.route("POST", "/v1/orgs/acme/api-keys", status=201, body={"apiKey": {"id": "k1"}, "rawKey": "ipa_live_SECRET"})
    r = ipx("org", "keys", "create", "ci", "--monthly-limit", "25", "--models", "a, b", "--ip", "10.0.0.0/8", "--rpm", "30")
    assert "ipa_live_SECRET" in r.output
    assert ipx.fake.last("/v1/orgs/acme/api-keys")["json"] == {
        "name": "ci", "monthly_credit_limit": 25.0, "allowed_models": ["a", "b"], "ip_allowlist": ["10.0.0.0/8"], "rate_limit_rpm": 30}


def test_member_limits(ipx):
    with_org(ipx)
    ipx.fake.route("PATCH", "/v1/orgs/acme/members/u9/limits", body={})
    ipx("org", "members", "limits", "u9", "--monthly-credits", "10", "--modalities", "text,image")
    assert ipx.fake.last("/v1/orgs/acme/members/u9/limits")["json"] == {"monthly_credit_limit": 10.0, "allowed_modalities": ["text", "image"]}
    with pytest.raises(IpxError):
        ipx("org", "members", "limits", "u9")


def test_delete_requires_typed_slug(ipx):
    with_org(ipx)
    ipx.fake.route("DELETE", "/v1/orgs/acme", body={})
    # --yes skips the prompt entirely
    ipx("--yes", "org", "delete")
    assert ipx.fake.last("/v1/orgs/acme")["json"]["confirm"] == "acme"
    from ipx.config import Config
    assert Config().org is None


def test_usage_breakdown_and_export(ipx, tmp_path):
    with_org(ipx)
    ipx.fake.route("GET", "/v1/orgs/acme/usage/breakdown", body={"rows": [{"user": "a@acme.com", "credits": 3.2}]})
    ipx.fake.route("GET", "/v1/orgs/acme/usage/export", body="user,credits\na,1\n", headers={"Content-Type": "text/csv"})
    r = ipx("org", "usage", "breakdown", "--days", "7", "--by", "model")
    assert "a@acme.com" in r.output
    assert "days=7" in ipx.fake.last("/v1/orgs/acme/usage/breakdown")["query"] and "by=model" in ipx.fake.last("/v1/orgs/acme/usage/breakdown")["query"]
    out = tmp_path / "u.csv"
    ipx("org", "usage", "export", "-o", str(out))
    assert out.read_text() == "user,credits\na,1\n"


def test_server_403_has_hint(ipx):
    with_org(ipx)
    ipx.fake.route("GET", "/v1/orgs/acme/billing", status=403, body={"detail": "Your role cannot do this (organization policy)"})
    with pytest.raises(ApiError) as e:
        ipx("org", "billing", "info")
    assert e.value.status == 403 and e.value.hint


def test_desktop_policy_set_flags(ipx):
    with_org(ipx)
    ipx.fake.route("PUT", "/v1/orgs/acme/desktop-policy", body={"desktop": {}})
    ipx("org", "desktop-policy", "set", "--pull-mode", "allowlist", "--pull-models", "llama3.2,qwen2.5", "--block-import", "--server-hosting", "admins")
    assert ipx.fake.last("/v1/orgs/acme/desktop-policy")["json"] == {
        "pull_mode": "allowlist", "pull_models": ["llama3.2", "qwen2.5"], "allow_model_import": False, "server_hosting": "admins"}


def test_audit_list_filters(ipx):
    with_org(ipx)
    ipx.fake.route("GET", "/v1/orgs/acme/audit", body={"events": [{"created_at": "2026-01-01T10:00:00Z", "actor_email": "a@acme.com", "action": "member.role_changed"}]})
    r = ipx("org", "audit", "list", "--action", "member.role_changed", "--limit", "5")
    assert "member.role_changed" in r.output
    q = ipx.fake.last("/v1/orgs/acme/audit")["query"]
    assert "action=member.role_changed" in q and "limit=5" in q
