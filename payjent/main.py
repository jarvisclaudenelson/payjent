from contextlib import asynccontextmanager
from datetime import datetime, timezone
import hmac
from urllib.parse import parse_qs, urlparse
from uuid import uuid4
from pathlib import Path

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select, update

from . import workos_auth
from .agent_actions import action_result_response, create_paid_action_response, execution_envelope_for_action
from .auth import (
    DASHBOARD_SESSION_COOKIE,
    create_bot_credential,
    create_dashboard_session_cookie,
    generate_api_key,
    get_account_from_cookie,
    hash_password,
    normalize_email,
    require_bot_credential,
    require_operator_credential,
    verify_password,
)
from .config import Settings, get_settings
from .db import (
    WORKOS_UNUSABLE_PASSWORD_HASH,
    account_password_hash_nullable,
    get_session,
    init_db,
)
from .models import (
    Account,
    AgentProfile,
    BotCredential,
    FulfillmentEvent,
    Grant,
    PaymentSession,
    Quote,
    RailConnection,
    SpendLedgerEntry,
    WebhookDeliveryAttempt,
)
from .money import quote_hash, validate_breakdown
from .providers.base import issue_receipt_and_grant
from .providers.link import LinkCredentialRequest as LinkProviderCredentialRequest
from .providers.link import (
    create_link_spend_request as create_link_provider_spend_request,
)
from .providers.link import retrieve_link_status as retrieve_link_provider_status
from .providers.link import validate_credential_type
from .providers.mock import complete_mock_payment
from .providers.paysh import build_execution_envelope as build_paysh_execution_envelope
from .providers.stripe import (
    create_stripe_checkout_session,
    parse_stripe_event,
    verify_stripe_signature,
)
from .rails import normalize_spend_rail
from .risk import assess_checkout_risk
from .schemas import (
    AgentRead,
    AgentRegisterRequest,
    AgentRegisterResponse,
    AgentActionCompleteResponse,
    AgentActionConsumeRequest,
    AgentActionCreate,
    AgentActionCreateResponse,
    AgentActionExecutionEnvelope,
    AgentActionStatusResponse,
    FulfillmentCreate,
    FulfillmentRead,
    GrantPresentation,
    GrantVerifyResponse,
    HostedSmokeBootstrapRequest,
    HostedSmokeBootstrapResponse,
    HostedSmokeStatusRequest,
    HostedSmokeStatusResponse,
    LinkCredentialApproval,
    LinkCredentialRequest,
    LinkPollResponse,
    MockPayResponse,
    PaymentSessionRead,
    PayShPremiumActionCreate,
    PayShPremiumActionCreateResponse,
    QuoteCreate,
    QuoteRead,
    RailConnectionRead,
    SpendAuthorizationCreate,
    SpendAuthorizationRead,
    SpendCaptureRequest,
    StripeConnectStartResponse,
    X402ConfigureRequest,
)
from .signing import PAYJENT_SIGNATURE_HEADER, PAYJENT_TIMESTAMP_HEADER, sign_webhook_payload, verify_signature


@asynccontextmanager
async def lifespan(_app: FastAPI):
    get_settings().validate_runtime_guardrails()
    if get_session not in _app.dependency_overrides:
        init_db()
    yield


app = FastAPI(title="Payjent", lifespan=lifespan)

DOCS_DIR = Path(__file__).resolve().parents[1] / "docs"


@app.get("/docs/agent-payjent-self-setup.md", response_class=FileResponse)
def agent_payjent_self_setup_doc():
    path = DOCS_DIR / "agent-payjent-self-setup.md"
    if not path.exists():
        raise HTTPException(404, "document not found")
    return FileResponse(path, media_type="text/markdown; charset=utf-8", filename="agent-payjent-self-setup.md")


@app.get("/docs/c3po-payjent-self-setup.md", response_class=FileResponse)
def c3po_payjent_self_setup_doc_redirect():
    return RedirectResponse("/docs/agent-payjent-self-setup.md", status_code=308)


@app.get("/", response_class=HTMLResponse)
def landing_page(settings: Settings = Depends(get_settings)):
    return HTMLResponse(_landing_page_html(settings))


def _html_escape(value) -> str:
    import html
    return html.escape(str(value), quote=True)


def _format_money(amount_minor: int, currency: str) -> str:
    return f"{amount_minor / 100:.2f} {currency.upper()}"


def _primary_cta(settings: Settings) -> str:
    return "/auth/workos/login" if workos_auth.workos_configured(settings) else "/auth/register"


