"""Every command must render --help, and the documented command tree must exist."""

import click
import pytest

from ipx.cli import _register, root


def walk(cmd, path=()):
    yield path, cmd
    if isinstance(cmd, click.Group):
        ctx = click.Context(cmd)
        for name in cmd.list_commands(ctx):
            yield from walk(cmd.get_command(ctx, name), path + (name,))


@pytest.fixture(scope="module", autouse=True)
def registered():
    _register()


def test_all_commands_render_help(ipx):
    n = 0
    for path, cmd in walk(root):
        r = ipx(*path, "--help")
        assert r.exit_code == 0, (path, r.output)
        assert "USAGE" in r.output, path
        n += 1
    assert n > 150


@pytest.mark.parametrize("path", [
    ("auth", "login"), ("auth", "logout"), ("auth", "signout"), ("auth", "context"), ("auth", "status"), ("auth", "whoami"),
    ("org", "list"), ("org", "members", "list"), ("org", "keys", "create"), ("org", "billing", "info"), ("org", "sso", "get"),
    ("p2g", "chat"), ("p2g", "text"), ("p2g", "image"), ("p2g", "video"), ("p2g", "audio", "speech"), ("p2g", "3d"), ("p2g", "balance"),
    ("gen", "chat"), ("gen", "text"), ("gen", "image"), ("gen", "video"), ("gen", "audio", "sfx"), ("gen", "plan"),
    ("server", "start"), ("server", "stop"), ("server", "status"), ("server", "logs"),
    ("usage", "get"), ("model", "pull"), ("model", "list"), ("local", "chat"), ("import", "space"), ("import", "website"),
    ("featherless", "deploy"), ("featherless", "list"), ("featherless", "terminate"), ("featherless", "uncancel"), ("agent", "create"), ("agent", "chat"),
    ("agent", "invite", "accept"), ("agent", "members", "role"), ("mcp", "council", "reason"), ("mcp", "council", "tiers"), ("mcp", "council", "connect"),
    ("config", "set"), ("doctor",), ("status",), ("api",), ("completion",),
])
def test_documented_commands_exist(path):
    cmd = root
    for part in path:
        cmd = cmd.get_command(click.Context(cmd), part)
        assert cmd is not None, path


def test_aliases_are_hidden_but_work(ipx):
    assert "signout" not in ipx("auth", "--help").output
    assert "whoami" not in ipx("auth", "--help").output


def test_json_flag_is_global(ipx):
    import json
    r = ipx("--json", "server", "status")
    json.loads(r.output)


def test_config_roundtrip(ipx):
    ipx("config", "set", "default_gen_model", "lightning")
    assert ipx("config", "get", "default_gen_model").output.strip() == "lightning"
    ipx("config", "unset", "default_gen_model")
    from ipx.errors import IpxError
    with pytest.raises(IpxError):
        ipx("config", "get", "default_gen_model")
    with pytest.raises(IpxError):
        ipx("config", "set", "bogus", "1")


def test_credentials_file_is_private(ipx):
    import os
    import sys

    from conftest import sign_in

    from ipx.config import Credentials
    sign_in(ipx)
    if sys.platform != "win32":
        assert oct(os.stat(Credentials().path).st_mode & 0o777) == "0o600"


def test_client_id_is_valid_and_stable(home):
    from ipx.config import CLIENT_ID_RE, Config
    a = Config().client_id
    assert CLIENT_ID_RE.match(a) and Config().client_id == a


def test_global_flags_can_follow_the_command():
    from ipx.cli import hoist_globals
    assert hoist_globals(["usage", "get", "--json"]) == ["--json", "usage", "get"]
    assert hoist_globals(["org", "members", "list", "-O", "acme", "--yes"]) == ["-O", "acme", "--yes", "org", "members", "list"]
    assert hoist_globals(["gen", "text", "--org=acme", "hi"]) == ["--org=acme", "gen", "text", "hi"]
    # after `--` and for ambiguous short flags nothing moves
    assert hoist_globals(["gen", "text", "--", "--json"]) == ["gen", "text", "--", "--json"]
    assert hoist_globals(["api", "GET", "/x", "-q", "a=b"]) == ["api", "GET", "/x", "-q", "a=b"]
