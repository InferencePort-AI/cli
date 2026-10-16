import json

import pytest
from conftest import sign_in

from ipx.errors import IpxError
from ipx.mcp import council_url, parse_rpc

TIERS = {"basic": {"name": "Basic", "description": "Fast", "surcharge_credits": 0.05, "model_count": 3, "judge_count": 1}}
ANSWER = {"answer": "42, with caveats.", "council": {"tier": "basic", "models_used": ["a", "b"], "iterations_used": 1, "failed_models": []},
          "billing": {"surcharge_credits": 0.05, "charged": True}, "usage": {"total_tokens": 1234}, "iteration_log": [{"round": 1}]}


def mcp_server(ipx, tool_results, *, sse=False, error_tool=None):
    seen = []

    def handler(rec):
        msg = rec["json"] or {}
        seen.append((msg.get("method"), rec["headers"]))
        if "id" not in msg:
            return 202, {}, {}
        m = msg["method"]
        if m == "initialize":
            result = {"protocolVersion": "2025-03-26", "serverInfo": {"name": "council-of-llms", "version": "1"}, "capabilities": {}}
            hdr = {"mcp-session-id": "sess-1"}
        elif m == "tools/list":
            result, hdr = {"tools": [{"name": "council_reason", "description": "Route a problem.\nMore."}]}, {}
        else:
            name = msg["params"]["name"]
            hdr = {}
            if name == error_tool:
                result = {"isError": True, "content": [{"type": "text", "text": "Insufficient credits: the basic council costs 0.05"}]}
            else:
                result = {"content": [{"type": "text", "text": json.dumps(tool_results[name])}]}
        body = {"jsonrpc": "2.0", "id": msg["id"], "result": result}
        if sse:
            return 200, {"Content-Type": "text/event-stream", **hdr}, f"event: message\ndata: {json.dumps(body)}\n\n"
        return 200, hdr, body

    ipx.fake.route("POST", "/mcp/council/", handler)
    return seen


def test_council_url_and_parse():
    assert council_url("https://us-east-2.api.inferenceport.ai") == "https://api.inferenceport.ai/mcp/council/"
    assert council_url("http://x:1/") == "http://x:1/mcp/council/"
    assert parse_rpc('event: message\ndata: {"id": 2, "result": {"a": 1}}\n\n', 2) == {"id": 2, "result": {"a": 1}}
    assert parse_rpc("") is None


@pytest.mark.parametrize("sse", [False, True])
def test_tiers_handshake_and_session(ipx, sse):
    sign_in(ipx)
    seen = mcp_server(ipx, {"council_list_tiers": TIERS}, sse=sse)
    r = ipx("mcp", "council", "tiers")
    assert "Basic" in r.output and "0.05" in r.output
    assert [m for m, _ in seen] == ["initialize", "notifications/initialized", "tools/call"]
    assert seen[0][1]["authorization"] == "Bearer tok_abc" and "mcp-session-id" not in seen[0][1]
    assert seen[2][1]["mcp-session-id"] == "sess-1"


def test_reason_builds_arguments_and_org_header(ipx, tmp_path):
    sign_in(ipx)
    from ipx.config import Config
    Config().set_org("acme")
    seen = mcp_server(ipx, {"council_reason": ANSWER})
    f = tmp_path / "q.md"
    f.write_text("context")
    r = ipx("mcp", "council", "reason", "Why?", "-f", str(f), "-t", "pro", "-r", "2", "--model", "m1", "--judge", "j1")
    call = [c for c in ipx.fake.calls if (c["json"] or {}).get("method") == "tools/call"][-1]["json"]["params"]
    assert call["name"] == "council_reason"
    a = call["arguments"]
    assert a["tier"] == "pro" and a["iteration_rounds"] == 2 and a["custom_models"] == ["m1"] and a["custom_judges"] == ["j1"]
    assert a["problem"].startswith("Why?") and "context" in a["problem"]
    assert all(h.get("x-inferenceport-org") == "acme" for _, h in seen)
    assert "42, with caveats." in r.output


def test_reason_json_drops_log_unless_asked(ipx):
    sign_in(ipx)
    mcp_server(ipx, {"council_reason": ANSWER})
    d = json.loads(ipx("--json", "mcp", "council", "reason", "Why?").output)
    assert d["answer"] == "42, with caveats." and "iteration_log" not in d
    assert "iteration_log" in json.loads(ipx("--json", "mcp", "council", "reason", "Why?", "--log").output)


def test_tool_error_surfaces_server_message(ipx):
    sign_in(ipx)
    mcp_server(ipx, {}, error_tool="council_reason")
    with pytest.raises(IpxError) as e:
        ipx("mcp", "council", "reason", "Why?")
    assert "Insufficient credits" in e.value.message


def test_reason_needs_a_problem(ipx):
    sign_in(ipx)
    mcp_server(ipx, {"council_reason": ANSWER})  # would answer if the check were missing
    with pytest.raises(IpxError) as e:
        ipx("mcp", "council", "reason")
    assert e.value.message == "No problem given." and not ipx.fake.calls


def test_connect_snippets_include_org(ipx):
    sign_in(ipx)
    assert "claude mcp add --transport http council" in ipx("mcp", "council", "connect").output
    out = ipx("-O", "acme", "mcp", "council", "connect", "cursor").output
    assert '"X-InferencePort-Org": "acme"' in out and "/mcp/council/" in out


def test_connect_quotes_shell_metacharacters(ipx):
    import shlex

    sign_in(ipx)
    out = ipx("-O", 'x"; touch /tmp/pwned; "', "mcp", "council", "connect", "claude-code").output
    line = next(ln for ln in out.splitlines() if ln.startswith("claude mcp add"))
    argv = shlex.split(line)
    assert argv[:6] == ["claude", "mcp", "add", "--transport", "http", "council"] and len(argv) == 9
    assert argv[-1] == 'X-InferencePort-Org: x"; touch /tmp/pwned; "'


def test_rejected_token_refreshes_once_and_retries(ipx):
    from ipx.config import Credentials

    sign_in(ipx)
    state = {"calls": 0}
    mcp_server(ipx, {"council_list_tiers": TIERS})
    ok = ipx.fake.routes[("POST", "/mcp/council/")]

    def flaky(rec):
        if rec["headers"].get("authorization") == "Bearer tok_abc":
            state["calls"] += 1
            return 401, {}, {"detail": "token rejected"}
        return ok(rec)

    ipx.fake.route("POST", "/mcp/council/", flaky)
    ipx.fake.route("POST", "/sb/auth/v1/token", body={"access_token": "tok_new", "refresh_token": "rt_2", "expires_at": 4102444800, "user": {"id": "u1", "email": "me@example.com"}})
    assert "Basic" in ipx("mcp", "council", "tiers").output
    assert state["calls"] == 1 and Credentials().access_token == "tok_new"