def _landing_page_html(settings: Settings) -> str:
    primary = _primary_cta(settings)
    return f"""<!doctype html><html lang='en'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><title>Payjent — kinetic escrow for agent actions</title><meta name='description' content='Protocol-noir payment gates for autonomous agent actions. Store the request, collect payment, issue a signed grant, resume exactly once.'><link rel='preconnect' href='https://fonts.googleapis.com'><link rel='preconnect' href='https://fonts.gstatic.com' crossorigin><link href='https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@500;700&family=Spline+Sans+Mono:wght@400;600;700&display=swap' rel='stylesheet'><style>
:root{{--ink:#07100f;--void:#020504;--noir:#07110f;--panel:#0d1b17d9;--ivory:#f4efd8;--muted:#aab9aa;--cyan:#3ff4ff;--lime:#d6ff5f;--seal:#ffef9a;--rust:#d46f3d;--line:rgba(244,239,216,.18)}}*{{box-sizing:border-box}}html{{scroll-behavior:smooth}}body{{margin:0;color:var(--ivory);font-family:'Space Grotesk',ui-sans-serif,sans-serif;background:radial-gradient(circle at 76% 8%,rgba(63,244,255,.18),transparent 28%),radial-gradient(circle at 7% 18%,rgba(214,255,95,.12),transparent 26%),linear-gradient(180deg,#020504,#07110f 52%,#020504);overflow-x:hidden}}body:before{{content:"";position:fixed;inset:0;pointer-events:none;opacity:.32;background:repeating-linear-gradient(90deg,rgba(244,239,216,.05) 0 1px,transparent 1px 72px),repeating-linear-gradient(0deg,rgba(244,239,216,.035) 0 1px,transparent 1px 46px);mask-image:linear-gradient(to bottom,#000,transparent 85%)}}body:after{{content:"";position:fixed;inset:-50%;pointer-events:none;background:conic-gradient(from 90deg,transparent,rgba(63,244,255,.08),transparent,rgba(214,255,95,.07),transparent);animation:orbit 24s linear infinite}}@keyframes orbit{{to{{transform:rotate(1turn)}}}}@keyframes ticker{{to{{transform:translateX(-50%)}}}}@keyframes pulse{{50%{{box-shadow:0 0 55px rgba(63,244,255,.36),0 0 90px rgba(214,255,95,.14)}}}}@media(prefers-reduced-motion:reduce){{*,*:before,*:after{{animation:none!important;transition:none!important}}}}a{{color:inherit}}.nav{{position:sticky;top:0;z-index:5;background:rgba(2,5,4,.68);backdrop-filter:blur(20px);border-bottom:1px solid var(--line)}}.navin{{max-width:1200px;margin:auto;padding:14px 22px;display:flex;align-items:center;justify-content:space-between;gap:16px}}.brand{{font-family:'Spline Sans Mono',monospace;text-decoration:none;font-weight:700;letter-spacing:.08em;text-transform:uppercase}}.brand:before{{content:'◉';color:var(--lime);margin-right:10px;text-shadow:0 0 22px var(--lime)}}.navlinks{{display:flex;gap:18px;align-items:center;color:var(--muted);font:600 13px 'Spline Sans Mono',monospace;text-transform:uppercase}}.btn{{display:inline-flex;align-items:center;gap:10px;border:1px solid var(--line);border-radius:999px;padding:12px 16px;text-decoration:none;font-weight:700;background:rgba(244,239,216,.07);box-shadow:0 18px 45px #0008}}.btn.primary{{background:linear-gradient(135deg,var(--lime),var(--cyan));color:#03100e;border:0}}.hero{{max-width:1200px;margin:auto;min-height:82vh;padding:82px 22px 56px;display:grid;grid-template-columns:minmax(0,.92fr) minmax(420px,1.08fr);gap:48px;align-items:center}}.eyebrow,.mono{{font-family:'Spline Sans Mono',monospace;text-transform:uppercase;letter-spacing:.15em;color:var(--lime);font-weight:700;font-size:12px}}h1{{font-size:clamp(50px,8vw,104px);line-height:.86;letter-spacing:-.075em;margin:16px 0 22px}}h1 em{{font-style:normal;color:transparent;-webkit-text-stroke:1px var(--ivory);text-shadow:0 0 42px rgba(63,244,255,.28)}}p{{font-size:18px;line-height:1.65;color:#d7ddcc}}.cta,.metrics{{display:flex;gap:12px;flex-wrap:wrap;margin-top:24px}}.metric{{min-width:142px;border:1px solid var(--line);border-radius:20px;padding:14px;background:rgba(13,27,23,.72)}}.metric b{{display:block;font-size:25px;color:var(--cyan)}}.escrow{{position:relative;min-height:640px;border:1px solid var(--line);border-radius:42px;background:linear-gradient(180deg,rgba(13,27,23,.72),rgba(2,5,4,.7));box-shadow:0 40px 140px #000b,inset 0 1px rgba(255,255,255,.12);overflow:hidden}}.escrow:before{{content:"";position:absolute;inset:54px;border-radius:50%;border:1px dashed rgba(63,244,255,.45);box-shadow:inset 0 0 80px rgba(63,244,255,.08);animation:orbit 18s linear infinite}}.escrow:after{{content:'SIGNED ESCROW RING';position:absolute;inset:auto 30px 22px;font:700 12px 'Spline Sans Mono';letter-spacing:.22em;color:rgba(244,239,216,.36);text-align:center}}.seal{{position:absolute;left:50%;top:50%;transform:translate(-50%,-50%);width:205px;height:205px;border-radius:50%;display:grid;place-items:center;text-align:center;color:#05110f;background:radial-gradient(circle,var(--ivory),var(--seal) 52%,var(--lime));font:700 19px 'Spline Sans Mono';letter-spacing:.12em;box-shadow:0 0 42px rgba(214,255,95,.25);animation:pulse 3.8s ease-in-out infinite}}.tape{{position:absolute;left:-8%;right:-8%;padding:12px 0;border-block:1px solid var(--line);background:#06100d;color:var(--cyan);font:700 12px 'Spline Sans Mono';white-space:nowrap;overflow:hidden}}.tape span{{display:inline-block;min-width:200%;animation:ticker 18s linear infinite}}.t1{{top:78px;transform:rotate(-7deg)}}.t2{{bottom:110px;transform:rotate(5deg)}}.packet{{position:absolute;width:260px;border:1px solid var(--line);border-radius:24px;padding:18px;background:rgba(7,16,15,.86);box-shadow:0 20px 70px #0009}}.packet small{{font:700 11px 'Spline Sans Mono';color:var(--lime);letter-spacing:.12em}}.packet h3{{margin:8px 0 4px;font-size:24px}}.packet p{{font-size:14px;margin:0;color:var(--muted)}}.p1{{left:28px;top:150px}}.p2{{right:28px;top:242px}}.p3{{left:54px;bottom:112px}}.sections{{max-width:1200px;margin:auto;padding:28px 22px 90px}}.panelgrid{{display:grid;grid-template-columns:repeat(3,1fr);gap:18px}}.panel{{border:1px solid var(--line);border-radius:30px;padding:24px;background:rgba(13,27,23,.72);box-shadow:0 28px 80px #0007}}.panel h2{{font-size:30px;margin:4px 0 12px}}.wide{{grid-column:span 2}}.receipt{{font-family:'Spline Sans Mono';background:#f4efd8;color:#07100f;border-radius:24px;padding:20px;transform:rotate(-1.5deg);box-shadow:0 20px 80px #0008}}.receipt div{{display:flex;justify-content:space-between;border-bottom:1px dashed #07100f55;padding:9px 0}}@media(max-width:900px){{.hero{{grid-template-columns:1fr;padding-top:52px}}.escrow{{min-height:560px}}.panelgrid{{grid-template-columns:1fr}}.wide{{grid-column:auto}}.navlinks a:not(.btn){{display:none}}}}
</style></head><body><nav class='nav'><div class='navin'><a class='brand' href='/'>Payjent</a><div class='navlinks'><a href='#loop'>Loop</a><a href='#trust'>Trust</a><a href='/dashboard'>Dashboard</a><a class='btn primary' href='{primary}'>Register your agent</a></div></div></nav><main><section class='hero'><div><div class='eyebrow'>Protocol noir / kinetic escrow terminal</div><h1>The paid-action <em>airlock</em> for agents.</h1><p><b>Payment-gate agent actions</b> with protocol-noir clarity: Payjent freezes an agent's exact request at the edge of spend, collects human-approved payment, mints a signed grant, then lets the agent resume that stored request—once, with a clean reasoning and receipt trail.</p><div class='cta'><a class='btn primary' href='{primary}'>Enter the command room →</a><a class='btn' href='/docs/agent-payjent-self-setup.md'>Send the agent setup guide</a></div><div class='metrics'><div class='metric'><b>01</b>Store request</div><div class='metric'><b>02</b>Gate payment</div><div class='metric'><b>03</b>Resume exactly</div></div></div><div class='escrow' aria-label='Animated escrow ring showing request, payment gate, and signed grant'><div class='tape t1'><span>REQUEST HASH • AWAITING PAYMENT • CHECKPOINT LOCKED • HUMAN CONSENT • </span></div><div class='packet p1'><small>REQUEST PACKET</small><h3>Premium action</h3><p>amount, user, agent, envelope, idempotency.</p></div><div class='packet p2'><small>PAYJENT GATE</small><h3>Payment clears</h3><p>Payjent gates payment and stores the execution envelope.</p></div><div class='packet p3'><small>SIGNED GRANT</small><h3>Resume once</h3><p>Receipt trail proves what was unlocked. Payjent does not execute downstream pay.sh.</p></div><div class='seal'>PAYJENT<br>ESCROW<br>SEAL</div><div class='tape t2'><span>GRANT ISSUED • TOKEN REDACTED • SPEND REASONING • FULFILLMENT TRAIL • </span></div></div></section><section class='sections' id='loop'><div class='panelgrid'><div class='panel wide'><div class='mono'>Operator flow</div><h2>Sign in → register → copy once → store as X-Payjent-Bot-Key.</h2><p>WorkOS brings operators into the dashboard. The browser form registers an agent, shows a one-time credential, and points you to the setup guide your agent needs to create paid actions safely.</p></div><div class='panel'><div class='mono'>Unforgettable idea</div><h2>An escrow airlock.</h2><p>Every paid action passes through a visible lock: request in, human payment, signed grant out. Nothing generic, nothing magical.</p></div><div class='panel' id='trust'><div class='mono'>Trust surface</div><h2>No token theater.</h2><p>Dashboard pages expose operational state, not secrets. Payment tokens and operator keys stay redacted; only the generated bot credential appears once after creation.</p></div><div class='panel receipt'><div>agent_action.create <b>$1.50</b></div><div>status <b>awaiting_payment</b></div><div>grant <b>issued after pay</b></div><div>resume <b>stored envelope</b></div></div><div class='panel'><div class='mono'>Rails</div><h2>Stripe, x402, Link-ready.</h2><p>Generic for any agent: Discord, web, CLI, or bespoke runtime. Payjent gates the action; your agent executes its own downstream work after authorization.</p></div></div></section></main></body></html>"""

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


def _validate_callback_url(callback_url: str | None, settings: Settings) -> str | None:
    if not callback_url:
        return None
    parsed = urlparse(callback_url)
    if parsed.scheme not in {"https", "http"} or not parsed.netloc:
        raise HTTPException(status_code=422, detail="callback_url must be an absolute http(s) URL")
    if parsed.scheme == "http":
        allowed_local = parsed.hostname in {"testserver", "127.0.0.1", "localhost"}
        if settings.is_production or not allowed_local:
            raise HTTPException(status_code=422, detail="callback_url must use https outside local/test")
    return callback_url


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
    grant_line = "<p>Access issued; agent will resume automatically.</p>" if grant else "<p>Access not issued yet.</p>"
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


@app.get("/healthz")
def healthz(session: Session = Depends(get_session)):
    bind = session.get_bind()
    backend = bind.dialect.name
    try:
        session.exec(text("select 1")).one()
    except Exception:
        return JSONResponse(
            status_code=503,
            content={"status": "unhealthy", "database": {"ok": False, "backend": backend}},
        )
    return {"status": "ok", "database": {"ok": True, "backend": backend}}


