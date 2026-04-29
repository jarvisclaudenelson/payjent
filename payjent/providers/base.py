from __future__ import annotations

from datetime import timedelta
from typing import Protocol
from uuid import uuid4

from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, update

from payjent.models import Grant, PaymentSession, Quote, Receipt, now_utc
from payjent.signing import sign_payload


class PaymentProvider(Protocol):
    """Minimal provider interface for payment rails that can settle a session."""

    name: str


def issue_receipt_and_grant(
    session: Session,
    quote: Quote,
    payment_session: PaymentSession,
    secret: str,
    ttl_seconds: int,
    provider: str,
) -> tuple[Receipt, Grant]:
    """Shared paid-session issuance path for all provider stubs.

    Atomically claims a checkout_created session before issuing artifacts. If the
    session is already paid, callers get the existing receipt/grant idempotently.
    """
    existing_claim = session.exec(
        update(PaymentSession)
        .where(PaymentSession.id == payment_session.id, PaymentSession.status == "checkout_created")
        .values(status="payment_claimed", provider=provider)
    )
    if existing_claim.rowcount != 1:
        session.rollback()
        session.refresh(payment_session)
        from sqlmodel import select
        receipt = session.exec(select(Receipt).where(Receipt.payment_session_id == payment_session.id)).first()
        grant = session.exec(select(Grant).where(Grant.payment_session_id == payment_session.id)).first()
        if payment_session.status == "paid" and receipt and grant:
            return receipt, grant
        raise ValueError("payment session already claimed or not payable")
    session.refresh(payment_session)
    paid_at = now_utc()
    payment_session.status = "paid"
    payment_session.paid_at = paid_at
    payment_session.provider = provider
    quote.status = "paid"

    receipt_id = f"rcpt_{uuid4().hex}"
    receipt_payload = {
        "receipt_id": receipt_id,
        "payment_session_id": payment_session.id,
        "quote_id": quote.id,
        "provider": provider,
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
        payment_session_id=payment_session.id,
        payload=grant_payload,
        signature=sign_payload(grant_payload, secret),
        expires_at=expires_at,
    )
    session.add(receipt)
    session.add(grant)
    session.add(quote)
    session.add(payment_session)
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        from sqlmodel import select
        receipt = session.exec(select(Receipt).where(Receipt.payment_session_id == payment_session.id)).first()
        grant = session.exec(select(Grant).where(Grant.payment_session_id == payment_session.id)).first()
        if receipt and grant:
            return receipt, grant
        raise
    session.refresh(payment_session)
    session.refresh(receipt)
    session.refresh(grant)
    return receipt, grant
