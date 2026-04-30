from contextlib import asynccontextmanager
from datetime import datetime, timezone
from uuid import uuid4
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlmodel import Session, select, update
from .config import Settings, get_settings
from .db import get_session, init_db
from .auth import require_bot_credential, require_operator_credential
from .models import BotCredential, Quote, PaymentSession, Grant, FulfillmentEvent
from .money import validate_breakdown, quote_hash
from .schemas import (
    FulfillmentCreate, FulfillmentRead, GrantPresentation, GrantVerifyResponse,
    LinkCredentialApproval, LinkCredentialRequest, MockPayResponse, PaymentSessionRead,
    QuoteCreate, QuoteRead,
)
from .signing import verify_signature
from .providers.mock import complete_mock_payment
from .providers.base import issue_receipt_and_grant
from .providers.stripe import create_stripe_checkout_session, parse_stripe_event, verify_stripe_signature
from .providers.link import LinkCredentialRequest as LinkProviderCredentialRequest, create_link_spend_request as create_link_provider_spend_request, validate_credential_type
from .risk import assess_checkout_risk

@asynccontextmanager
async def lifespan(_app: FastAPI):
    get_settings().validate_runtime_guardrails()
    init_db()
    yield


app = FastAPI(title="Payjent", lifespan=lifespan)


def _html_escape(value) -> str:
    import html
    return html.escape(str(value), quote=True)


def _format_money(amount_minor: int, currency: str) -> str:
    return f"{amount_minor / 100:.2f} {currency.upper()}"


def _find_session_bundle(session: Session, session_id: str):
    ps = session.get(PaymentSession, session_id)
    if not ps:
        raise HTTPException(404, "payment session not found")
    q = session.get(Quote, ps.quote_id)
    if not q:
        raise HTTPException(404, "quote not found")
    grant = session.exec(select(Grant).where(Grant.quote_id == q.id)).first()
    fulfillment = session.exec(select(FulfillmentEvent).where(FulfillmentEvent.quote_id == q.id)).all()
    return ps, q, grant, fulfillment


def _is_operator(credential: BotCredential) -> bool:
    return credential.role in {"operator", "admin"}


def _enforce_bot_scope(credential: BotCredential, bot_id: str) -> None:
    if not _is_operator(credential) and credential.bot_id != bot_id:
        raise HTTPException(status_code=403, detail="credential not authorized for bot_id")


@app.get("/pay/{payment_session_id}", response_class=HTMLResponse)
def pay_page(payment_session_id: str, session: Session = Depends(get_session), settings: Settings = Depends(get_settings)):
    ps, q, grant, _fulfillment = _find_session_bundle(session, payment_session_id)
    breakdown = "".join(
        f"<li>{_html_escape(item.get('label', 'item'))}: {_html_escape(_format_money(int(item.get('amount_minor', 0)), q.currency))}</li>"
        for item in q.cost_breakdown
    )
    mock_form = ""
    if settings.effective_mock_provider_enabled and ps.status != "paid":
        mock_form = f"""
        <section>
          <h2>Dev mock payment</h2>
          <p>The browser page is intentionally read-only. To complete this local development payment, call the authenticated API with an operator credential:</p>
          <pre><code>curl -X POST http://localhost:8000/api/v1/payment-sessions/{_html_escape(ps.id)}/mock-pay \\
  -H 'X-Payjent-Bot-Key: &lt;operator-key&gt;'</code></pre>
        </section>
        """
    grant_line = f"<p>Grant: <code>{_html_escape(grant.id)}</code></p>" if grant else "<p>Grant: not issued</p>"
    return f"""
    <!doctype html><html><head><title>Payjent checkout</title><style>body{{font-family:system-ui;margin:2rem;max-width:760px}}code{{background:#eee;padding:.1rem .25rem}}</style></head>
    <body>
      <h1>Payjent checkout</h1>
      <p>Payment session: <code>{_html_escape(ps.id)}</code></p>
      <p>Status: <strong>{_html_escape(ps.status)}</strong></p>
      <p>Amount: <strong>{_html_escape(_format_money(q.amount_minor, q.currency))}</strong></p>
      <h2>Request</h2><p>{_html_escape(q.request_summary)}</p>
      <h2>Breakdown</h2><ul>{breakdown}</ul>
      {grant_line}
      <p><a href="/status/{_html_escape(ps.id)}">View status</a></p>
      {mock_form}
    </body></html>
    """