@app.get("/status/{payment_session_id}", response_class=HTMLResponse)
def status_page(payment_session_id: str, session: Session = Depends(get_session)):
    ps, q, grant, fulfillment = _find_session_bundle(session, payment_session_id)
    fulfillment_items = "".join(f"<li>{_html_escape(ev.status)} <code>{_html_escape(ev.id)}</code></li>" for ev in fulfillment) or "<li>none</li>"
    grant_state = "not issued"
    if grant:
        grant_state = f"issued; consumed: {_html_escape(grant.consumed_at is not None)}; agent will resume automatically"
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
      <p>Access: {grant_state}</p>
      {link_instructions}
      <h2>Fulfillment</h2><ul>{fulfillment_items}</ul>
      <p><a href="/pay/{_html_escape(ps.id)}">Back to checkout</a></p>
    </body></html>
    """


def _rail_to_read(r: RailConnection) -> RailConnectionRead:
    return RailConnectionRead(rail=r.rail, status=r.status, mode=r.mode, config=r.config_json)


def _agent_to_read(agent: AgentProfile, session: Session) -> AgentRead:
    rails = session.exec(select(RailConnection).where(RailConnection.agent_id == agent.id)).all()
    return AgentRead(
        id=agent.id,
        owner_id=agent.owner_id,
        bot_id=agent.bot_id,
        name=agent.name,
        platform=agent.platform,
        callback_url=agent.callback_url,
        default_currency=agent.default_currency,
        status=agent.status,
        rails={r.rail: _rail_to_read(r) for r in rails},
    )


def _upsert_rail(session: Session, agent: AgentProfile, rail: str, status: str, mode: str, config: dict) -> RailConnection:
    existing = session.exec(select(RailConnection).where(RailConnection.agent_id == agent.id, RailConnection.rail == rail)).first()
    now = datetime.now(timezone.utc)
    if existing:
        existing.status = status
        existing.mode = mode
        existing.config_json = config
        existing.updated_at = now
        rail_row = existing
    else:
        rail_row = RailConnection(id=f"rail_{uuid4().hex}", agent_id=agent.id, bot_id=agent.bot_id, rail=rail, status=status, mode=mode, config_json=config, updated_at=now)
    session.add(rail_row); session.commit(); session.refresh(rail_row)
    return rail_row


def quote_to_read(q: Quote) -> QuoteRead:
    return QuoteRead.model_validate(q, from_attributes=True)


def session_to_read(s: PaymentSession) -> PaymentSessionRead:
    return PaymentSessionRead.model_validate(s, from_attributes=True)


def _extract_bootstrap_token(authorization: str | None, x_payjent_bootstrap_token: str | None) -> str | None:
    if x_payjent_bootstrap_token:
        return x_payjent_bootstrap_token
    if authorization:
        scheme, _, token = authorization.partition(" ")
        if scheme.lower() == "bearer" and token:
            return token
    return None


def require_bootstrap_token(
    authorization: str | None = Header(default=None, alias="Authorization"),
    x_payjent_bootstrap_token: str | None = Header(default=None, alias="X-Payjent-Bootstrap-Token"),
    settings: Settings = Depends(get_settings),
) -> None:
    expected = settings.bootstrap_token
    if not expected:
        raise HTTPException(status_code=404, detail="bootstrap disabled")
    supplied = _extract_bootstrap_token(authorization, x_payjent_bootstrap_token)
    if not supplied or not hmac.compare_digest(supplied.encode("utf-8"), expected.encode("utf-8")):
        raise HTTPException(status_code=401, detail="invalid bootstrap token")


@app.post("/api/v1/bootstrap/hosted-smoke", response_model=HostedSmokeBootstrapResponse)
def hosted_smoke_bootstrap(
    payload: HostedSmokeBootstrapRequest,
    session: Session = Depends(get_session),
    settings: Settings = Depends(get_settings),
    _authorized: None = Depends(require_bootstrap_token),
):
    bot_id = payload.bot_id.strip()
    operator_id = payload.operator_id.strip()
    if not bot_id or not operator_id:
        raise HTTPException(status_code=422, detail="bot_id and operator_id are required")
    callback_url = _validate_callback_url(payload.callback_url, settings)
    agent = session.exec(select(AgentProfile).where(AgentProfile.bot_id == bot_id)).first()
    if not agent:
        agent = AgentProfile(
            id=f"agent_{uuid4().hex}",
            bot_id=bot_id,
            name=payload.agent_name,
            platform=payload.platform,
            callback_url=callback_url,
            default_currency=payload.default_currency.upper(),
        )
        session.add(agent)
        session.commit()
        session.refresh(agent)
    bot_key = generate_api_key()
    operator_key = generate_api_key()
    create_bot_credential(session, bot_id, bot_key, settings.signing_secret, role="bot")
    create_bot_credential(session, operator_id, operator_key, settings.signing_secret, role="operator")
    return HostedSmokeBootstrapResponse(
        bot_id=bot_id,
        operator_id=operator_id,
        agent=_agent_to_read(agent, session),
        bot_api_key=bot_key,
        operator_api_key=operator_key,
    )


def _redacted_status(status: AgentActionStatusResponse | dict) -> dict:
    data = status if isinstance(status, dict) else status.model_dump()
    return {
        "status": data.get("status"),
        "payment_status": data.get("payment_status"),
        "payment_token_status": data.get("payment_token_status"),
        "token_present": bool(data.get("payment_token")),
        "token_redacted": bool(data.get("payment_token")),
    }


@app.post("/api/v1/smoke/agent-webhook", response_model=HostedSmokeStatusResponse)
def hosted_smoke_agent_webhook(
    payload: HostedSmokeStatusRequest | None = None,
    request: Request = None,
    session: Session = Depends(get_session),
    settings: Settings = Depends(get_settings),
    _authorized: None = Depends(require_bootstrap_token),
):
    payload = payload or HostedSmokeStatusRequest()
    bot_id = (payload.bot_id or f"hosted-smoke-bot-{uuid4().hex[:8]}").strip()
    operator_id = (payload.operator_id or f"hosted-smoke-operator-{uuid4().hex[:8]}").strip()
    if not bot_id or not operator_id:
        raise HTTPException(status_code=422, detail="bot_id/operator_id may not be blank")
    callback_url = _validate_callback_url(payload.callback_url, settings)
    callback_mode = "external" if callback_url else "none"

    bot_key = generate_api_key()
    operator_key = generate_api_key()
    bot_credential = create_bot_credential(session, bot_id, bot_key, settings.signing_secret, role="bot")
    operator_credential = create_bot_credential(session, operator_id, operator_key, settings.signing_secret, role="operator")

    action_payload = PayShPremiumActionCreate(
        bot_id=bot_id,
        external_user_id="hosted-smoke-user",
        request_summary="Payjent hosted smoke: verify gate/resume/fulfillment metadata only",
        amount_minor=100,
        currency="USD",
        cost_breakdown=[{"label": "hosted smoke", "amount_minor": 100}],
        service_url="https://example.invalid/payjent-hosted-smoke",
        method="POST",
        body={"smoke": True},
        description="Payjent hosted smoke metadata check; pay.sh is not executed by this endpoint.",
        callback_url=callback_url,
    )
    action = create_pay_sh_premium_action(action_payload, idempotency_key=None, provider="mock", session=session, settings=settings, credential=bot_credential)
    action_data = action if isinstance(action, dict) else action.model_dump()
    action_id = action_data["action_id"]
    payment_session_id = action_data["payment_session_id"]
    unpaid = get_agent_action_status(action_id, session=session, credential=bot_credential)

    if not (settings.effective_mock_provider_enabled or settings.hosted_smoke_test_rail_enabled):
        raise HTTPException(status_code=503, detail="hosted smoke test rail unavailable")
    ps = session.get(PaymentSession, payment_session_id)
    q = session.get(Quote, action_id)
    if not ps or not q:
        raise HTTPException(500, "hosted smoke action was not persisted")
    receipt, grant = complete_mock_payment(session, q, ps, settings.signing_secret, settings.grant_ttl_seconds)
    _deliver_agent_action_callback(session, q, ps, settings, "mock")
    callback_attempt = session.exec(select(WebhookDeliveryAttempt).where(WebhookDeliveryAttempt.payment_session_id == payment_session_id).order_by(WebhookDeliveryAttempt.created_at.desc())).first()
    paid = get_agent_action_status(action_id, session=session, credential=bot_credential)
    paid_data = paid if isinstance(paid, dict) else paid.model_dump()
    payment_token = paid_data.get("payment_token")
    if not payment_token:
        raise HTTPException(500, "hosted smoke payment token was not issued")
    resumed = consume_agent_action(
        action_id,
        AgentActionConsumeRequest(payment_token=payment_token, presentation=GrantPresentation(bot_id=bot_id, external_user_id="hosted-smoke-user", request_hash=paid_data.get("request_hash"))),
        session=session,
        settings=settings,
        credential=bot_credential,
    )
    fulfilled = complete_agent_action(
        action_id,
        FulfillmentCreate(status="fulfilled", metadata={"smoke": True, "provider": "pay_sh", "settlement": "external_pay_sh_runtime"}),
        session=session,
        credential=bot_credential,
    )
    callback_payload = callback_attempt.payload if callback_attempt else {}
    resumed_data = resumed if isinstance(resumed, dict) else resumed.model_dump()
    fulfilled_data = fulfilled if isinstance(fulfilled, dict) else fulfilled.model_dump()
    resumed_status = resumed_data.get("status")
    fulfilled_status = fulfilled_data.get("status")
    base_url = str(request.base_url).rstrip("/") if request else ""
    public_base_url = settings.public_base_url or base_url
    return HostedSmokeStatusResponse(
        ok=fulfilled_status == "fulfilled" and resumed_status == "ready_to_execute",
        base_url=base_url,
        public_base_url=public_base_url,
        action_id=action_id,
        payment_session_id=payment_session_id,
        payment_link_exists=bool(action_data.get("payment_url")),
        callback_mode=callback_mode,
        callback_contains_payment_token="payment_token" in callback_payload,
        callback_contains_grant=any("grant" in key.lower() for key in callback_payload),
        unpaid_poll=_redacted_status(unpaid),
        paid_poll=_redacted_status(paid),
        resumed_status=resumed_status,
        fulfilled_status=fulfilled_status,
    )


@app.post("/api/v1/agents/register", response_model=AgentRegisterResponse)
def register_agent(payload: AgentRegisterRequest, session: Session = Depends(get_session), settings: Settings = Depends(get_settings), _credential: BotCredential = Depends(require_operator_credential)):
    existing = session.exec(select(AgentProfile).where(AgentProfile.bot_id == payload.bot_id)).first()
    generated_key = None
    if existing:
        return AgentRegisterResponse(agent=_agent_to_read(existing, session), bot_api_key=None)
    agent = AgentProfile(id=f"agent_{uuid4().hex}", bot_id=payload.bot_id, name=payload.name, platform=payload.platform, callback_url=payload.callback_url, default_currency=payload.default_currency.upper())
    session.add(agent); session.commit(); session.refresh(agent)
    if not session.exec(select(BotCredential).where(BotCredential.bot_id == agent.bot_id, BotCredential.role == "bot")).first():
        generated_key = generate_api_key()
        create_bot_credential(session, agent.bot_id, generated_key, settings.signing_secret, role="bot")
    return AgentRegisterResponse(agent=_agent_to_read(agent, session), bot_api_key=generated_key)


@app.get("/api/v1/agents", response_model=list[AgentRead])
def list_agents(session: Session = Depends(get_session), _credential: BotCredential = Depends(require_operator_credential)):
    agents = session.exec(select(AgentProfile).order_by(AgentProfile.created_at.desc())).all()
    return [_agent_to_read(a, session) for a in agents]


@app.post("/api/v1/agents/{agent_id}/stripe-connect/start", response_model=StripeConnectStartResponse)
def start_stripe_connect(agent_id: str, session: Session = Depends(get_session), settings: Settings = Depends(get_settings), _credential: BotCredential = Depends(require_operator_credential)):
    agent = session.get(AgentProfile, agent_id)
    if not agent:
        raise HTTPException(404, "agent not found")
    if settings.is_production:
        raise HTTPException(503, "live Stripe Connect OAuth is not configured; refusing to simulate production Connect")
    mode = "test" if settings.stripe_secret_key and "test" in settings.stripe_secret_key else "local"
    account_id = f"acct_test_{agent.id[-12:]}"
    onboarding_url = f"/dashboard/agents/{agent.id}?stripe_onboarding=simulated&account_id={account_id}"
    _upsert_rail(session, agent, "stripe_connect", "onboarding_started", mode, {"account_id": account_id, "onboarding_url": onboarding_url})
    return StripeConnectStartResponse(mode=mode, account_id=account_id, onboarding_url=onboarding_url, status="onboarding_started")


@app.post("/api/v1/agents/{agent_id}/stripe-connect/complete", response_model=RailConnectionRead)
def complete_stripe_connect(agent_id: str, account_id: str | None = None, session: Session = Depends(get_session), settings: Settings = Depends(get_settings), _credential: BotCredential = Depends(require_operator_credential)):
    agent = session.get(AgentProfile, agent_id)
    if not agent:
        raise HTTPException(404, "agent not found")
    if settings.is_production:
        raise HTTPException(503, "live Stripe Connect completion is not configured")
    existing = session.exec(select(RailConnection).where(RailConnection.agent_id == agent.id, RailConnection.rail == "stripe_connect")).first()
    acct = account_id or (existing.config_json.get("account_id") if existing else None) or f"acct_test_{agent.id[-12:]}"
    rail = _upsert_rail(session, agent, "stripe_connect", "connected", existing.mode if existing else "local", {"account_id": acct})
    return _rail_to_read(rail)


@app.post("/api/v1/agents/{agent_id}/x402/configure", response_model=RailConnectionRead)
def configure_x402(agent_id: str, payload: X402ConfigureRequest, session: Session = Depends(get_session), _credential: BotCredential = Depends(require_operator_credential)):
    agent = session.get(AgentProfile, agent_id)
    if not agent:
        raise HTTPException(404, "agent not found")
    if payload.enabled and payload.max_per_call_minor > payload.max_per_request_minor:
        raise HTTPException(status_code=422, detail="max_per_call_minor must be <= max_per_request_minor")
    config = payload.model_dump()
    rail = _upsert_rail(session, agent, "x402", "enabled" if payload.enabled else "disabled", "test", config)
    return _rail_to_read(rail)


def _integration_snippet(agent: AgentProfile) -> str:
    return f"""export PAYJENT_BASE_URL=http://localhost:8000
