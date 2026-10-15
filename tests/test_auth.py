import json

from conftest import sign_in


def session_body(email="me@example.com"):
    return {"access_token": "tok_pw", "refresh_token": "rt_pw", "expires_in": 3600,
            "user": {"id": "u-1", "email": email, "app_metadata": {"provider": "email"}}}


def test_password_login_status_logout(ipx):
    ipx.fake.route("POST", "/sb/auth/v1/token", body=session_body())
    ipx.fake.route("GET", "/subscription", body={"email": "me@example.com", "plan_key": "light", "plan_name": "Light Plan"})
    r = ipx("auth", "login", "--email", "me@example.com", "--password-stdin", input="hunter2\n")
    assert r.exit_code == 0, r.output
    assert "Signed in as" in r.output
    sent = ipx.fake.last("/sb/auth/v1/token")
    assert sent["json"] == {"email": "me@example.com", "password": "hunter2"}
    assert "grant_type=password" in sent["query"]
    assert sent["headers"]["apikey"] == "pk_test"

    r = ipx("--json", "auth", "status")
    data = json.loads(r.output)
    assert data["signed_in"] and data["email"] == "me@example.com" and data["plan"] == "Light Plan"
    # the stored token is used as the bearer
    assert ipx.fake.last("/subscription")["headers"]["authorization"] == "Bearer tok_pw"

    ipx.fake.route("POST", "/sb/auth/v1/logout", body={})
    r = ipx("auth", "logout")
    assert r.exit_code == 0 and "Signed out" in r.output
    r = ipx("auth", "status")
    assert r.exit_code == 1


def test_bad_password_is_friendly(ipx):
    ipx.fake.route("POST", "/sb/auth/v1/token", status=400, body={"error_description": "Invalid login credentials"})
    from ipx.errors import IpxError
    try:
        ipx("auth", "login", "--email", "a@b.co", "--password", "nope")
    except IpxError as e:
        assert "Incorrect email or password" in e.message
    else:
        raise AssertionError("expected IpxError")


def test_api_key_login(ipx):
    ipx.fake.route("GET", "/v1/me", body={"identity": {"email": "svc@example.com", "auth_type": "api_key"}, "wallet": {}})
    r = ipx("auth", "login", "--api-key", "ipa_live_abc")
    assert r.exit_code == 0 and "svc@example.com" in r.output
    assert ipx.fake.last("/v1/me")["headers"]["authorization"] == "Bearer ipa_live_abc"


def test_context_resolves_slug_name_and_id(ipx):
    sign_in(ipx)
    ipx.fake.route("GET", "/v1/orgs", body={"orgs": [{"id": "o-1", "slug": "acme", "name": "Acme Inc", "role": "admin"},
                                                    {"id": "o-2", "slug": "beta", "name": "Beta", "role": "member"}]})
    r = ipx("auth", "context", "Acme Inc")
    assert r.exit_code == 0 and "acme" in r.output
    from ipx.config import Config
    assert Config().org == "acme"
    r = ipx("auth", "context", "o-2")
    assert Config().org == "beta"
    r = ipx("auth", "context", "--clear")
    assert Config().org is None


def test_context_unknown_org(ipx):
    sign_in(ipx)
    ipx.fake.route("GET", "/v1/orgs", body={"orgs": [{"id": "o-1", "slug": "acme", "name": "Acme", "role": "admin"}]})
    from ipx.errors import IpxError
    try:
        ipx("auth", "context", "nope")
    except IpxError as e:
        assert "not a member" in e.message and "acme" in e.hint
    else:
        raise AssertionError("expected IpxError")


def test_api_key_cannot_list_orgs(ipx):
    from ipx.config import Credentials
    Credentials().store_api_key("ipa_live_x", email="svc@example.com")
    from ipx.errors import IpxError
    try:
        ipx("org", "list")
    except IpxError as e:
        assert "signed-in session" in e.message
    else:
        raise AssertionError("expected IpxError")


def test_org_header_is_sent(ipx):
    sign_in(ipx)
    ipx.fake.route("GET", "/v1/me", body={"identity": {}, "wallet": {"balance_credits": "5"}, "usage_summary": {}})
    ipx("-O", "acme", "p2g", "balance")
    assert ipx.fake.last("/v1/me")["headers"]["x-inferenceport-org"] == "acme"
    ipx("--personal", "-O", "acme", "p2g", "balance")
    assert "x-inferenceport-org" not in ipx.fake.last("/v1/me")["headers"]


def test_parse_callback_url():
    from ipx.auth import parse_callback
    s = parse_callback("https://inference.js.org/authcallback.html?rd=x#access_token=AAA&refresh_token=BBB&expires_in=3600&token_type=bearer")
    assert s["access_token"] == "AAA" and s["refresh_token"] == "BBB" and s["expires_at"] > 0


def test_expired_session_refreshes(ipx):
    from ipx.config import Credentials
    c = Credentials()
    c.store_session({"access_token": "old", "refresh_token": "rt_old", "expires_at": 1, "user": {"id": "u", "email": "e@x.co"}})
    ipx.fake.route("POST", "/sb/auth/v1/token", body=session_body("e@x.co"))
    ipx.fake.route("GET", "/subscription", body={"email": "e@x.co", "plan_name": "Free"})
    ipx("--json", "auth", "status")
    assert ipx.fake.last("/subscription")["headers"]["authorization"] == "Bearer tok_pw"
    assert "grant_type=refresh_token" in ipx.fake.last("/sb/auth/v1/token")["query"]