@app.get("/status", response_class=HTMLResponse)
def status_index():
    return """<!doctype html><html><head><title>Payjent status</title></head><body><h1>Payjent status</h1><p>Open <code>/status/{payment_session_id}</code> to view a payment session.</p></body></html>"""


@app.get("/status/{payment_session_id}", response_class=HTMLResponse)
def status_page(payment_session_id: str, session: Session = Depends(get_session)):
    ps, q, grant, fulfillment = _find_session_bundle(session, payment_session_id)
    fulfillment_items = "".join(f"<li>{_html_escape(ev.status)} <code>{_html_escape(ev.id)}</code></li>" for ev in fulfillment) or "<li>none</li>"
    grant_state = "not issued"
    if grant:
        grant_state = f"issued: <code>{_html_escape(grant.id)}</code>; consumed: {_html_escape(grant.consumed_at is not None)}"
    link_instructions = ""
    if ps.provider == "link" and ps.status != "paid":
        link_instructions = f"""
      <section>
        <h2>Link approval required</h2>
        <p>This session uses Link as an experimental one-time credential rail. There is no browser-side completion on this page.</p>
        <p>An operator must evaluate the merchant site, choose the explicit credential type, and call the authenticated API:</p>
        <pre><code>POST /api/v1/payment-sessions/{_html_escape(ps.id)}/link/spend-request</code></pre>
        <p>Payjent will return a Link approval URL and polling hint. Approval does not mark this session paid or issue a grant.</p>
      </section>
        """
    return f"""
    <!doctype html><html><head><title>Payjent status</title><style>body{{font-family:system-ui;margin:2rem;max-width:760px}}code{{background:#eee;padding:.1rem .25rem}}</style></head>
    <body>
      <h1>Payjent status</h1>
      <p>Payment session: <code>{_html_escape(ps.id)}</code></p>
      <p>Payment status: <strong>{_html_escape(ps.status)}</strong></p>
      <p>Quote: <code>{_html_escape(q.id)}</code> ({_html_escape(q.status)})</p>
      <p>Grant: {grant_state}</p>
      {link_instructions}
      <h2>Fulfillment</h2><ul>{fulfillment_items}</ul>
      <p><a href="/pay/{_html_escape(ps.id)}">Back to checkout</a></p>
    </body></html>
    """


def quote_to_read(q: Quote) -> QuoteRead:
    return QuoteRead.model_validate(q, from_attributes=True)


def session_to_read(s: PaymentSession) -> PaymentSessionRead:
    return PaymentSessionRead.model_validate(s, from_attributes=True)


def _enforce_stripe_checkout_guardrails(settings: Settings) -> None:
    if not settings.stripe_secret_key:
        raise HTTPException(status_code=503, detail="PAYJENT_STRIPE_SECRET_KEY is required for Stripe checkout")
    if settings.is_production and not settings.stripe_webhook_secret:
        raise HTTPException(
            status_code=503,
            detail="PAYJENT_STRIPE_WEBHOOK_SECRET is required for Stripe checkout in production",
        )


@app.post("/api/v1/quotes", response_model=QuoteRead)
def create_quote(payload: QuoteCreate, session: Session = Depends(get_session), credential: BotCredential = Depends(require_bot_credential)):
    _enforce_bot_scope(credential, payload.bot_id)
    try:
        validate_breakdown(payload.amount_minor, payload.cost_breakdown)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    request_hash = payload.request_hash or quote_hash({
        "bot_id": payload.bot_id,
        "external_user_id": payload.external_user_id,
        "request_summary": payload.request_summary,
        "execution_envelope": payload.execution_envelope,
    })
    canonical = {
        "bot_id": payload.bot_id,
        "external_user_id": payload.external_user_id,
        "request_summary": payload.request_summary,
        "request_hash": request_hash,
        "amount_minor": payload.amount_minor,
        "currency": payload.currency.upper(),
        "cost_breakdown": [i.model_dump() for i in payload.cost_breakdown],
        "execution_envelope": payload.execution_envelope,
    }
    q = Quote(id=f"quote_{uuid4().hex}", quote_hash=quote_hash(canonical), **canonical)
    session.add(q); session.commit(); session.refresh(q)
    return quote_to_read(q)