export PAYJENT_BOT_ID={agent.bot_id}
export PAYJENT_BOT_KEY=<shown-once-from-registration>
python -m payjent.demo discord-aggregator-stripe-smoke"""


def _create_agent_profile_from_form(form: dict[str, str], account: Account, session: Session, settings: Settings) -> tuple[AgentProfile, str | None, bool]:
    name = (form.get("name") or "").strip()
    platform = (form.get("platform") or "").strip()
    bot_id = (form.get("bot_id") or "").strip()
    default_currency = (form.get("default_currency") or "USD").strip().upper() or "USD"
    callback_url = _validate_callback_url((form.get("callback_url") or "").strip() or None, settings)
    if not name or not platform or not bot_id:
        raise HTTPException(status_code=422, detail="agent name, platform, and bot_id are required")
    existing = session.exec(select(AgentProfile).where(AgentProfile.bot_id == bot_id)).first()
    if existing:
        return existing, None, False
    agent = AgentProfile(
        id=f"agent_{uuid4().hex}",
        owner_id=account.id,
        bot_id=bot_id,
        name=name,
        platform=platform,
        callback_url=callback_url,
        default_currency=default_currency,
    )
    session.add(agent)
    session.commit()
    session.refresh(agent)
    generated_key = None
    if not session.exec(select(BotCredential).where(BotCredential.bot_id == agent.bot_id, BotCredential.role == "bot")).first():
        generated_key = generate_api_key()
        create_bot_credential(session, agent.bot_id, generated_key, settings.signing_secret, role="bot")
    return agent, generated_key, True


def _credential_display_html(account: Account, agent: AgentProfile, bot_api_key: str | None, created: bool) -> str:
    key_block = ""
    if bot_api_key:
        key_block = f"""<div class='card'><h3>Copy this Payjent agent credential now</h3><p class='warnbox'><b>Shown once.</b> Payjent will not show this value again. Store it in the agent's private secret/tool store as <code>X-Payjent-Bot-Key</code> / Payjent agent credential. Do not paste it into chat.</p><pre><code>{_html_escape(bot_api_key)}</code></pre><p><a class='btn' href='/docs/agent-payjent-self-setup.md'>Open agent setup guide</a></p></div>"""
    else:
        key_block = """<div class='card'><h3>Existing agent found</h3><p class='muted'>This bot_id is already registered. Existing credentials are copy-once and cannot be revealed. If the original value was lost, create another credential from this agent's command view and store the new value privately.</p></div>"""
    status = "Agent registered" if created else "Agent already registered"
    return f"<!doctype html><html><head><title>{_html_escape(status)} · Payjent</title>{_DASHBOARD_CSS}</head><body><main><div class='topbar'><span class='muted'>Signed in as <b>{_html_escape(account.email)}</b></span><form method='post' action='/auth/logout'><button class='logout' type='submit'>Log out</button></form></div><section class='hero'><div class='eyebrow'>One-time credential</div><h1>{_html_escape(status)}</h1><p class='muted'>{_html_escape(agent.name)} · <code>{_html_escape(agent.bot_id)}</code></p></section><div class='grid'>{key_block}<div class='card'><h3>Next steps</h3><ol><li>Copy the one-time credential.</li><li>Store it in the agent's private secret store as <code>X-Payjent-Bot-Key</code>.</li><li>Send the agent this guide: <a href='/docs/agent-payjent-self-setup.md'>/docs/agent-payjent-self-setup.md</a>.</li><li>Return to the agent command view to configure rails.</li></ol><p><a class='btn' href='/dashboard/agents/{_html_escape(agent.id)}'>Open agent command view</a></p></div></div></main></body></html>"


