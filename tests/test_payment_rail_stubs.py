import hashlib
import hmac
import json

from payjent.config import Settings, get_settings
from payjent.main import app


def _checkout(client, quote_payload, bot_headers):
    q = client.post("/api/v1/quotes", json=quote_payload, headers=bot_headers).json()
    ps = client.post(f"/api/v1/quotes/{q['id']}/checkout", headers=bot_headers).json()
    return q, ps


def _stripe_signature(body: bytes, secret: str, timestamp: str = "1700000000") -> str:
    digest = hmac.new(secret.encode("utf-8"), timestamp.encode("utf-8") + b"." + body, hashlib.sha256).hexdigest()
    return f"t={timestamp},v1={digest}"


def test_stripe_webhook_rejects_missing_or_invalid_signature_when_secret_configured(client, quote_payload, bot_headers):
    secret = "whsec_test"
    app.dependency_overrides[get_settings] = lambda: Settings(stripe_webhook_secret=secret)
    _, ps = _checkout(client, quote_payload, bot_headers)
    body = json.dumps({"type": "checkout.session.completed", "data": {"object": {"metadata": {"payment_session_id": ps["id"]}}}}).encode()

    missing = client.post("/api/v1/webhooks/stripe", content=body, headers={"content-type": "application/json"})
    assert missing.status_code == 400
    assert missing.json()["detail"] == "missing Stripe signature"

    invalid = client.post("/api/v1/webhooks/stripe", content=body, headers={"content-type": "application/json", "Stripe-Signature": "t=1700000000,v1=bad"})
    assert invalid.status_code == 400
    assert invalid.json()["detail"] == "invalid Stripe signature"


def test_stripe_webhook_marks_paid_and_duplicate_is_idempotent(client, quote_payload, bot_headers):
    secret = "whsec_test"
    app.dependency_overrides[get_settings] = lambda: Settings(stripe_webhook_secret=secret)
    _, ps = _checkout(client, quote_payload, bot_headers)
    body = json.dumps({"type": "checkout.session.completed", "data": {"object": {"payment_session_id": ps["id"]}}}, separators=(",", ":")).encode()
    headers = {"content-type": "application/json", "Stripe-Signature": _stripe_signature(body, secret)}

    paid = client.post("/api/v1/webhooks/stripe", content=body, headers=headers)
    assert paid.status_code == 200
    payload = paid.json()
    assert payload["processed"] is True
    assert payload["payment_session"]["status"] == "paid"
    assert payload["payment_session"]["provider"] == "stripe"
    assert payload["receipt"]["payload"]["provider"] == "stripe"
    assert payload["grant"]["payload"]["quote_id"] == ps["quote_id"]

    duplicate = client.post("/api/v1/webhooks/stripe", content=body, headers=headers)
    assert duplicate.status_code == 200
    assert duplicate.json()["processed"] is False
    assert duplicate.json()["reason"] == "payment session already paid"


def test_stripe_webhook_rejects_unconfigured_secret_without_marking_paid(client, quote_payload, bot_headers):
    app.dependency_overrides[get_settings] = lambda: Settings(stripe_webhook_secret=None)
    _, ps = _checkout(client, quote_payload, bot_headers)
    body = {"type": "payment_intent.succeeded", "data": {"object": {"metadata": {"payment_session_id": ps["id"]}}}}
    response = client.post("/api/v1/webhooks/stripe", json=body)
    assert response.status_code == 503
    assert response.json()["detail"] == "Stripe webhook secret not configured"

    unchanged = client.get(f"/api/v1/payment-sessions/{ps['id']}")
    assert unchanged.json()["status"] == "checkout_created"


def test_crypto_mark_paid_requires_operator_and_uses_shared_issuance(client, quote_payload, bot_headers, operator_headers):
    _, ps = _checkout(client, quote_payload, bot_headers)

    denied = client.post(f"/api/v1/payment-sessions/{ps['id']}/crypto/mark-paid", headers=bot_headers)
    assert denied.status_code == 403

    paid = client.post(f"/api/v1/payment-sessions/{ps['id']}/crypto/mark-paid", headers=operator_headers)
    assert paid.status_code == 200
    payload = paid.json()
    assert payload["payment_session"]["status"] == "paid"
    assert payload["payment_session"]["provider"] == "crypto-manual"
    assert payload["receipt"]["payload"]["provider"] == "crypto-manual"
    assert payload["grant"]["payload"]["quote_id"] == ps["quote_id"]

    duplicate = client.post(f"/api/v1/payment-sessions/{ps['id']}/crypto/mark-paid", headers=operator_headers)
    assert duplicate.status_code == 409
