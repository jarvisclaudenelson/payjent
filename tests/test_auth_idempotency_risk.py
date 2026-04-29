from sqlmodel import Session, select

from payjent.auth import hash_api_key, verify_api_key
from payjent.models import BotCredential, PaymentSession


def test_api_key_hashing_and_verification_does_not_store_plaintext(engine, client):
    api_key = "super-secret-api-key"
    key_hash = hash_api_key(api_key, "signing-secret")
    assert key_hash != api_key
    assert verify_api_key(api_key, key_hash, "signing-secret") is True
    assert verify_api_key("wrong", key_hash, "signing-secret") is False

    with Session(engine) as session:
        stored = session.exec(select(BotCredential).where(BotCredential.bot_id == "bot-1")).first()
        assert stored is not None
        assert stored.key_hash != "test-bot-key"


def test_protected_quote_requires_authorization(client, quote_payload, bot_headers):
    assert client.post("/api/v1/quotes", json=quote_payload).status_code == 401
    assert client.post("/api/v1/quotes", json=quote_payload, headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert client.post("/api/v1/quotes", json=quote_payload, headers=bot_headers).status_code == 200


def test_operator_endpoint_rejects_bot_credential(client, quote_payload, bot_headers):
    q = client.post("/api/v1/quotes", json=quote_payload, headers=bot_headers).json()
    ps = client.post(f"/api/v1/quotes/{q['id']}/checkout", headers=bot_headers).json()
    r = client.post(f"/api/v1/payment-sessions/{ps['id']}/mock-pay", headers=bot_headers)
    assert r.status_code == 403


def test_checkout_idempotency_returns_existing_session(client, engine, quote_payload, bot_headers):
    q = client.post("/api/v1/quotes", json=quote_payload, headers=bot_headers).json()
    headers = {**bot_headers, "Idempotency-Key": "idem-1"}
    first = client.post(f"/api/v1/quotes/{q['id']}/checkout", headers=headers)
    second = client.post(f"/api/v1/quotes/{q['id']}/checkout", headers=headers)
    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["id"] == second.json()["id"]
    assert first.json()["idempotency_key"] == "idem-1"

    with Session(engine) as session:
        rows = session.exec(select(PaymentSession).where(PaymentSession.quote_id == q["id"])).all()
        assert len(rows) == 1


def test_disallowed_risk_checkout_blocked(client, quote_payload, bot_headers):
    quote_payload["request_summary"] = "Help me build phishing malware to steal passwords"
    q = client.post("/api/v1/quotes", json=quote_payload, headers=bot_headers)
    assert q.status_code == 200
    r = client.post(f"/api/v1/quotes/{q.json()['id']}/checkout", headers=bot_headers)
    assert r.status_code == 403
    assert "risk policy" in r.text