@app.get("/api/v1/quotes/{quote_id}", response_model=QuoteRead)
def get_quote(quote_id: str, session: Session = Depends(get_session)):
    q = session.get(Quote, quote_id)
    if not q: raise HTTPException(404, "quote not found")
    return quote_to_read(q)


@app.post("/api/v1/quotes/{quote_id}/checkout", response_model=PaymentSessionRead)
def checkout(
    quote_id: str,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    provider: str | None = Header(default=None, alias="X-Payjent-Provider"),
    session: Session = Depends(get_session),
    settings: Settings = Depends(get_settings),
    credential: BotCredential = Depends(require_bot_credential),
):
    q = session.get(Quote, quote_id)
    if not q: raise HTTPException(404, "quote not found")
    _enforce_bot_scope(credential, q.bot_id)
    risk = assess_checkout_risk(q.request_summary, q.execution_envelope)
    if not risk.allowed:
        raise HTTPException(status_code=403, detail=f"checkout blocked by risk policy: {risk.reason}")
    if idempotency_key:
        existing = session.exec(
            select(PaymentSession).where(
                PaymentSession.quote_id == q.id,
                PaymentSession.idempotency_key == idempotency_key,
            )
        ).first()
        if existing:
            return session_to_read(existing)
    requested_provider = (provider or settings.checkout_provider or "mock").lower()
    if requested_provider not in {"mock", "local", "stripe", "link"}:
        raise HTTPException(status_code=422, detail="unsupported checkout provider")
    session_id = f"ps_{uuid4().hex}"
    ps = PaymentSession(
        id=session_id,
        quote_id=q.id,
        provider="mock" if requested_provider in {"mock", "local"} else requested_provider,
        checkout_url=f"/pay/{session_id}",
        idempotency_key=idempotency_key,
    )
    if requested_provider == "stripe":
        _enforce_stripe_checkout_guardrails(settings)
        provider_session_id, hosted_url = create_stripe_checkout_session(q, ps, settings)
        ps.provider_session_id = provider_session_id
        ps.checkout_url = hosted_url
    session.add(ps); session.commit(); session.refresh(ps)
    return session_to_read(ps)


@app.get("/api/v1/payment-sessions/{session_id}", response_model=PaymentSessionRead)
def get_payment_session(session_id: str, session: Session = Depends(get_session)):
    ps = session.get(PaymentSession, session_id)
    if not ps: raise HTTPException(404, "payment session not found")
    return session_to_read(ps)


def _issued_response(ps: PaymentSession, receipt, grant):
    return {"payment_session": session_to_read(ps), "receipt": {"payload": receipt.payload, "signature": receipt.signature}, "grant": {"id": grant.id, "payload": grant.payload, "signature": grant.signature}}


def _issue_paid_session(session: Session, ps: PaymentSession, settings: Settings, provider: str):
    q = session.get(Quote, ps.quote_id)
    if not q: raise HTTPException(404, "quote not found")
    try:
        return issue_receipt_and_grant(session, q, ps, settings.signing_secret, settings.grant_ttl_seconds, provider=provider)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@app.post("/api/v1/payment-sessions/{session_id}/mock-pay", response_model=MockPayResponse)
def mock_pay(session_id: str, session: Session = Depends(get_session), settings: Settings = Depends(get_settings), _credential: BotCredential = Depends(require_operator_credential)):
    if not settings.effective_mock_provider_enabled:
        raise HTTPException(403, "mock provider disabled")
    ps = session.get(PaymentSession, session_id)
    if not ps: raise HTTPException(404, "payment session not found")
    q = session.get(Quote, ps.quote_id)
    if not q: raise HTTPException(404, "quote not found")
    if ps.status == "paid": raise HTTPException(409, "payment session already paid")
    receipt, grant = complete_mock_payment(session, q, ps, settings.signing_secret, settings.grant_ttl_seconds)
    return _issued_response(ps, receipt, grant)


