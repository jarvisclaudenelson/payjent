from sqlmodel import Session

from payjent.models import Grant, PaymentSession, Quote, Receipt
from payjent.providers.base import issue_receipt_and_grant


def complete_mock_payment(session: Session, quote: Quote, payment_session: PaymentSession, secret: str, ttl_seconds: int) -> tuple[Receipt, Grant]:
    return issue_receipt_and_grant(session, quote, payment_session, secret, ttl_seconds, provider="mock")
