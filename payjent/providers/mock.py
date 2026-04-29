from datetime import timedelta
from uuid import uuid4
from sqlmodel import Session
from payjent.models import Quote, PaymentSession, Receipt, Grant, now_utc
from payjent.signing import sign_payload


def complete_mock_payment(session: Session, quote: Quote, payment_session: PaymentSession, secret: str, ttl_seconds: int):
    paid_at = now_utc()
    payment_session.status = "paid"
    payment_session.paid_at = paid_at
    quote.status = "paid"

    receipt_id = f"rcpt_{uuid4().hex}"
    receipt_payload = {
        "receipt_id": receipt_id,
        "payment_session_id": payment_session.id,
        "quote_id": quote.id,
        "provider": "mock",
        "amount_minor": quote.amount_minor,
        "currency": quote.currency,
        "paid_at": paid_at.isoformat(),
    }
    receipt = Receipt(
        id=receipt_id,
        quote_id=quote.id,
        payment_session_id=payment_session.id,
        payload=receipt_payload,
        signature=sign_payload(receipt_payload, secret),
    )
    payment_session.receipt_id = receipt_id

    grant_id = f"grant_{uuid4().hex}"
    expires_at = paid_at + timedelta(seconds=ttl_seconds)
    grant_payload = {
        "grant_id": grant_id,
        "quote_id": quote.id,
        "bot_id": quote.bot_id,
        "external_user_id": quote.external_user_id,
        "request_hash": quote.request_hash,
        "amount_minor": quote.amount_minor,
        "currency": quote.currency,
        "expires_at": expires_at.isoformat(),
        "execution_envelope": quote.execution_envelope,
    }
    grant = Grant(
        id=grant_id,
        quote_id=quote.id,
        payload=grant_payload,
        signature=sign_payload(grant_payload, secret),
        expires_at=expires_at,
    )
    session.add(receipt)
    session.add(grant)
    session.add(quote)
    session.add(payment_session)
    session.commit()
    session.refresh(payment_session)
    session.refresh(receipt)
    session.refresh(grant)
    return receipt, grant
