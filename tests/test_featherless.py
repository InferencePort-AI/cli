
import pytest
from conftest import sign_in

from ipx.errors import IpxError

DEP = {"id": "d1", "featherless_model_id": "Qwen/Qwen2.5-7B", "display_model_id": "hf/Qwen/Qwen2.5-7B", "status": "active",
       "monthly_fee": 0.5, "billing_cycle_end": "2026-11-15T00:00:00+00:00", "model_state": "warm"}


def with_org(ipx, slug="acme"):
    sign_in(ipx)
    from ipx.config import Config
    Config().set_org(slug)


def test_models_needs_no_login(ipx):
    ipx.fake.route("GET", "/v1/deploy/models", body={"data": [{"featherless_model_id": "Qwen/Qwen2.5-7B", "context_length": 32768,
                                                              "our_pricing_per_million": {"input": 0.2, "output": 0.3}}], "total": 1, "page": 1, "total_pages": 1})
    r = ipx("featherless", "models", "--search", "qwen")
    assert "Qwen/Qwen2.5-7B" in r.output and "32,768" in r.output
    assert ipx.fake.last("/v1/deploy/models")["query"] == "search=qwen&page=1&limit=25"
    assert "authorization" not in ipx.fake.last("/v1/deploy/models")["headers"]


def test_personal_list_uses_personal_endpoint(ipx):
    sign_in(ipx)
    ipx.fake.route("GET", "/v1/deploy", body={"data": [DEP, {**DEP, "id": "d2", "status": "expired"}]})
    r = ipx("featherless", "list")
    assert "d1" in r.output and "d2" not in r.output and "your account" in r.output
    assert "d2" in ipx("featherless", "list", "--all").output


def test_org_context_routes_to_org_endpoint(ipx):
    with_org(ipx)
    ipx.fake.route("GET", "/v1/orgs/acme/deployments", body={"data": [{**DEP, "org_id": "o1"}]})
    r = ipx("featherless", "list")
    assert "organization acme" in r.output and "d1" in r.output
    # --personal wins over the saved context
    ipx.fake.route("GET", "/v1/deploy", body={"data": []})
    assert "your account" in ipx("--personal", "featherless", "list").output


def test_deploy_normalizes_model_and_bills_org(ipx):
    with_org(ipx)
    ipx.fake.route("GET", "/v1/deploy/model-status/Qwen/Qwen2.5-7B", body={"state": "cold"})
    ipx.fake.route("POST", "/v1/orgs/acme/deployments", body={"deployment": DEP, "message": "Model deployed."})
    r = ipx("-y", "featherless", "deploy", "Qwen/Qwen2.5-7B")
    assert ipx.fake.last("/v1/orgs/acme/deployments")["json"] == {"model_id": "hf/Qwen/Qwen2.5-7B"}
    assert "Model deployed." in r.output and "5-15 minutes" in r.output
    ipx("-y", "featherless", "deploy", "hf/Qwen/Qwen2.5-7B")
    assert ipx.fake.last("/v1/orgs/acme/deployments")["json"] == {"model_id": "hf/Qwen/Qwen2.5-7B"}


def test_deploy_requires_confirmation_non_interactively(ipx):
    sign_in(ipx)
    ipx.fake.route("GET", "/v1/deploy/model-status/a/b", body={"state": "warm"})
    with pytest.raises(IpxError):
        ipx("featherless", "deploy", "a/b")
    assert not any(c["method"] == "POST" for c in ipx.fake.calls)


def test_cancel_restore_terminate_org(ipx):
    with_org(ipx)
    ipx.fake.route("POST", "/v1/orgs/acme/deployments/d1/cancel", body={"message": "Deployment cancelled."})
    ipx.fake.route("POST", "/v1/orgs/acme/deployments/d1/uncancel", body={"message": "Deployment restored."})
    ipx.fake.route("DELETE", "/v1/orgs/acme/deployments/d1", body={"message": "Deployment terminated immediately."})
    assert "cancelled" in ipx("-y", "featherless", "cancel", "d1").output
    assert "restored" in ipx("featherless", "restore", "d1").output
    assert "restored" in ipx("featherless", "uncancel", "d1").output
    assert "terminated" in ipx("-y", "featherless", "terminate", "d1").output


def test_usage_and_get(ipx):
    sign_in(ipx)
    ipx.fake.route("GET", "/v1/deploy/d1/usage", body={"featherless_model_id": "a/b", "display_model_id": "hf/a/b", "status": "active",
                                                         "total_input_tokens": 1200, "total_output_tokens": 300, "total_credits_charged": 0.12})
    ipx.fake.route("GET", "/v1/deploy/d1", body=DEP)
    assert "1,200" in ipx("featherless", "usage", "d1").output
    assert "hf/Qwen/Qwen2.5-7B" in ipx("featherless", "get", "d1").output


def test_org_deployments_refuse_api_keys(ipx):
    from ipx.config import Config, Credentials

    Credentials().store_api_key("ipa_live_x")
    Config().set_org("acme")
    with pytest.raises(IpxError) as e:
        ipx("featherless", "list")
    assert "signed-in session" in e.value.message
    assert not ipx.fake.calls