_DASHBOARD_CSS = """<link rel='preconnect' href='https://fonts.googleapis.com'><link rel='preconnect' href='https://fonts.gstatic.com' crossorigin><link href='https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@500;700&family=Spline+Sans+Mono:wght@400;600;700&display=swap' rel='stylesheet'><style>
:root{--void:#020504;--panel:#0c1b17e8;--card:#10221dcc;--ivory:#f4efd8;--muted:#aab9aa;--cyan:#3ff4ff;--lime:#d6ff5f;--line:rgba(244,239,216,.18);--warn:#ffdc6e;--bad:#ff8b7d}*{box-sizing:border-box}body{margin:0;font-family:'Space Grotesk',ui-sans-serif,sans-serif;color:var(--ivory);background:radial-gradient(circle at 85% 0,rgba(63,244,255,.16),transparent 30%),radial-gradient(circle at 0 20%,rgba(214,255,95,.12),transparent 26%),linear-gradient(180deg,#020504,#07110f 60%,#020504);min-height:100vh}body:before{content:"";position:fixed;inset:0;pointer-events:none;opacity:.24;background:repeating-linear-gradient(90deg,rgba(244,239,216,.045) 0 1px,transparent 1px 68px),repeating-linear-gradient(0deg,rgba(244,239,216,.035) 0 1px,transparent 1px 44px)}a{color:var(--cyan)}main{max-width:1220px;margin:auto;padding:32px 20px 70px}.hero{position:relative;overflow:hidden;padding:34px;border:1px solid var(--line);border-radius:34px;background:linear-gradient(135deg,rgba(16,34,29,.92),rgba(2,5,4,.72));box-shadow:0 34px 100px #000a,inset 0 1px rgba(255,255,255,.12)}.hero:after{content:'PAYJENT / KINETIC ESCROW';position:absolute;right:-50px;bottom:18px;color:rgba(244,239,216,.06);font:700 54px 'Spline Sans Mono';letter-spacing:.08em}.eyebrow{color:var(--lime);font:700 12px 'Spline Sans Mono',monospace;text-transform:uppercase;letter-spacing:.16em}h1{font-size:clamp(38px,6vw,68px);line-height:.9;letter-spacing:-.055em;margin:12px 0 14px}h2{margin:28px 0 12px}h3{margin:8px 0 10px}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(250px,1fr));gap:18px;margin-top:22px}.card{position:relative;background:linear-gradient(180deg,rgba(16,34,29,.86),rgba(7,17,15,.86));border:1px solid var(--line);border-radius:24px;padding:20px;box-shadow:0 20px 70px #0007;overflow:hidden}.card:before{content:"";position:absolute;inset:0 0 auto;height:3px;background:linear-gradient(90deg,var(--lime),transparent,var(--cyan));opacity:.75}.pill{display:inline-flex;border:1px solid rgba(63,244,255,.32);border-radius:999px;padding:5px 10px;font:700 12px 'Spline Sans Mono';background:rgba(63,244,255,.08);color:var(--cyan)}.ok{border-color:rgba(214,255,95,.38);background:rgba(214,255,95,.09);color:var(--lime)}.warn{border-color:rgba(255,220,110,.38);background:rgba(255,220,110,.09);color:var(--warn)}pre{overflow:auto;background:#020504;color:#e9ffe3;border:1px solid var(--line);border-radius:16px;padding:16px;font-family:'Spline Sans Mono',monospace}.muted{color:var(--muted)}.fine{font-size:13px;color:#9fae9d}.warnbox{background:rgba(255,220,110,.12);color:#fff2b4;border:1px solid rgba(255,220,110,.42);border-radius:16px;padding:13px}.btn,button{display:inline-block;background:linear-gradient(135deg,var(--lime),var(--cyan));color:#03100e;text-decoration:none;border:0;border-radius:999px;padding:11px 15px;font-weight:700;cursor:pointer;box-shadow:0 12px 36px #0007}.topbar{display:flex;justify-content:space-between;align-items:center;margin-bottom:18px}.logout{background:rgba(244,239,216,.07);color:var(--ivory);border:1px solid var(--line);box-shadow:none}.auth-wrap{min-height:100vh;display:grid;place-items:center;padding:24px}.auth-card{width:min(500px,100%);background:linear-gradient(180deg,rgba(16,34,29,.94),rgba(2,5,4,.9));border:1px solid var(--line);border-radius:30px;padding:30px;box-shadow:0 36px 110px #000b}.error{background:rgba(255,139,125,.13);color:#ffd5cf;border:1px solid rgba(255,139,125,.55);border-radius:14px;padding:10px 12px;margin:12px 0}label{display:block;font-weight:700;margin-top:12px}input{width:100%;margin-top:6px;margin-bottom:8px;border:1px solid var(--line);border-radius:14px;background:#020504;color:var(--ivory);padding:12px 13px;font:600 15px 'Space Grotesk'}code{font-family:'Spline Sans Mono';color:#e7ffb0}.stat{font-size:28px;font-weight:700;color:var(--lime);line-height:1.15}.timeline{display:grid;gap:10px}.event{border-left:3px solid var(--cyan);background:rgba(244,239,216,.045);border-radius:14px;padding:12px}ol li{margin:10px 0}@media(max-width:720px){main{padding:20px 14px}.topbar{gap:12px;align-items:flex-start;flex-direction:column}.hero{padding:24px}.hero:after{display:none}}
</style>"""


async def _form_fields(request: Request) -> dict[str, str]:
    body = (await request.body()).decode("utf-8")
    parsed = parse_qs(body, keep_blank_values=True)
    return {key: values[-1] if values else "" for key, values in parsed.items()}


def _auth_page(kind: str, error: str | None = None, email: str = "", settings: Settings | None = None) -> HTMLResponse:
    is_register = kind == "register"
    title = "Create your Payjent account" if is_register else "Log in to Payjent"
    action = "/auth/register" if is_register else "/auth/login"
    alternate = "Already have an account? <a href='/auth/login'>Log in</a>" if is_register else "New to Payjent? <a href='/auth/register'>Create an account</a>"
    error_html = f"<div class='error'>{_html_escape(error)}</div>" if error else ""
    workos_html = ""
    if settings and workos_auth.workos_configured(settings):
        workos_html = "<p><a class='btn' href='/auth/workos/login'>Sign in with WorkOS AuthKit</a></p><p class='fine'>Hosted WorkOS AuthKit is configured for this Payjent instance.</p><hr>"
    else:
        workos_html = "<p class='fine'>WorkOS AuthKit sign-in is not configured; use Payjent account auth below.</p>"
    return HTMLResponse(f"""<!doctype html><html><head><title>{title}</title>{_DASHBOARD_CSS}</head><body><div class='auth-wrap'><section class='auth-card'><div class='eyebrow'>Payjent secure command center</div><h1>{title}</h1><p class='muted'>Use WorkOS to enter the operator dashboard, register your agent, issue agent credentials, then teach the agent how to create paid actions and resume after payment.</p>{error_html}{workos_html}<form method='post' action='{action}'><label>Email</label><input name='email' type='email' autocomplete='email' required value='{_html_escape(email)}'><label>Password</label><input name='password' type='password' autocomplete='current-password' minlength='8' required><button type='submit'>{'Create fallback account' if is_register else 'Log in with fallback auth'}</button></form><p class='fine'>{alternate}</p><p class='fine'>First-party auth remains available as a fallback. Use a unique password and a production PAYJENT_SIGNING_SECRET.</p></section></div></body></html>""")


def _set_session(response: RedirectResponse, account: Account, settings: Settings) -> RedirectResponse:
    response.set_cookie(DASHBOARD_SESSION_COOKIE, create_dashboard_session_cookie(account.id, settings.signing_secret), httponly=True, samesite="lax", secure=settings.is_production, max_age=60 * 60 * 24 * 7, path="/")
    return response


def _clear_session(response: RedirectResponse) -> RedirectResponse:
    response.delete_cookie(DASHBOARD_SESSION_COOKIE, path="/")
    return response


def _money_totals_by_currency(rows, statuses: set[str]) -> dict[str, int]:
    totals: dict[str, int] = {}
    for row in rows:
        if row.status not in statuses:
            continue
        currency = (row.currency or "USD").upper()
        totals[currency] = totals.get(currency, 0) + int(row.amount_minor)
    return totals


def _format_money_totals(totals: dict[str, int]) -> str:
    if not totals:
        return "No paid volume yet"
    return "<br>".join(_html_escape(_format_money(amount, currency)) for currency, amount in sorted(totals.items()))


def _sessions_by_quote_id(session: Session, quote_ids: list[str]) -> dict[str, PaymentSession]:
    if not quote_ids:
        return {}
    sessions = session.exec(select(PaymentSession).where(PaymentSession.quote_id.in_(quote_ids))).all()
    return {payment_session.quote_id: payment_session for payment_session in sessions}


def _require_dashboard_account(request: Request, session: Session, settings: Settings):
    account = get_account_from_cookie(request.cookies.get(DASHBOARD_SESSION_COOKIE), session, settings)
    if account:
        return account
    has_accounts = session.exec(select(Account.id).limit(1)).first() is not None
    return RedirectResponse("/auth/login" if has_accounts else "/auth/register", status_code=303)


@app.get("/auth/register", response_class=HTMLResponse)
def register_page(settings: Settings = Depends(get_settings)):
    return _auth_page("register", settings=settings)


@app.post("/auth/register")
async def register_account(request: Request, session: Session = Depends(get_session), settings: Settings = Depends(get_settings)):
    form = await _form_fields(request)
    email = normalize_email(form.get("email", ""))
    password = form.get("password", "")
    if "@" not in email or "." not in email.rsplit("@", 1)[-1]:
        return _auth_page("register", "Enter a valid email address.", email, settings)
    if len(password) < 8:
        return _auth_page("register", "Password must be at least 8 characters.", email, settings)
    account = Account(id=f"acct_{uuid4().hex}", email=email, password_hash=hash_password(password))
    session.add(account)
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        return _auth_page("register", "An account with that email already exists. Log in instead.", email, settings)
    session.refresh(account)
    return _set_session(RedirectResponse("/dashboard", status_code=303), account, settings)


@app.get("/auth/login", response_class=HTMLResponse)
def login_page(settings: Settings = Depends(get_settings)):
    return _auth_page("login", settings=settings)


@app.post("/auth/login")
async def login_account(request: Request, session: Session = Depends(get_session), settings: Settings = Depends(get_settings)):
    form = await _form_fields(request)
    email = normalize_email(form.get("email", ""))
    password = form.get("password", "")
    account = session.exec(select(Account).where(Account.email == email)).first()
    if not account or not verify_password(password, account.password_hash):
        return _auth_page("login", "Invalid email or password.", email, settings)
    return _set_session(RedirectResponse("/dashboard", status_code=303), account, settings)


@app.get("/auth/workos/login")
def workos_login(settings: Settings = Depends(get_settings)):
    redirect_uri = workos_auth.require_workos_config(settings)
    client = workos_auth.create_workos_client(settings)
    return RedirectResponse(workos_auth.get_authorization_url(client, redirect_uri), status_code=303)


