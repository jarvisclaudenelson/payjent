from sqlmodel import Session, select

from payjent.auth import create_bot_credential
from payjent.config import get_settings
from payjent.models import Grant, Receipt


def _checkout(client, quote_payload, bot_headers):
    q = client.post("/api/v1/quotes", json=quote_payload, headers=bot_headers).json()
    ps = client.post(f"/api/v1/quotes/{q['id']}/checkout", headers=bot_headers).json()
    return q, ps


def test_bot_credentials_are_scoped_to_matching_bot_id(client, quote_payload, bot_headers, operator_headers, engine):
    settings = get_settings()
    with Session(engine) as session:
        create_bot_credential(session, "bot-2", "test-bot-2-key", settings.signing_secret)
    bot2_headers = {"Authorization": "Bearer test-bot-2-key"}

    cross_payload = {**quote_payload, "bot_id": "bot-2"}
    denied_create = client.post("/api/v1/quotes", json=cross_payload, headers=bot_headers)
    assert denied_create.status_code == 403

    quote = client.post("/api/v1/quotes", json=cross_payload, headers=bot2_headers).json()
    denied_checkout = client.post(f"/api/v1/quotes/{quote['id']}/checkout", headers=bot_headers)
    assert denied_checkout.status_code == 403

    checkout = client.post(f"/api/v1/quotes/{quote['id']}/checkout", headers=bot2_headers).json()
    paid = client.post(f"/api/v1/payment-sessions/{checkout['id']}/mock-pay", headers=operator_headers).json()
    grant_id = paid["grant"]["id"]

    denied_verify = client.post(f"/api/v1/grants/{grant_id}/verify", headers=bot_headers, json={"bot_id": "bot-2"})
    assert denied_verify.status_code == 403
    denied_consume = client.post(f"/api/v1/grants/{grant_id}/consume", headers=bot_headers, json={"bot_id": "bot-2"})
    assert denied_consume.status_code == 403
    denied_fulfill = client.post(f"/api/v1/quotes/{quote['id']}/fulfillment", headers=bot_headers, json={"status": "fulfilled", "metadata": {}})
    assert denied_fulfill.status_code == 403

    operator_quote = client.post("/api/v1/quotes", json={**quote_payload, "bot_id": "bot-operator"}, headers=operator_headers)
    assert operator_quote.status_code == 200


def test_fulfillment_requires_paid_quote_and_consumed_grant(client, quote_payload, bot_headers, operator_headers):
    quote, ps = _checkout(client, quote_payload, bot_headers)

    unpaid = client.post(f"/api/v1/quotes/{quote['id']}/fulfillment", headers=bot_headers, json={"status": "fulfilled", "metadata": {}})
    assert unpaid.status_code == 409
    assert "paid" in unpaid.json()["detail"]

    paid = client.post(f"/api/v1/payment-sessions/{ps['id']}/mock-pay", headers=operator_headers).json()
    unconsumed = client.post(f"/api/v1/quotes/{quote['id']}/fulfillment", headers=bot_headers, json={"status": "fulfilled", "metadata": {}})
    assert unconsumed.status_code == 409
    assert "consumed grant" in unconsumed.json()["detail"]

    consumed = client.post(f"/api/v1/grants/{paid['grant']['id']}/consume", headers=bot_headers, json={"bot_id": quote_payload["bot_id"]})
    assert consumed.status_code == 200
    fulfilled = client.post(f"/api/v1/quotes/{quote['id']}/fulfillment", headers=bot_headers, json={"status": "fulfilled", "metadata": {"message_id": "m1"}})
    assert fulfilled.status_code == 200
    assert fulfilled.json()["status"] == "fulfilled"


def test_paid_issuance_duplicate_does_not_create_extra_artifacts(client, quote_payload, operator_headers, bot_headers, engine):
    _quote, ps = _checkout(client, quote_payload, bot_headers)
    first = client.post(f"/api/v1/payment-sessions/{ps['id']}/mock-pay", headers=operator_headers)
    assert first.status_code == 200
    second = client.post(f"/api/v1/payment-sessions/{ps['id']}/mock-pay", headers=operator_headers)
    assert second.status_code == 409

    with Session(engine) as session:
        receipts = session.exec(select(Receipt).where(Receipt.payment_session_id == ps["id"])).all()
        grants = session.exec(select(Grant).where(Grant.payment_session_id == ps["id"])).all()
    assert len(receipts) == 1
    assert len(grants) == 1
