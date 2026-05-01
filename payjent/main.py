from contextlib import asynccontextmanager
from datetime import datetime, timezone
from urllib.parse import parse_qs
from uuid import uuid4
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select, update
from .config import Settings, get_settings
from .db import get_session, init_db
from .auth import DASHBOARD_SESSION_COOKIE, create_dashboard_session_cookie, generate_api_key, create_bot_credential, get_account_from_cookie, hash_password, normalize_email, require_bot_credential, require_operator_credential, verify_password
from .models import Account, AgentProfile, BotCredential, RailConnection, Quote, PaymentSession, Grant, FulfillmentEvent, SpendLedgerEntry
from .money import validate_breakdown, quote_hash
from .schemas import (
    AgentRead, AgentRegisterRequest, AgentRegisterResponse, FulfillmentCreate, FulfillmentRead, GrantPresentation, GrantVerifyResponse,
    LinkCredentialApproval, LinkCredentialRequest, LinkPollResponse, MockPayResponse, PaymentSessionRead,
    QuoteCreate, QuoteRead, RailConnectionRead, SpendAuthorizationCreate, SpendAuthorizationRead, SpendCaptureRequest,
    StripeConnectStartResponse, X402ConfigureRequest,
)
from .signing import verify_signature
from .providers.mock import complete_mock_payment
from .providers.base import issue_receipt_and_grant
from .providers.stripe import create_stripe_checkout_session, parse_stripe_event, verify_stripe_signature
from .providers.link import LinkCredentialRequest as LinkProviderCredentialRequest, create_link_spend_request as create_link_provider_spend_request, retrieve_link_status as retrieve_link_provider_status, validate_credential_type
from .risk import assess_checkout_risk
from . import workos_auth

@asynccontextmanager
async def lifespan(_app: FastAPI):
    get_settings().validate_runtime_guardrails()
    if get_session not in _app.dependency_overrides:
        init_db()
    yield


app = FastAPI(title="Payjent", lifespan=lifespan)


@app.get("/")
def root_redirect():
    return RedirectResponse("/dashboard", status_code=303)


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


