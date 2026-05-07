import json

from payjent.config import Settings, get_settings
from sqlmodel import Session, select

from payjent.main import app
from payjent.models import FulfillmentEvent, Grant, SpendLedgerEntry
import payjent.main as main_module


def _stripe_signature(body: bytes, secret: str, timestamp: str = "1700000000") -> str:
    import hashlib, hmac
    digest = hmac.new(secret.encode(), timestamp.encode() + b"." + body, hashlib.sha256).hexdigest()
    return f"t={timestamp},v1={digest}"


def _create_action(client, bot_headers, service_url="https://downstream.example/run", headers=None, body=None, method="POST", flag="payjent_fulfillment_callback"):
    return client.post(
        "/api/v1/premium-actions/pay-sh",
        headers=bot_headers,
        json={
            "bot_id": "bot-1",
            "external_user_id": "user-1",
            "request_summary": "run premium downstream action",
            "request_hash": "downstream-hash",
            "amount_minor": 250,
            "currency": "USD",
            "cost_breakdown": [{"label": "work", "amount_minor": 250}],
            "service_url": service_url,
            "method": method,
            "body": body or {"task": "run"},
            "headers": headers or {"Accept": "application/json", "Authorization": "Bearer leak", "X-Api-Key": "leak", "Cookie": "leak"},
            flag: True,
        },
    ).json()


def _paid_body(payment_session_id, amount=250, currency="usd"):
    return json.dumps({
        "type": "checkout.session.completed",
        "data": {"object": {"id": "cs_downstream", "payment_session_id": payment_session_id, "amount_total": amount, "currency": currency}},
    }, separators=(",", ":")).encode()


