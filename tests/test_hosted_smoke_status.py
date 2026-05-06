import json
import re

from payjent.config import Settings, get_settings
from payjent.main import app


def _enable_bootstrap(token="smoke-status-token"):
    app.dependency_overrides[get_settings] = lambda: Settings(bootstrap_token=token)
    return token


def _clear_bootstrap():
    app.dependency_overrides.pop(get_settings, None)


def test_hosted_smoke_status_disabled_and_requires_token(client):
    assert client.post("/api/v1/smoke/agent-webhook", json={}).status_code == 404
    token = _enable_bootstrap()
    try:
        assert client.post("/api/v1/smoke/agent-webhook", json={}).status_code == 401
        assert client.post("/api/v1/smoke/agent-webhook", json={}, headers={"X-Payjent-Bootstrap-Token": "wrong"}).status_code == 401
        ok = client.post("/api/v1/smoke/agent-webhook", json={}, headers={"Authorization": f"Bearer {token}"})
        assert ok.status_code == 200, ok.text
    finally:
        _clear_bootstrap()


def test_hosted_smoke_status_production_requires_explicit_test_rail(client):
    token = "production-smoke-token"
    app.dependency_overrides[get_settings] = lambda: Settings(
        env="production",
        dev_mode=False,
        public_base_url="https://payjent.example",
        signing_secret="prod-smoke-signing-secret",
        bootstrap_token=token,
    )
    try:
        denied = client.post("/api/v1/smoke/agent-webhook", json={}, headers={"X-Payjent-Bootstrap-Token": token})
        assert denied.status_code == 503
    finally:
        _clear_bootstrap()

    app.dependency_overrides[get_settings] = lambda: Settings(
        env="production",
        dev_mode=False,
        public_base_url="https://payjent.example",
        signing_secret="prod-smoke-signing-secret",
        bootstrap_token=token,
        hosted_smoke_test_rail_enabled=True,
    )
    try:
        ok = client.post("/api/v1/smoke/agent-webhook", json={}, headers={"X-Payjent-Bootstrap-Token": token})
        assert ok.status_code == 200, ok.text
        data = ok.json()
        assert data["ok"] is True
        assert data["operator_mock_pay"] == "test_rail_only"
        assert data["settlement"] == "external_pay_sh_runtime"
    finally:
        _clear_bootstrap()


def test_hosted_smoke_status_success_and_redaction(client):
    token = _enable_bootstrap()
    try:
        response = client.post(
            "/api/v1/smoke/agent-webhook",
            json={"bot_id": "status-bot", "operator_id": "status-operator"},
            headers={"X-Payjent-Bootstrap-Token": token},
        )
    finally:
        _clear_bootstrap()
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["ok"] is True
    assert data["provider"] == "pay_sh"
    assert data["settlement"] == "external_pay_sh_runtime"
    assert data["operator_mock_pay"] == "test_rail_only"
    assert data["payment_link_exists"] is True
    assert data["callback_contains_payment_token"] is False
    assert data["callback_contains_grant"] is False
    assert data["unpaid_poll"] == {
        "status": "awaiting_payment",
        "payment_status": "checkout_created",
        "payment_token_status": "unissued",
        "token_present": False,
        "token_redacted": False,
    }
    assert data["paid_poll"]["status"] == "ready"
    assert data["paid_poll"]["payment_status"] == "paid"
    assert data["paid_poll"]["payment_token_status"] == "available"
    assert data["paid_poll"]["token_present"] is True
    assert data["paid_poll"]["token_redacted"] is True
    assert data["resumed_status"] == "ready_to_execute"
    assert data["fulfilled_status"] == "fulfilled"

    text = json.dumps(data)
    assert not re.search(r"payjent_[A-Za-z0-9_-]{20,}", text)
    assert not re.search(r"grant_[A-Za-z0-9]{8,}", text)
    assert "payment_token" not in data["paid_poll"]