@app.get("/auth/workos/callback")
def workos_callback(code: str | None = None, session: Session = Depends(get_session), settings: Settings = Depends(get_settings)):
    if not code:
        return HTMLResponse("WorkOS authentication failed: missing authorization code.", status_code=400)
    try:
        workos_auth.require_workos_config(settings)
        client = workos_auth.create_workos_client(settings)
        profile = workos_auth.authenticate_with_code(client, code)
    except HTTPException:
        raise
    except Exception:
        return HTMLResponse("WorkOS authentication failed. Please try again.", status_code=401)

    email = normalize_email(profile.email)
    account = session.exec(select(Account).where(Account.email == email)).first()
    if account:
        account.auth_provider = "workos"
        if profile.user_id:
            account.workos_user_id = profile.user_id
    else:
        password_hash = None if account_password_hash_nullable(session) else WORKOS_UNUSABLE_PASSWORD_HASH
        account = Account(id=f"acct_{uuid4().hex}", email=email, password_hash=password_hash, auth_provider="workos", workos_user_id=profile.user_id)
    session.add(account)
    session.commit()
    session.refresh(account)
    return _set_session(RedirectResponse("/dashboard", status_code=303), account, settings)


@app.post("/auth/logout")
def logout_account():
    return _clear_session(RedirectResponse("/auth/login", status_code=303))


@app.get("/auth/logout")
def logout_account_get():
    return _clear_session(RedirectResponse("/auth/login", status_code=303))


@app.get("/dashboard", response_class=HTMLResponse)
def dashboard(request: Request, session: Session = Depends(get_session), settings: Settings = Depends(get_settings)):
    account = _require_dashboard_account(request, session, settings)
    if isinstance(account, RedirectResponse):
        return account
    agents = session.exec(select(AgentProfile).order_by(AgentProfile.created_at.desc())).all()
    quotes = session.exec(select(Quote).order_by(Quote.created_at.desc()).limit(20)).all()
    spends = session.exec(select(SpendLedgerEntry).order_by(SpendLedgerEntry.created_at.desc()).limit(20)).all()
    sessions_by_quote = _sessions_by_quote_id(session, [q.id for q in quotes])
    paid_totals = _money_totals_by_currency(quotes, {"paid", "fulfilled", "executing"})
    spend_totals = _money_totals_by_currency(spends, {"authorized", "captured"})
    cards = "".join(f"<div class='card' data-agent-id='{_html_escape(a.id)}'><span class='pill'>{_html_escape(a.platform)}</span><h3>{_html_escape(a.name)}</h3><p class='muted'><code>{_html_escape(a.bot_id)}</code></p><p><a class='btn' href='/dashboard/agents/{_html_escape(a.id)}'>Open command view</a></p></div>" for a in agents) or "<div class='card'><h3>No agents yet</h3><p class='muted'>Use the Register agent form above. Payjent will show the generated credential once, then never reveal it again.</p></div>"
    interactions = "".join(f"<div class='event' data-quote-id='{_html_escape(q.id)}'><b>{_html_escape(q.status)}</b> · {_html_escape(_format_money(q.amount_minor, q.currency))}<br><span class='muted'>{_html_escape(q.request_summary)}</span><br><span class='muted'>How paid: {_html_escape(sessions_by_quote[q.id].provider)} / {_html_escape(sessions_by_quote[q.id].status)}</span></div>" if q.id in sessions_by_quote else f"<div class='event' data-quote-id='{_html_escape(q.id)}'><b>{_html_escape(q.status)}</b> · {_html_escape(_format_money(q.amount_minor, q.currency))}<br><span class='muted'>{_html_escape(q.request_summary)}</span><br><span class='muted'>How paid: no payment session yet</span></div>" for q in quotes[:6]) or "<div class='event'><b>No interactions yet</b><br><span class='muted'>Paid action requests will appear here when your agents create real Payjent quotes.</span></div>"
    spend_events = "".join(f"<div class='event' data-spend-id='{_html_escape(s.id)}'><b>{_html_escape(s.tool)} → {_html_escape(s.vendor)}</b> · {_html_escape(_format_money(s.amount_minor, s.currency))}<br><span class='muted'>{_html_escape(s.reason or 'No reason supplied by agent.')}</span></div>" for s in spends[:6]) or "<div class='event'><b>No downstream spend yet</b><br><span class='muted'>Reason-backed spend ledger entries appear only after an agent consumes a grant and requests spend authorization.</span></div>"
    register_form = """<form method='post' action='/dashboard/agents/register'><label>Agent name</label><input name='name' placeholder='Research assistant' required><label>Platform</label><input name='platform' placeholder='discord, web, slack, cli' required><label>Bot ID</label><input name='bot_id' placeholder='stable-agent-id' required><label>Default currency</label><input name='default_currency' value='USD' maxlength='3' required><label>Callback URL (optional)</label><input name='callback_url' type='url' placeholder='https://agent.example/callback'><button type='submit'>Register agent and create credential</button></form>"""
    return f"<!doctype html><html><head><title>Payjent dashboard</title>{_DASHBOARD_CSS}</head><body><main><div class='topbar'><span class='muted'>Signed in as <b>{_html_escape(account.email)}</b></span><form method='post' action='/auth/logout'><button class='logout' type='submit'>Log out</button></form></div><section class='hero'><div class='eyebrow'>Payment operations</div><h1>Agent payment command center</h1><p class='muted'>Exact setup flow: Sign in with WorkOS → Dashboard → Register agent → Copy one-time credential → Store in the agent secret store as X-Payjent-Bot-Key → Send /docs/agent-payjent-self-setup.md to the agent. Credentials are shown once and never on ordinary dashboard pages. Stripe Connect, x402, and integration snippets remain on each agent detail page.</p><div class='grid'><div class='card'><h3>Register agent</h3><p class='muted'>Use this authenticated form; no operator API key or curl is needed in the browser.</p>{register_form}<p class='fine'>After submit, copy the generated Payjent agent credential immediately. Do not paste it into chat.</p></div><div class='card'><h3>Agents</h3><div class='stat'>{len(agents)}</div><p class='muted'>Registered agent identities with copy-once credentials.</p></div><div class='card'><h3>Paid action volume</h3><div class='stat'>{_format_money_totals(paid_totals)}</div><p class='muted'>Grouped by currency from recent paid/fulfilled quotes; currencies are never mixed.</p></div><div class='card'><h3>Downstream spend</h3><div class='stat'>{_format_money_totals(spend_totals)}</div><p class='muted'>Authorized or captured spend ledger totals, grouped by currency.</p></div></div></section><h2>Agents</h2><div class='grid'>{cards}</div><div class='grid'><div class='card'><h3>Recent agent interactions</h3><div class='timeline'>{interactions}</div></div><div class='card'><h3>Spend reasoning trail</h3><div class='timeline'>{spend_events}</div></div></div></main></body></html>"


@app.post("/dashboard/agents/register", response_class=HTMLResponse)
async def dashboard_register_agent(request: Request, session: Session = Depends(get_session), settings: Settings = Depends(get_settings)):
    account = _require_dashboard_account(request, session, settings)
    if isinstance(account, RedirectResponse):
        return account
    form = await _form_fields(request)
    agent, bot_api_key, created = _create_agent_profile_from_form(form, account, session, settings)
    return HTMLResponse(_credential_display_html(account, agent, bot_api_key, created))


@app.get("/dashboard/agents/{agent_id}", response_class=HTMLResponse)
def dashboard_agent(agent_id: str, request: Request, session: Session = Depends(get_session), settings: Settings = Depends(get_settings)):
    account = _require_dashboard_account(request, session, settings)
    if isinstance(account, RedirectResponse):
        return account
    agent = session.get(AgentProfile, agent_id)
    if not agent:
        raise HTTPException(404, "agent not found")
    rails = session.exec(select(RailConnection).where(RailConnection.agent_id == agent.id)).all()
    rail_cards = "".join(f"<div class='card'><span class='pill {'ok' if r.status in {'connected','enabled'} else 'warn'}'>{_html_escape(r.status)}</span><h3>{_html_escape(r.rail)}</h3><p class='muted'>mode: {_html_escape(r.mode)}</p><pre><code>{_html_escape(r.config_json)}</code></pre></div>" for r in rails) or "<div class='card'><h3>Rails not configured</h3><p class='muted'>Start Stripe Connect and configure x402 with operator-authenticated API calls.</p></div>"
    quotes = session.exec(select(Quote).where(Quote.bot_id == agent.bot_id).order_by(Quote.created_at.desc()).limit(10)).all()
    quote_ids = [q.id for q in quotes]
    spends = session.exec(select(SpendLedgerEntry).where(SpendLedgerEntry.quote_id.in_(quote_ids)).order_by(SpendLedgerEntry.created_at.desc()).limit(10)).all() if quote_ids else []
    ledger = "".join(f"<li>{_html_escape(s.rail)} {_html_escape(_format_money(s.amount_minor, s.currency))} — {_html_escape(s.status)}</li>" for s in spends) or "<li>No spend ledger entries yet.</li>"
    stripe_cmd = f"curl -X POST /api/v1/agents/{agent.id}/stripe-connect/start -H 'X-Payjent-Bot-Key: &lt;operator-key&gt;'"
    x402_cmd = f"curl -X POST /api/v1/agents/{agent.id}/x402/configure -H 'X-Payjent-Bot-Key: &lt;operator-key&gt;' -H 'Content-Type: application/json' -d '{{\"network\":\"base-sepolia\",\"pay_to\":\"0xTEST_PAY_TO\",\"max_per_request_minor\":900,\"max_per_call_minor\":250,\"enabled\":true}}'"
    credential_form = f"""<form method='post' action='/dashboard/agents/{_html_escape(agent.id)}/credentials'><p class='muted'>Credentials are copy-once. Existing values cannot be revealed. Create another credential only if the agent needs a fresh private key; store it as <code>X-Payjent-Bot-Key</code>.</p><button type='submit'>Create another credential</button></form>"""
    return f"<!doctype html><html><head><title>{_html_escape(agent.name)} · Payjent</title>{_DASHBOARD_CSS}</head><body><main><div class='topbar'><a href='/dashboard'>← Dashboard</a><form method='post' action='/auth/logout'><button class='logout' type='submit'>Log out</button></form></div><section class='hero'><span class='pill'>{_html_escape(agent.status)}</span><h1>{_html_escape(agent.name)}</h1><p class='muted'>{_html_escape(agent.platform)} · <code>{_html_escape(agent.bot_id)}</code></p></section><div class='grid'>{rail_cards}</div><div class='grid'><div class='card'><h3>Agent credential</h3>{credential_form}</div><div class='card'><h3>Stripe Connect</h3><p class='muted'>Local/test starts return a simulated account link; production fails closed until live OAuth is configured.</p><pre><code>{_html_escape(stripe_cmd)}</code></pre></div><div class='card'><h3>x402 rail configuration</h3><p class='muted'>Stores only non-secret network, pay_to, facilitator URL, and caps.</p><pre><code>{_html_escape(x402_cmd)}</code></pre></div></div><div class='card'><h3>Integration snippet</h3><pre><code>{_html_escape(_integration_snippet(agent))}</code></pre></div><div class='card'><h3>Recent payments / spend ledger</h3><p>{len(quotes)} recent quotes</p><ul>{ledger}</ul></div></main></body></html>"