@app.post("/api/v1/payment-sessions/{session_id}/link/spend-request", response_model=LinkCredentialApproval)
def create_link_spend_request(session_id: str, payload: LinkCredentialRequest, session: Session = Depends(get_session), _credential: BotCredential = Depends(require_operator_credential)):
    ps = session.get(PaymentSession, session_id)
    if not ps:
        raise HTTPException(404, "payment session not found")
    if ps.provider != "link":
        raise HTTPException(409, "payment session provider is not link")
    if ps.status == "paid":
        raise HTTPException(409, "payment session already paid")
    q = session.get(Quote, ps.quote_id)
    if not q:
        raise HTTPException(404, "quote not found")
    reserved_metadata_keys = {"payjent_quote_id", "payjent_payment_session_id"}
    if reserved_metadata_keys.intersection(payload.metadata):
        raise HTTPException(status_code=422, detail="metadata may not include reserved Payjent keys")
    try:
        provider_payload = LinkProviderCredentialRequest(
            merchant_url=payload.merchant_url,
            credential_type=validate_credential_type(payload.credential_type),
            amount_minor=q.amount_minor,
            currency=q.currency,
            purpose=payload.purpose or q.request_summary,
            external_user_id=q.external_user_id,
            metadata={**payload.metadata, "payjent_quote_id": q.id, "payjent_payment_session_id": ps.id},
        )
        approval = create_link_provider_spend_request(provider_payload)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    ps.provider_session_id = approval.provider_session_id
    ps.checkout_url = approval.approval_url
    session.add(ps); session.commit(); session.refresh(ps)
    return LinkCredentialApproval(
        payment_session=session_to_read(ps),
        approval_url=approval.approval_url,
        provider_session_id=approval.provider_session_id,
        polling_command=approval.polling_command,
        message="Show approval_url to the user and poll Link; Payjent remains unpaid until verified settlement is implemented.",
    )


@app.post("/api/v1/payment-sessions/{session_id}/crypto/mark-paid", response_model=MockPayResponse)
def crypto_mark_paid(session_id: str, session: Session = Depends(get_session), settings: Settings = Depends(get_settings), _credential: BotCredential = Depends(require_operator_credential)):
    if settings.is_production or not settings.dev_mode:
        raise HTTPException(403, "crypto mark-paid is dev/admin only")
    ps = session.get(PaymentSession, session_id)
    if not ps: raise HTTPException(404, "payment session not found")
    if ps.status == "paid": raise HTTPException(409, "payment session already paid")
    receipt, grant = _issue_paid_session(session, ps, settings, provider="crypto-manual")
    return _issued_response(ps, receipt, grant)


@app.post("/api/v1/webhooks/stripe")
async def stripe_webhook(request: Request, stripe_signature: str | None = Header(default=None, alias="Stripe-Signature"), session: Session = Depends(get_session), settings: Settings = Depends(get_settings)):
    raw_body = await request.body()
    verify_stripe_signature(raw_body, stripe_signature, settings.stripe_webhook_secret)
    event = parse_stripe_event(raw_body)
    event_type = event.get("type")
    data_object = event.get("data", {}).get("object", {}) if isinstance(event.get("data"), dict) else {}
    metadata = data_object.get("metadata", {}) if isinstance(data_object, dict) else {}
    session_id = data_object.get("payment_session_id") or metadata.get("payment_session_id")
    provider_session_id = data_object.get("id")

    if event_type not in {"checkout.session.completed", "payment_intent.succeeded"}:
        return {"received": True, "processed": False, "reason": "event ignored"}
    if event_type == "checkout.session.completed" and data_object.get("payment_status") not in {None, "paid"}:
        return {"received": True, "processed": False, "reason": "checkout session not paid"}
    ps = session.get(PaymentSession, session_id) if session_id else None
    if not ps and provider_session_id:
        ps = session.exec(select(PaymentSession).where(PaymentSession.provider_session_id == provider_session_id)).first()
    if not ps:
        raise HTTPException(404 if (session_id or provider_session_id) else 400, "payment session not found" if (session_id or provider_session_id) else "missing payment_session_id")
    if ps.status == "paid":
        return {"received": True, "processed": False, "reason": "payment session already paid", "payment_session": session_to_read(ps)}
    receipt, grant = _issue_paid_session(session, ps, settings, provider="stripe")
    return {"received": True, "processed": True, **_issued_response(ps, receipt, grant)}


