"""Buckets discovery reuses global context without sending it as authority."""

from click.testing import CliRunner

from ipx.cli import _register, hoist_globals, root
from ipx.http import Transport


def test_bucket_commands_use_public_catalog_and_preserve_org_flags(monkeypatch, home):
    calls = []

    def get(self, path, **kwargs):
        calls.append((path, kwargs, self.org))
        return {"status": "not_available", "endpoint": "https://api.inferenceport.ai/s3/",
                "sdk_compatibility": "unverified", "pricing": {"status": "unavailable", "meters": []}}

    monkeypatch.setattr(Transport, 'get', get)
    _register()
    runner = CliRunner()
    for command in ['status', 'pricing']:
        result = runner.invoke(root, hoist_globals(['bucket', command, '--org', 'acme', '--json']))
        assert result.exit_code == 0, result.output
        assert 'unavailable' in result.output or 'not_available' in result.output
    assert calls == [('/v1/buckets/status', {'auth': False, 'org': False}, 'acme'),
                     ('/v1/buckets/pricing', {'auth': False, 'org': False}, 'acme')]


def test_existing_commands_and_bucket_help_remain_registered(home):
    _register()
    assert {'bucket', 'auth', 'org', 'gen', 'p2g', 'model'} <= set(root.commands)
    result = CliRunner().invoke(root, ['bucket', '--help'])
    assert result.exit_code == 0 and 'not available yet' in result.output
    assert 'pricing' in result.output and 'status' in result.output