@app.post("/dashboard/agents/{agent_id}/credentials", response_class=HTMLResponse)
def dashboard_create_agent_credential(agent_id: str, request: Request, session: Session = Depends(get_session), settings: Settings = Depends(get_settings)):
    account = _require_dashboard_account(request, session, settings)
    if isinstance(account, RedirectResponse):
        return account
    agent = session.get(AgentProfile, agent_id)
    if not agent:
        raise HTTPException(404, "agent not found")
    generated_key = generate_api_key()
    create_bot_credential(session, agent.bot_id, generated_key, settings.signing_secret, role="bot")
    return HTMLResponse(_credential_display_html(account, agent, generated_key, True))


def _enforce_stripe_checkout_guardrails(settings: Settings) -> None:
    if not settings.stripe_secret_key:
        raise HTTPException(status_code=503, detail="PAYJENT_STRIPE_SECRET_KEY is required for Stripe checkout")
    if settings.is_production and not settings.stripe_webhook_secret:
        raise HTTPException(
            status_code=503,
            detail="PAYJENT_STRIPE_WEBHOOK_SECRET is required for Stripe checkout in production",
        )


@app.post("/api/v1/quotes", response_model=QuoteRead)
def create_quote(payload: QuoteCreate, session: Session = Depends(get_session), settings: Settings = Depends(get_settings), credential: BotCredential = Depends(require_bot_credential)):
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
        "callback_url": _validate_callback_url(getattr(payload, "callback_url", None), settings),
    }
    q = Quote(id=f"quote_{uuid4().hex}", quote_hash=quote_hash(canonical), **canonical)
    session.add(q); session.commit(); session.refresh(q)
    return quote_to_read(q)


@app.get("/api/v1/quotes/{quote_id}", response_model=QuoteRead)
def get_quote(quote_id: str, session: Session = Depends(get_session)):
    q = session.get(Quote, quote_id)
    if not q: raise HTTPException(404, "quote not found")
    return quote_to_read(q)


def _create_checkout_for_quote(
    q: Quote,
    *,
    idempotency_key: str | None,
    provider: str | None,
    session: Session,
    settings: Settings,
) -> PaymentSession:
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
            return existing
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
    return ps


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
    ps = _create_checkout_for_quote(q, idempotency_key=idempotency_key, provider=provider, session=session, settings=settings)
    return session_to_read(ps)


@app.post("/api/v1/agent-actions", response_model=AgentActionCreateResponse)
def create_agent_action(
    payload: AgentActionCreate,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    provider: str | None = Header(default=None, alias="X-Payjent-Provider"),
    session: Session = Depends(get_session),
    settings: Settings = Depends(get_settings),
    credential: BotCredential = Depends(require_bot_credential),
):
    q = create_quote(payload, session=session, settings=settings, credential=credential)
    stored_quote = session.get(Quote, q.id)
    if not stored_quote:
        raise HTTPException(500, "agent action quote was not persisted")
    ps = _create_checkout_for_quote(stored_quote, idempotency_key=idempotency_key or stored_quote.request_hash, provider=provider, session=session, settings=settings)
    return create_paid_action_response(quote=stored_quote, payment_session=ps)