_DASHBOARD_CSS = """<style>
body{margin:0;font-family:Inter,ui-sans-serif,system-ui;color:#0f172a;background:linear-gradient(135deg,#f8fbff,#fff 35%,#f5f3ff)}a{color:#4f46e5}main{max-width:1180px;margin:auto;padding:32px 20px}.hero{padding:34px;border:1px solid #e5e7eb;border-radius:28px;background:rgba(255,255,255,.82);box-shadow:0 24px 70px #4f46e51a}.eyebrow{color:#6366f1;font-weight:700;text-transform:uppercase;letter-spacing:.12em}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(250px,1fr));gap:18px;margin-top:22px}.card{background:#fff;border:1px solid #e5e7eb;border-radius:20px;padding:20px;box-shadow:0 8px 30px #0f172a0c}.pill{display:inline-flex;border-radius:999px;padding:5px 10px;font-size:12px;font-weight:700;background:#eef2ff;color:#3730a3}.ok{background:#dcfce7;color:#166534}.warn{background:#fef3c7;color:#92400e}pre{overflow:auto;background:#0b1020;color:#dbeafe;border-radius:16px;padding:16px}.muted{color:#64748b}.btn,button{display:inline-block;background:#111827;color:white;text-decoration:none;border:0;border-radius:10px;padding:10px 13px;font-weight:750;cursor:pointer}.topbar{display:flex;justify-content:space-between;align-items:center;margin-bottom:18px}.logout{background:#fff;color:#334155;border:1px solid #e5e7eb}.auth-wrap{min-height:100vh;display:grid;place-items:center;padding:24px}.auth-card{width:min(440px,100%);background:rgba(255,255,255,.9);border:1px solid #e5e7eb;border-radius:24px;padding:30px;box-shadow:0 24px 70px #0f172a14}.error{background:#fef2f2;color:#991b1b;border:1px solid #fecaca;border-radius:12px;padding:10px 12px;margin:12px 0}label{display:block;font-weight:700;margin-top:12px}input{width:100%;box-sizing:border-box;border:1px solid #d1d5db;border-radius:10px;padding:11px;margin:6px 0 12px;background:white}.fine{font-size:13px;color:#64748b;line-height:1.5}@media(max-width:650px){main{padding:18px}.hero{padding:22px}}
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
    return HTMLResponse(f"""<!doctype html><html><head><title>{title}</title>{_DASHBOARD_CSS}</head><body><div class='auth-wrap'><section class='auth-card'><div class='eyebrow'>Payjent dashboard</div><h1>{title}</h1><p class='muted'>Dashboard sessions use HTTP-only signed cookies.</p>{error_html}{workos_html}<form method='post' action='{action}'><label>Email</label><input name='email' type='email' autocomplete='email' required value='{_html_escape(email)}'><label>Password</label><input name='password' type='password' autocomplete='current-password' minlength='8' required><button type='submit'>{'Create account' if is_register else 'Log in'}</button></form><p class='fine'>{alternate}</p><p class='fine'>First-party auth remains available as a fallback. Use a unique password and a production PAYJENT_SIGNING_SECRET.</p></section></div></body></html>""")


def _set_session(response: RedirectResponse, account: Account, settings: Settings) -> RedirectResponse:
    response.set_cookie(DASHBOARD_SESSION_COOKIE, create_dashboard_session_cookie(account.id, settings.signing_secret), httponly=True, samesite="lax", secure=settings.is_production, max_age=60 * 60 * 24 * 7, path="/")
    return response


def _clear_session(response: RedirectResponse) -> RedirectResponse:
    response.delete_cookie(DASHBOARD_SESSION_COOKIE, path="/")
    return response


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
        account = Account(id=f"acct_{uuid4().hex}", email=email, auth_provider="workos", workos_user_id=profile.user_id)
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
    cards = "".join(f"<div class='card'><span class='pill'>{_html_escape(a.platform)}</span><h3>{_html_escape(a.name)}</h3><p class='muted'><code>{_html_escape(a.bot_id)}</code></p><p><a class='btn' href='/dashboard/agents/{_html_escape(a.id)}'>Open setup</a></p></div>" for a in agents) or "<div class='card'><h3>No agents yet</h3><p class='muted'>Register via the operator-authenticated API below.</p></div>"
    register_curl = "curl -X POST /api/v1/agents/register -H 'X-Payjent-Bot-Key: &lt;operator-key&gt;' -H 'Content-Type: application/json' -d '{&quot;name&quot;:&quot;Hermes Research&quot;,&quot;platform&quot;:&quot;discord&quot;,&quot;bot_id&quot;:&quot;hermes-discord&quot;,&quot;default_currency&quot;:&quot;USD&quot;}'"
    return f"<!doctype html><html><head><title>Payjent dashboard</title>{_DASHBOARD_CSS}</head><body><main><div class='topbar'><span class='muted'>Signed in as <b>{_html_escape(account.email)}</b></span><form method='post' action='/auth/logout'><button class='logout' type='submit'>Log out</button></form></div><section class='hero'><div class='eyebrow'>Payjent dashboard v0</div><h1>Agent rail control plane</h1><p class='muted'>Register agents, prepare Stripe Connect user funding, configure x402 downstream spend, and copy safe local integration snippets.</p><div class='grid'><div class='card'><h3>Agent registration</h3><pre><code>{register_curl}</code></pre></div><div class='card'><h3>Ledger stats</h3><p><b>{len(agents)}</b> agents</p><p class='muted'>Recent quote/spend state appears on each agent detail page.</p></div></div></section><h2>Agents</h2><div class='grid'>{cards}</div></main></body></html>"


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
    return f"<!doctype html><html><head><title>{_html_escape(agent.name)} · Payjent</title>{_DASHBOARD_CSS}</head><body><main><div class='topbar'><a href='/dashboard'>← Dashboard</a><form method='post' action='/auth/logout'><button class='logout' type='submit'>Log out</button></form></div><section class='hero'><span class='pill'>{_html_escape(agent.status)}</span><h1>{_html_escape(agent.name)}</h1><p class='muted'>{_html_escape(agent.platform)} · <code>{_html_escape(agent.bot_id)}</code></p></section><div class='grid'>{rail_cards}</div><div class='grid'><div class='card'><h3>Stripe Connect</h3><p class='muted'>Local/test starts return a simulated account link; production fails closed until live OAuth is configured.</p><pre><code>{_html_escape(stripe_cmd)}</code></pre></div><div class='card'><h3>x402 rail configuration</h3><p class='muted'>Stores only non-secret network, pay_to, facilitator URL, and caps.</p><pre><code>{_html_escape(x402_cmd)}</code></pre></div></div><div class='card'><h3>Integration snippet</h3><pre><code>{_html_escape(_integration_snippet(agent))}</code></pre></div><div class='card'><h3>Recent payments / spend ledger</h3><p>{len(quotes)} recent quotes</p><ul>{ledger}</ul></div></main></body></html>"


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
        rail=payload.rail,
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