def test_stripe_webhook_only_issues_grant_agent_consumes_and_authorizes_x402_once(client, bot_headers, monkeypatch, engine):
    secret = "whsec_test"
    app.dependency_overrides[get_settings] = lambda: Settings(
        checkout_provider="stripe",
        stripe_secret_key="sk_test_fake",
        public_base_url="https://payjent.example",
        stripe_webhook_secret=secret,
        managed_execution_allowed_hosts="downstream.example",
    )
    monkeypatch.setattr(main_module, "create_stripe_checkout_session", lambda *_: ("cs_downstream", "https://checkout.stripe.test/session"))
    calls = []

    class FakeClient:
        def __init__(self, *args, **kwargs): pass
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def post(self, url, json=None, headers=None):
            calls.append({"url": url, "json": json, "headers": headers})
            raise AssertionError("Payjent must not POST service_url for pay.sh/x402 premium actions")

    monkeypatch.setattr(main_module.httpx, "Client", FakeClient)
    action = _create_action(client, bot_headers)
    assert action["payment_session_id"].startswith("ps_")

    body = _paid_body(action["payment_session_id"])
    headers = {"content-type": "application/json", "Stripe-Signature": _stripe_signature(body, secret)}
    paid = client.post("/api/v1/webhooks/stripe", content=body, headers=headers)
    duplicate = client.post("/api/v1/webhooks/stripe", content=body, headers=headers)

    assert paid.status_code == 200
    assert paid.json() == {"received": True, "processed": True}
    assert "grant" not in paid.text.lower()
    assert duplicate.status_code == 200
    assert duplicate.json()["processed"] is False
    assert calls == []

    status = client.get(f"/api/v1/agent-actions/{action['action_id']}", headers=bot_headers).json()
    assert status["status"] == "ready"
    assert status["payment_token_status"] == "available"
    assert status["fulfillment_events"] == []
    payment_token = status["payment_token"]
    presentation = {"bot_id": "bot-1", "external_user_id": "user-1", "request_hash": "downstream-hash"}

    with Session(engine) as db:
        grant = db.exec(select(Grant).where(Grant.id == payment_token)).one()
        assert grant.consumed_at is None
        assert db.exec(select(SpendLedgerEntry).where(SpendLedgerEntry.quote_id == action["action_id"])).all() == []

    resumed = client.post(
        f"/api/v1/agent-actions/{action['action_id']}/consume",
        headers=bot_headers,
        json={"payment_token": payment_token, "presentation": presentation},
    )
    assert resumed.status_code == 200
    envelope = resumed.json()["execution_envelope"]
    assert envelope["provider"] == "pay_sh"
    assert envelope["service_url"] == "https://downstream.example/run"
    assert envelope["payjent_fulfillment_callback"] is False
    assert envelope["payjent_managed_execution"] is False
    assert envelope["payjent_execution_boundary"] == "agent_executes_after_spend_authorization"

    spend_payload = {
        "operation_id": f"pay_sh:{action['action_id']}:downstream-hash",
        "presentation": presentation,
        "tool": "payjent.create_pay_sh_premium_action",
        "vendor": "pay.sh",
        "rail": "x402",
        "amount_minor": 250,
        "currency": "USD",
        "reason": "run premium downstream action",
        "provider_reference": "https://downstream.example/run",
        "metadata": {"provider": "pay_sh", "agent_executes": True},
        "capture": True,
    }
    spend = client.post(f"/api/v1/grants/{payment_token}/spend-authorizations", headers=bot_headers, json=spend_payload)
    duplicate_spend = client.post(f"/api/v1/grants/{payment_token}/spend-authorizations", headers=bot_headers, json=spend_payload)
    over_budget = client.post(
        f"/api/v1/grants/{payment_token}/spend-authorizations",
        headers=bot_headers,
        json={**spend_payload, "operation_id": "second", "amount_minor": 1},
    )

    assert spend.status_code == 200
    assert duplicate_spend.status_code == 200
    assert duplicate_spend.json()["id"] == spend.json()["id"]
    assert spend.json()["status"] == "captured"
    assert spend.json()["rail"] == "x402_payment"
    assert spend.json()["amount_minor"] == 250
    assert spend.json()["remaining_budget"] == 0
    assert over_budget.status_code == 409

    complete = client.post(
        f"/api/v1/agent-actions/{action['action_id']}/complete",
        headers=bot_headers,
        json={"status": "fulfilled", "metadata": {"executed_by": "agent", "spend_id": spend.json()["id"]}},
    )
    assert complete.status_code == 200
    assert complete.json()["status"] == "fulfilled"

    with Session(engine) as db:
        grants = db.exec(select(Grant).where(Grant.quote_id == action["action_id"])).all()
        spends = db.exec(select(SpendLedgerEntry).where(SpendLedgerEntry.quote_id == action["action_id"])).all()
        events = db.exec(select(FulfillmentEvent).where(FulfillmentEvent.quote_id == action["action_id"])).all()
    assert len(grants) == 1
    assert grants[0].consumed_at is not None
    assert len(spends) == 1
    assert spends[0].rail == "x402_payment"
    assert spends[0].vendor == "pay.sh"
    assert spends[0].status == "captured"
    assert spends[0].amount_minor == 250
    assert len(events) == 1
    assert events[0].metadata_json["executed_by"] == "agent"


def test_pay_sh_legacy_execution_flags_do_not_require_allowlist_or_safe_service_url(client, bot_headers, monkeypatch):
    app.dependency_overrides[get_settings] = lambda: Settings(
        env="production",
        dev_mode=False,
        checkout_provider="stripe",
        stripe_secret_key="sk_test_fake",
        public_base_url="https://payjent.example",
        stripe_webhook_secret="whsec_test",
    )
    monkeypatch.setattr(main_module, "create_stripe_checkout_session", lambda *_: ("cs_legacy", "https://checkout.stripe.test/legacy"))

    response = client.post(
        "/api/v1/premium-actions/pay-sh",
        headers=bot_headers,
        json={
            "bot_id": "bot-1", "external_user_id": "user-1", "request_summary": "run", "request_hash": "legacy-no-exec",
            "amount_minor": 250, "currency": "USD", "cost_breakdown": [{"label": "work", "amount_minor": 250}],
            "service_url": "https://downstream.example/run", "method": "POST", "body": {"task": {"token": "not-sent-by-payjent"}}, "payjent_managed_execution": True,
        },
    )

    assert response.status_code == 200
    assert response.json()["payment_url"] == "https://checkout.stripe.test/legacy"