@app.post("/api/v1/premium-actions/pay-sh", response_model=PayShPremiumActionCreateResponse)
def create_pay_sh_premium_action(
    payload: PayShPremiumActionCreate,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    provider: str | None = Header(default=None, alias="X-Payjent-Provider"),
    session: Session = Depends(get_session),
    settings: Settings = Depends(get_settings),
    credential: BotCredential = Depends(require_bot_credential),
):
    try:
        envelope = build_paysh_execution_envelope(
            service_url=payload.service_url,
            service_fqn=payload.service_fqn,
            resource=payload.resource,
            method=payload.method,
            body=payload.body,
            headers=payload.headers,
            description=payload.description or payload.request_summary,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    action_payload = AgentActionCreate(
        bot_id=payload.bot_id,
        external_user_id=payload.external_user_id,
        request_summary=payload.request_summary,
        request_hash=payload.request_hash,
        amount_minor=payload.amount_minor,
        currency=payload.currency,
        cost_breakdown=payload.cost_breakdown,
        execution_envelope=envelope,
        callback_url=payload.callback_url,
    )
    action = create_agent_action(
        action_payload,
        idempotency_key=idempotency_key,
        provider=provider,
        session=session,
        settings=settings,
        credential=credential,
    )
    data = action if isinstance(action, dict) else action.model_dump()
    return {**data, "provider": "pay_sh", "premium_provider": "pay_sh", "command_preview": envelope["command_preview"]}


@app.get("/api/v1/agent-actions/{action_id}", response_model=AgentActionStatusResponse)
def get_agent_action_status(action_id: str, session: Session = Depends(get_session), credential: BotCredential = Depends(require_bot_credential)):
    q = session.get(Quote, action_id)
    if not q:
        raise HTTPException(404, "agent action not found")
    _enforce_bot_scope(credential, q.bot_id)
    payment_session = session.exec(select(PaymentSession).where(PaymentSession.quote_id == q.id).order_by(PaymentSession.created_at.desc())).first()
    grants = session.exec(select(Grant).where(Grant.quote_id == q.id).order_by(Grant.created_at.desc())).all()
    available_grant = next((grant for grant in grants if grant.consumed_at is None), None)
    consumed_grant = next((grant for grant in grants if grant.consumed_at is not None), None)
    payment_token = available_grant.id if available_grant and payment_session and payment_session.status == "paid" else None
    token_status = "available" if payment_token else "consumed" if consumed_grant else "unissued"
    status = "ready" if payment_token else "consumed" if token_status == "consumed" else "awaiting_payment"
    return {
        "action_id": q.id,
        "quote_id": q.id,
        "payment_session_id": payment_session.id if payment_session else None,
        "payment_status": payment_session.status if payment_session else None,
        "quote_status": q.status,
        "status": status,
        "request_hash": q.request_hash,
        "external_user_id": q.external_user_id,
        "amount_minor": q.amount_minor,
        "currency": q.currency,
        "payment_token": payment_token,
        "payment_token_status": token_status,
    }


@app.get("/api/v1/agent-actions/{action_id}/status", response_model=AgentActionStatusResponse)
def get_agent_action_status_alias(action_id: str, session: Session = Depends(get_session), credential: BotCredential = Depends(require_bot_credential)):
    return get_agent_action_status(action_id, session=session, credential=credential)


@app.post("/api/v1/agent-actions/{action_id}/consume", response_model=AgentActionExecutionEnvelope)
def consume_agent_action(action_id: str, payload: AgentActionConsumeRequest, session: Session = Depends(get_session), settings: Settings = Depends(get_settings), credential: BotCredential = Depends(require_bot_credential)):
    q = session.get(Quote, action_id)
    if not q:
        raise HTTPException(404, "agent action not found")
    _enforce_bot_scope(credential, q.bot_id)
    paid_session = session.exec(select(PaymentSession).where(PaymentSession.quote_id == q.id, PaymentSession.status == "paid")).first()
    if not paid_session:
        raise HTTPException(status_code=409, detail="agent action is not paid")
    grant = session.get(Grant, payload.payment_token)
    if not grant or grant.quote_id != q.id:
        raise HTTPException(403, "payment_token is not valid for this action")
    grant = _load_valid_grant(grant.id, payload.presentation, session, settings)
    if grant.quote_id != q.id:
        raise HTTPException(403, "payment_token is not valid for this action")
    if not _mark_grant_consumed(grant.id, session):
        raise HTTPException(409, "payment_token already consumed")
    session.refresh(grant)
    return execution_envelope_for_action(quote=q, grant=grant)


@app.post("/api/v1/agent-actions/{action_id}/start", response_model=AgentActionExecutionEnvelope)
def start_agent_action(action_id: str, payload: AgentActionConsumeRequest, session: Session = Depends(get_session), settings: Settings = Depends(get_settings), credential: BotCredential = Depends(require_bot_credential)):
    return consume_agent_action(action_id, payload, session=session, settings=settings, credential=credential)


@app.post("/api/v1/agent-actions/{action_id}/complete", response_model=AgentActionCompleteResponse)
def complete_agent_action(action_id: str, payload: FulfillmentCreate, session: Session = Depends(get_session), credential: BotCredential = Depends(require_bot_credential)):
    event = record_fulfillment(action_id, payload, session=session, credential=credential)
    stored = session.get(FulfillmentEvent, event.id)
    if not stored:
        raise HTTPException(500, "agent action fulfillment was not persisted")
    return action_result_response(stored)


@app.get("/api/v1/payment-sessions/{session_id}", response_model=PaymentSessionRead)
def get_payment_session(session_id: str, session: Session = Depends(get_session)):
    ps = session.get(PaymentSession, session_id)
    if not ps: raise HTTPException(404, "payment session not found")
    return session_to_read(ps)


def _deliver_agent_action_callback(session: Session, q: Quote, ps: PaymentSession, settings: Settings, provider: str) -> WebhookDeliveryAttempt | None:
    if not q.callback_url:
        return None
    payload = {
        "event_type": "agent_action.ready",
        "action_id": q.id,
        "quote_id": q.id,
        "payment_session_id": ps.id,
        "bot_id": q.bot_id,
        "external_user_id": q.external_user_id,
        "status": "ready",
        "payment_status": ps.status,
        "provider": provider,
        "provider_session_id": ps.provider_session_id,
        "amount_minor": q.amount_minor,
        "currency": q.currency,
        "request_hash": q.request_hash,
    }
    timestamp, signature = sign_webhook_payload(payload, settings.signing_secret)
    attempt = WebhookDeliveryAttempt(
        id=f"wh_{uuid4().hex}", quote_id=q.id, action_id=q.id, payment_session_id=ps.id,
        callback_url=q.callback_url, status="pending", payload=payload,
    )
    try:
        with httpx.Client(timeout=5.0) as client:
            response = client.post(q.callback_url, json=payload, headers={PAYJENT_TIMESTAMP_HEADER: timestamp, PAYJENT_SIGNATURE_HEADER: signature})
        attempt.http_status = response.status_code
        attempt.status = "success" if 200 <= response.status_code < 300 else "failed"
        if attempt.status == "failed":
            attempt.error = response.text[:500]
    except Exception as exc:
        attempt.status = "failed"
        attempt.error = str(exc)[:500]
    session.add(attempt); session.commit(); session.refresh(attempt)
    return attempt


def _issued_response(ps: PaymentSession, receipt, grant):
    return {"payment_session": session_to_read(ps), "receipt": {"payload": receipt.payload, "signature": receipt.signature}, "grant": {"id": grant.id, "payload": grant.payload, "signature": grant.signature}}


def _issue_paid_session(session: Session, ps: PaymentSession, settings: Settings, provider: str):
    q = session.get(Quote, ps.quote_id)
    if not q: raise HTTPException(404, "quote not found")
    try:
        receipt, grant = issue_receipt_and_grant(session, q, ps, settings.signing_secret, settings.grant_ttl_seconds, provider=provider)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    _deliver_agent_action_callback(session, q, ps, settings, provider)
    return receipt, grant


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
    _deliver_agent_action_callback(session, q, ps, settings, "mock")
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


@app.post("/api/v1/payment-sessions/{session_id}/link/poll", response_model=LinkPollResponse)
def poll_link_payment_session(session_id: str, session: Session = Depends(get_session), _credential: BotCredential = Depends(require_operator_credential)):
    ps = session.get(PaymentSession, session_id)
    if not ps:
        raise HTTPException(404, "payment session not found")
    if ps.provider != "link":
        raise HTTPException(409, "payment session provider is not link")
    if not ps.provider_session_id:
        raise HTTPException(409, "payment session has no Link provider_session_id; create a Link spend request before polling")
    try:
        status = retrieve_link_provider_status(ps.provider_session_id)
    except ValueError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    # Fail closed: Link approval, credential creation, unknown values, and even
    # currently parsed settled-ish values do not issue Payjent receipts/grants
    # until a production Link terminal settlement signal is explicitly mapped.
    session.add(ps); session.commit(); session.refresh(ps)
    return LinkPollResponse(
        payment_session=session_to_read(ps),
        normalized_status=status.normalized_status,
        provider_session_id=status.provider_session_id or ps.provider_session_id,
        raw_status=status.raw_status,
        is_settled=status.is_settled,
        settlement_mapping_required=True,
        message="Payjent remains unpaid; Link polling is fail-closed until terminal settlement mapping is enabled.",
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


def _spend_totals(session: Session, grant_id: str) -> tuple[int, int]:
    entries = session.exec(select(SpendLedgerEntry).where(SpendLedgerEntry.grant_id == grant_id)).all()
    total_authorized = sum(e.amount_minor for e in entries if e.status in {"authorized", "captured"})
    total_captured = sum(e.amount_minor for e in entries if e.status == "captured")
    return total_authorized, total_captured


def _spend_to_read(entry: SpendLedgerEntry, session: Session, grant: Grant) -> SpendAuthorizationRead:
    total_authorized, total_captured = _spend_totals(session, grant.id)
    budget = int(grant.payload.get("amount_minor", 0))
    return SpendAuthorizationRead(
        id=entry.id,
        grant_id=entry.grant_id,
        quote_id=entry.quote_id,
        operation_id=entry.operation_id,
        tool=entry.tool,
        vendor=entry.vendor,
        rail=entry.rail,
        amount_minor=entry.amount_minor,
        currency=entry.currency,
        reason=entry.reason,
        status=entry.status,
        provider_reference=entry.provider_reference,
        metadata=entry.metadata_json,
        total_authorized=total_authorized,
        total_captured=total_captured,
        remaining_budget=budget - total_authorized,
    )


@app.post("/api/v1/grants/{grant_id}/spend-authorizations", response_model=SpendAuthorizationRead)
def create_spend_authorization(grant_id: str, payload: SpendAuthorizationCreate, session: Session = Depends(get_session), settings: Settings = Depends(get_settings), credential: BotCredential = Depends(require_bot_credential)):
    grant = _load_valid_grant(grant_id, payload.presentation, session, settings)
    _enforce_bot_scope(credential, grant.payload.get("bot_id"))
    if grant.consumed_at is None:
        raise HTTPException(409, "grant must be consumed before spend authorization")
    existing = session.exec(
        select(SpendLedgerEntry).where(
            SpendLedgerEntry.grant_id == grant.id,
            SpendLedgerEntry.operation_id == payload.operation_id,
        )
    ).first()
    if existing:
        return _spend_to_read(existing, session, grant)
    currency = payload.currency.upper()
    try:
        rail = normalize_spend_rail(payload.rail)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if currency != str(grant.payload.get("currency", "")).upper():
        raise HTTPException(status_code=422, detail="currency mismatch")
    budget = int(grant.payload.get("amount_minor", 0))
    total_authorized, _total_captured = _spend_totals(session, grant.id)
    if total_authorized + payload.amount_minor > budget:
        raise HTTPException(status_code=409, detail="spend exceeds remaining grant budget")
    entry = SpendLedgerEntry(
        id=f"spend_{uuid4().hex}",
        grant_id=grant.id,
        quote_id=grant.quote_id,
        operation_id=payload.operation_id,
        tool=payload.tool,
        vendor=payload.vendor,
        rail=rail,
        amount_minor=payload.amount_minor,
        currency=currency,
        reason=payload.reason,
        status="captured" if payload.capture else "authorized",
        provider_reference=payload.provider_reference,
        metadata_json=payload.metadata,
    )
    session.add(entry)
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        existing = session.exec(
            select(SpendLedgerEntry).where(
                SpendLedgerEntry.grant_id == grant.id,
                SpendLedgerEntry.operation_id == payload.operation_id,
            )
        ).first()
        if existing:
            return _spend_to_read(existing, session, grant)
        raise
    session.refresh(entry)
    return _spend_to_read(entry, session, grant)


@app.post("/api/v1/spend-authorizations/{spend_id}/capture", response_model=SpendAuthorizationRead)
def capture_spend_authorization(spend_id: str, payload: SpendCaptureRequest, session: Session = Depends(get_session), settings: Settings = Depends(get_settings), credential: BotCredential = Depends(require_bot_credential)):
    entry = session.get(SpendLedgerEntry, spend_id)
    if not entry:
        raise HTTPException(404, "spend authorization not found")
    grant = _load_valid_grant(entry.grant_id, payload.presentation, session, settings)
    _enforce_bot_scope(credential, grant.payload.get("bot_id"))
    if grant.consumed_at is None:
        raise HTTPException(409, "grant must be consumed before spend capture")
    if entry.status == "captured":
        return _spend_to_read(entry, session, grant)
    if entry.status != "authorized":
        raise HTTPException(409, "spend authorization is not capturable")
    entry.status = "captured"
    if payload.provider_reference:
        entry.provider_reference = payload.provider_reference
    if payload.metadata:
        entry.metadata_json = {**entry.metadata_json, **payload.metadata}
    session.add(entry); session.commit(); session.refresh(entry)
    return _spend_to_read(entry, session, grant)


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