def _load_valid_grant(grant_id: str, presentation: GrantPresentation, session: Session, settings: Settings) -> Grant:
    grant = session.get(Grant, grant_id)
    if not grant: raise HTTPException(404, "grant not found")
    if not verify_signature(grant.payload, grant.signature, settings.signing_secret):
        raise HTTPException(400, "invalid grant signature")
    exp = grant.expires_at
    if exp.tzinfo is None: exp = exp.replace(tzinfo=timezone.utc)
    if exp <= datetime.now(timezone.utc): raise HTTPException(400, "grant expired")
    for field in ("bot_id", "external_user_id", "request_hash"):
        presented = getattr(presentation, field)
        if presented is not None and presented != grant.payload.get(field):
            raise HTTPException(403, f"{field} mismatch")
    return grant


@app.post("/api/v1/grants/{grant_id}/verify", response_model=GrantVerifyResponse)
def verify_grant(grant_id: str, presentation: GrantPresentation, session: Session = Depends(get_session), settings: Settings = Depends(get_settings), credential: BotCredential = Depends(require_bot_credential)):
    grant = _load_valid_grant(grant_id, presentation, session, settings)
    _enforce_bot_scope(credential, grant.payload.get("bot_id"))
    return GrantVerifyResponse(valid=True, grant_id=grant.id, consumed=grant.consumed_at is not None, payload=grant.payload)


def _mark_grant_consumed(grant_id: str, session: Session) -> bool:
    result = session.exec(
        update(Grant)
        .where(Grant.id == grant_id, Grant.consumed_at.is_(None))
        .values(consumed_at=datetime.now(timezone.utc))
    )
    if result.rowcount != 1:
        session.rollback()
        return False
    session.commit()
    return True


@app.post("/api/v1/grants/{grant_id}/consume", response_model=GrantVerifyResponse)
def consume_grant(grant_id: str, presentation: GrantPresentation, session: Session = Depends(get_session), settings: Settings = Depends(get_settings), credential: BotCredential = Depends(require_bot_credential)):
    grant = _load_valid_grant(grant_id, presentation, session, settings)
    _enforce_bot_scope(credential, grant.payload.get("bot_id"))
    if not _mark_grant_consumed(grant.id, session):
        raise HTTPException(409, "grant already consumed")
    return GrantVerifyResponse(valid=True, grant_id=grant.id, consumed=True, payload=grant.payload)


@app.post("/api/v1/quotes/{quote_id}/fulfillment", response_model=FulfillmentRead)
def record_fulfillment(quote_id: str, payload: FulfillmentCreate, session: Session = Depends(get_session), credential: BotCredential = Depends(require_bot_credential)):
    q = session.get(Quote, quote_id)
    if not q: raise HTTPException(404, "quote not found")
    _enforce_bot_scope(credential, q.bot_id)
    if payload.status in {"executing", "fulfilled", "failed", "refunded"}:
        paid_session = session.exec(select(PaymentSession).where(PaymentSession.quote_id == q.id, PaymentSession.status == "paid")).first()
        if not paid_session:
            raise HTTPException(status_code=409, detail="quote must be paid before fulfillment")
        consumed_grant = session.exec(select(Grant).where(Grant.quote_id == q.id, Grant.consumed_at.is_not(None))).first()
        if not consumed_grant:
            raise HTTPException(status_code=409, detail="a consumed grant is required before fulfillment")
    ev = FulfillmentEvent(id=f"ful_{uuid4().hex}", quote_id=quote_id, status=payload.status, metadata_json=payload.metadata)
    q.status = payload.status
    session.add(q); session.add(ev); session.commit(); session.refresh(ev)
    return FulfillmentRead(id=ev.id, quote_id=ev.quote_id, status=ev.status, metadata=ev.metadata_json)
