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




@app.get("/docs", response_class=HTMLResponse)
def docs_index():
    return HTMLResponse("""<!doctype html><html><head><title>Payjent docs</title><meta name='viewport' content='width=device-width,initial-scale=1'></head><body><main style='font-family:system-ui;max-width:760px;margin:48px auto;padding:0 20px'><h1>Payjent agent setup</h1><p>Agent-readable setup guide for integrating paid action approvals.</p><p><a href='/docs/agent-payjent-self-setup.md'>Open /docs/agent-payjent-self-setup.md</a></p></main></body></html>""")


@app.get("/.well-known/payjent-agent-setup")
def well_known_payjent_agent_setup():
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
    return """<!doctype html><html lang='en'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><title>Payjent — payment gate for agent work</title><meta name='description' content='Payjent is the human-approved checkpoint for paid agent work: quote, approval, single-use grant, and receipt-backed ledger.'><style>
:root{--paper:#fafaf7;--paper2:#f1efe8;--paper3:#e6e3d9;--ink:#0c0c0a;--ink2:#3a3935;--ink3:#74716a;--accent:#1947e5;--ok:#0e7a3b;--warn:#a8731f;--danger:#b51f1f}*{box-sizing:border-box}html{scroll-behavior:smooth}body{margin:0;background:var(--paper);color:var(--ink);font-family:Inter,ui-sans-serif,system-ui,-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;-webkit-font-smoothing:antialiased}a{color:inherit;text-decoration:none}.mono{font-family:'IBM Plex Mono','SFMono-Regular',Menlo,monospace}.container{max-width:1200px;margin:0 auto;padding:0 28px}.nav{position:sticky;top:0;z-index:10;background:var(--paper);border-bottom:1px solid var(--ink)}.nav-row{height:60px;display:flex;align-items:center;gap:28px}.brand{display:flex;align-items:center;gap:10px;font-weight:700;letter-spacing:-.02em}.mark{width:24px;height:24px;border-radius:6px;background:var(--accent);color:#fff;display:grid;place-items:center;font-family:monospace}.navlinks{display:flex;gap:22px;color:var(--ink2);font-size:14px}.nav-cta{margin-left:auto;display:flex;gap:10px}.btn{display:inline-flex;align-items:center;justify-content:center;gap:8px;padding:10px 16px;border:1px solid var(--ink);border-radius:8px;font-weight:600;font-size:14px;background:var(--paper);color:var(--ink)}.btn:hover{background:var(--ink);color:var(--paper)}.btn.accent{background:var(--accent);border-color:var(--accent);color:#fff}.btn.accent:hover{background:var(--ink);border-color:var(--ink)}.btn.ghost{border-color:transparent;color:var(--ink2)}.eyebrow{font:700 11px/1.2 ui-monospace,Menlo,monospace;letter-spacing:.16em;text-transform:uppercase;color:var(--accent)}.kicker{display:inline-flex;align-items:center;gap:8px;font:700 11px/1.2 ui-monospace,Menlo,monospace;text-transform:uppercase;letter-spacing:.12em;color:var(--ink2)}.dot{width:6px;height:6px;border-radius:50%;background:var(--accent)}.hero{padding:64px 0 0;border-bottom:1px solid var(--ink)}.hero-grid{display:grid;grid-template-columns:.9fr 1.1fr;gap:46px;align-items:center;padding-bottom:58px}.hero h1{font-size:clamp(48px,7vw,76px);line-height:.96;letter-spacing:-.055em;margin:18px 0 22px}.hero h1 em,h2 em{font-family:Georgia,serif;font-style:italic;font-weight:400;color:var(--accent)}.hero p{font-size:18px;line-height:1.5;color:var(--ink2);max-width:560px}.hero-cta{display:flex;flex-wrap:wrap;gap:12px;margin-top:28px}.hero-meta{display:flex;flex-wrap:wrap;gap:18px;margin-top:24px;color:var(--ink3);font:12px ui-monospace,Menlo,monospace}.hero-meta span:before{content:'• ';color:var(--accent)}.demo{border:1px solid var(--ink);border-radius:14px;overflow:hidden;height:560px;background:var(--paper);display:grid;grid-template-columns:1fr 1fr;box-shadow:0 22px 48px -26px rgba(12,12,10,.25)}.demo-col{min-width:0;display:flex;flex-direction:column}.demo-col+.demo-col{border-left:1px solid var(--ink)}.demo-hd{display:flex;justify-content:space-between;align-items:center;padding:11px 14px;border-bottom:1px solid var(--ink);background:var(--paper2);font:11px ui-monospace,Menlo,monospace;text-transform:uppercase;letter-spacing:.06em;color:var(--ink2)}.live{color:var(--accent)}.chat{flex:1;overflow:auto;padding:18px;display:flex;flex-direction:column;gap:10px}.msg{max-width:88%;animation:slide .3s ease-out}.msg .who{display:block;margin-bottom:4px;font:10px ui-monospace,Menlo,monospace;text-transform:uppercase;letter-spacing:.1em;color:var(--ink3)}.msg .body{padding:10px 13px;border-radius:10px;border:1px solid var(--paper3);background:#fff;font-size:14px;line-height:1.45}.msg.user{align-self:flex-end;text-align:right}.msg.user .body{background:var(--ink);color:var(--paper);border-color:var(--ink)}.msg.system{align-self:center;text-align:center}.msg.system .body{background:transparent;border-style:dashed;font:11px ui-monospace,Menlo,monospace;color:var(--ink3);border-radius:999px}.approve-card{background:#fff;border:1px solid var(--ink);border-radius:12px;overflow:hidden;text-align:left}.approve-card .ah{padding:9px 12px;background:var(--accent);color:#fff;border-bottom:1px solid var(--ink);display:flex;justify-content:space-between;font:10px ui-monospace,Menlo,monospace;text-transform:uppercase}.approve-card .ab{padding:12px}.approve-card .row{display:flex;justify-content:space-between;gap:10px;padding:4px 0;font:11px ui-monospace,Menlo,monospace;color:var(--ink2)}.approve-card .actions{display:flex;border-top:1px solid var(--paper3)}.approve-card button{flex:1;border:0;background:#fff;padding:10px;font:11px ui-monospace,Menlo,monospace;text-transform:uppercase}.approve-card .yes{background:var(--ink);color:#fff}.ledger{flex:1;display:flex;flex-direction:column;font-family:ui-monospace,Menlo,monospace}.ledger-meta{display:grid;grid-template-columns:repeat(3,1fr);border-bottom:1px solid var(--ink)}.ledger-meta>div{padding:11px 14px}.ledger-meta>div+div{border-left:1px solid var(--paper3)}.lbl{font-size:10px;text-transform:uppercase;letter-spacing:.08em;color:var(--ink3)}.val{font:600 18px Inter,system-ui;color:var(--ink);margin-top:4px}.ledger-list{flex:1;overflow:auto}.ledger-row{display:grid;grid-template-columns:54px 1fr auto;gap:10px;padding:10px 14px;border-bottom:1px solid var(--paper3);align-items:center;animation:slide .35s ease-out}.ts{color:var(--ink3);font-size:10px}.what .a{font-size:12px}.what .b{font-size:10px;color:var(--ink3)}.amt{font-weight:700}.badge{color:var(--accent)}.replay{display:flex;align-items:center;gap:8px;padding:10px 14px;border-top:1px solid var(--ink);background:var(--paper2)}.txt{border:0;background:transparent;font:11px ui-monospace,Menlo,monospace;cursor:pointer}.bar{height:3px;background:var(--paper3);flex:1;transform-origin:left}.bar-fill{height:100%;background:var(--accent);transform-origin:left}.scenarios{display:flex;gap:6px}.sc-dot{width:8px;height:8px;border-radius:50%;border:1px solid var(--ink3);background:transparent}.sc-dot.active{background:var(--accent);border-color:var(--accent)}@keyframes slide{from{opacity:0;transform:translateY(6px)}to{opacity:1;transform:none}}section{padding:82px 0;border-bottom:1px solid var(--paper3)}.sec-head{display:grid;grid-template-columns:1fr 280px;gap:40px;margin-bottom:34px}.sec-head h2{font-size:clamp(36px,5vw,62px);line-height:1;letter-spacing:-.045em;margin:12px 0 0}.meta{font:12px/1.6 ui-monospace,Menlo,monospace;color:var(--ink3);text-transform:uppercase;letter-spacing:.06em}.steps,.cards,.compare-grid{display:grid;grid-template-columns:repeat(4,1fr);border-top:1px solid var(--ink);border-bottom:1px solid var(--ink)}.step,.card2{padding:24px;border-right:1px solid var(--ink);min-height:210px}.step:last-child,.card2:last-child{border-right:0}.num,.tag{font:11px ui-monospace,Menlo,monospace;color:var(--accent);text-transform:uppercase;letter-spacing:.1em}.step h3,.card2 h3{margin:34px 0 10px;font-size:21px;letter-spacing:-.02em}.step p,.card2 p,.int-card p{color:var(--ink2);line-height:1.5}.trust-list{display:grid;grid-template-columns:repeat(3,1fr);gap:1px;background:var(--ink);border:1px solid var(--ink)}.trust-list div{background:#fff;padding:22px}.int-grid{display:grid;grid-template-columns:1fr 1fr;gap:24px}.int-card{border:1px solid var(--ink);border-radius:14px;padding:28px;background:#fff}.int-card h3{font-size:28px}.int-list{font:12px ui-monospace,Menlo,monospace;color:var(--ink3);line-height:1.8}.compare{overflow:auto;border:1px solid var(--ink);background:#fff}table{width:100%;border-collapse:collapse}th,td{padding:15px;border-bottom:1px solid var(--paper3);text-align:left;font-size:14px}th{font:11px ui-monospace,Menlo,monospace;text-transform:uppercase;color:var(--ink3)}.yes{color:var(--accent);font-family:ui-monospace,Menlo,monospace}.no{color:var(--ink3);font-family:ui-monospace,Menlo,monospace}.final{background:var(--ink);color:var(--paper);text-align:center}.final p{color:rgba(250,250,247,.72);max-width:620px;margin:0 auto 24px;line-height:1.5}.final .btn{background:var(--paper);color:var(--ink);border-color:var(--paper)}.btn{transition:background .18s ease,color .18s ease,border-color .18s ease,transform .12s ease,box-shadow .18s ease}.btn:active{transform:translateY(1px) scale(.99)}.btn:focus-visible,.txt:focus-visible,.sc-dot:focus-visible,.approve-card button:focus-visible,a:focus-visible{outline:2px solid var(--accent);outline-offset:3px;box-shadow:0 0 0 5px rgba(25,71,229,.14)}.btn[href$='→']:hover,.btn:hover{gap:11px}.typing-dots{display:inline-flex;gap:3px;margin:0 0 6px 3px;animation:typingGone .55s ease .45s forwards}.typing-dots i{width:4px;height:4px;border-radius:50%;background:var(--ink3);animation:typingDot .7s ease-in-out infinite}.typing-dots i:nth-child(2){animation-delay:.12s}.typing-dots i:nth-child(3){animation-delay:.24s}.approve-card{animation:approvalPulse 1.15s ease-in-out 2}.ledger-row{position:relative;overflow:hidden}.ledger-row:after{content:'';position:absolute;inset:0;background:linear-gradient(90deg,transparent,rgba(25,71,229,.12),transparent);transform:translateX(-100%);animation:ledgerScan .65s ease-out .05s both;pointer-events:none}.grant-stamp{justify-self:end;border:1px solid var(--accent);color:var(--accent);border-radius:999px;padding:2px 6px;font:700 9px ui-monospace,Menlo,monospace;text-transform:uppercase;letter-spacing:.08em;transform:rotate(-4deg) scale(.92);opacity:0;animation:grantStamp .42s cubic-bezier(.2,.8,.2,1) .18s forwards}.steps{position:relative}.steps:before{content:'';position:absolute;left:0;right:0;top:64px;border-top:1px dotted rgba(25,71,229,.55);transform:scaleX(0);transform-origin:left}.steps.is-visible:before{animation:flowLine 1s ease-out forwards}.steps.is-visible .step .num{animation:primitivePin .38s ease-out both}.steps.is-visible .step:nth-child(2) .num{animation-delay:.22s}.steps.is-visible .step:nth-child(3) .num{animation-delay:.44s}.steps.is-visible .step:nth-child(4) .num{animation-delay:.66s}.compare.is-visible tbody td:nth-child(2) .yes{display:inline-block;animation:nativePulse .55s ease-out both}.compare.is-visible tbody tr:nth-child(2) td:nth-child(2) .yes{animation-delay:.12s}.compare.is-visible tbody tr:nth-child(3) td:nth-child(2) .yes{animation-delay:.24s}.compare.is-visible tbody tr:nth-child(4) td:nth-child(2) .yes{animation-delay:.36s}@keyframes typingDot{0%,80%,100%{opacity:.35;transform:translateY(0)}40%{opacity:1;transform:translateY(-2px)}}@keyframes typingGone{to{opacity:0;height:0;margin:0;overflow:hidden}}@keyframes approvalPulse{0%,100%{box-shadow:0 0 0 0 rgba(25,71,229,0)}50%{box-shadow:0 0 0 4px rgba(25,71,229,.13)}}@keyframes ledgerScan{to{transform:translateX(100%)}}@keyframes grantStamp{to{opacity:1;transform:rotate(-4deg) scale(1)}}@keyframes flowLine{to{transform:scaleX(1)}}@keyframes primitivePin{50%{color:#fff;background:var(--accent);box-shadow:0 0 0 5px rgba(25,71,229,.12)}}@keyframes nativePulse{50%{text-shadow:0 0 14px rgba(25,71,229,.35);transform:translateY(-1px)}}@media(prefers-reduced-motion:reduce){html{scroll-behavior:auto}*,*:before,*:after{animation:none!important;transition:none!important;transform:none!important}.steps:before{transform:scaleX(1)}}@media(max-width:900px){.navlinks{display:none}.hero-grid,.sec-head,.int-grid{grid-template-columns:1fr}.demo{height:auto;min-height:640px;grid-template-columns:1fr}.demo-col+.demo-col{border-left:0;border-top:1px solid var(--ink)}.steps,.cards,.trust-list{grid-template-columns:1fr}.step,.card2{border-right:0;border-bottom:1px solid var(--ink)}}
</style></head><body><nav class='nav'><div class='container nav-row'><a class='brand' href='/'><span class='mark'>P</span><span>payjent</span></a><div class='navlinks'><a href='#how'>How</a><a href='#integrate'>Integrate</a><a href='#trust'>Trust</a><a href='#compare'>Compare</a><a href='/dashboard'>Dashboard</a></div><div class='nav-cta'><a class='btn ghost' href='/dashboard'>Sign in</a><a class='btn accent' href='__PRIMARY__'>Register your agent →</a></div></div></nav><main><header class='hero'><div class='container hero-grid'><div><span class='kicker'><span class='dot'></span>Payment-gate agent actions · Human-in-the-loop payments for agents</span><h1>The <em>payment gate</em> for agent work.</h1><p>Payjent lets your agent ask a person to approve and pay for premium work at the moment money is needed, then resume the exact action with a single-use grant and a receipt-backed ledger.</p><div class='hero-cta'><a class='btn accent' href='__PRIMARY__'>Register your agent →</a><a class='btn' href='/docs/agent-payjent-self-setup.md'>Send/read setup guide</a></div><div class='hero-meta'><span>Request-bound grants</span><span>Human approval before resume</span><span>Rail-aware: Stripe, x402, custom</span></div></div><div class='demo' aria-label='Live Payjent chat and ledger demo'><div class='demo-col'><div class='demo-hd'><span id='demo-agent'>Atlas / research · @erik</span><span class='live'>● Live</span></div><div class='chat' id='demo-chat'></div></div><div class='demo-col'><div class='demo-hd'><span id='demo-ledger-title'>Live ledger · research.payjent</span><span id='demo-total'>$0.00</span></div><div class='ledger'><div class='ledger-meta'><div><div class='lbl'>Grants</div><div class='val' id='demo-grants'>0</div></div><div><div class='lbl'>Captured</div><div class='val' id='demo-captured'>$0.00</div></div><div><div class='lbl'>Pending</div><div class='val' id='demo-pending'>—</div></div></div><div class='ledger-list' id='demo-ledger'><div style='padding:24px 14px;color:var(--ink3);font-size:11px;text-transform:uppercase;letter-spacing:.06em'>Awaiting first event…</div></div><div class='replay'><button class='txt' id='pause-demo'>❚❚ Pause</button><button class='txt' id='replay-demo'>↺ Replay</button><div class='bar'><div class='bar-fill' id='demo-progress'></div></div><div class='scenarios'><button class='sc-dot active' data-s='0'></button><button class='sc-dot' data-s='1'></button><button class='sc-dot' data-s='2'></button></div></div></div></div></div></div></header><section id='how' data-animate='flow'><div class='container'><div class='sec-head'><div><span class='eyebrow'>How it works</span><h2>Quote, <em>approve</em>, grant, capture.</h2></div><div class='meta'>Four primitives. One pause. The original action resumes only after approval.</div></div></div><div class='steps'><div class='step'><span class='num'>01</span><h3>Agent quotes the work</h3><p>The agent records the paid action, amount, vendor, reason, and execution envelope before any spend happens.</p></div><div class='step'><span class='num'>02</span><h3>Human approves once</h3><p>Payjent creates a checkout/approval checkpoint tied to that exact request and amount.</p></div><div class='step'><span class='num'>03</span><h3>Single-use grant issued</h3><p>After payment, a signed grant unlocks the original action once. Replay and re-scope fail closed.</p></div><div class='step'><span class='num'>04</span><h3>Receipt lands in the ledger</h3><p>The agent resumes; Payjent records quote, approval, grant, fulfillment, and spend context.</p></div></div></section><section id='integrate'><div class='container'><div class='sec-head'><div><span class='eyebrow'>Integrate</span><h2>Ask your agent to <em>wire itself up.</em></h2></div><div class='meta'>No code blocks on the landing page. The setup contract is agent-readable.</div></div><div class='int-grid'><div class='int-card'><span class='num'>01</span><h3>Send the setup guide</h3><p>Point your coding agent at this site's Payjent setup guide. It can read the contract and implement quote, checkout, status, and resume behavior in your app.</p><p><a class='btn accent' href='/docs/agent-payjent-self-setup.md'>Send/read setup guide →</a></p><ul class='int-list'><li>Machine-readable Markdown</li><li>Works with any agent framework</li><li>Uses real Payjent API routes</li></ul></div><div class='int-card'><span class='num'>02</span><h3>Register in the dashboard</h3><p>Mint one credential for one agent identity, store it privately, and let every paid action flow through human approval and the ledger.</p><p><a class='btn' href='__PRIMARY__'>Register agent in dashboard →</a></p><ul class='int-list'><li>Credential shown once</li><li>No secrets exposed in ordinary dashboard pages</li><li>Rail configuration stays operator-controlled</li></ul></div></div></div></section><section id='wedge'><div class='container'><div class='sec-head'><div><span class='eyebrow'>Why Payjent</span><h2>The wedge is <em>where money meets intent.</em></h2></div><div class='meta'>Built for solo devs, founders, and CTOs shipping agents that sometimes need paid work.</div></div><div class='cards'><div class='card2'><span class='tag'>Wedge 01</span><h3>The checkpoint agents never had</h3><p>Payjent treats payment as a deliberate human checkpoint, not a background budget.</p></div><div class='card2'><span class='tag'>Wedge 02</span><h3>Premium work without credential sprawl</h3><p>One Payjent agent credential gates paid tools and rails while downstream rail details remain controlled by the operator.</p></div><div class='card2'><span class='tag'>Wedge 03</span><h3>Receipts with reasons</h3><p>Ledger entries keep the prompt, quote, approver, grant, and outcome keyed together for auditability.</p></div><div class='card2'><span class='tag'>Wedge 04</span><h3>Rail-aware, not rail-confused</h3><p>Payjent can sit around Stripe, x402, pay.sh-style flows, or custom rails; Payjent does not execute downstream pay.sh itself — it gates, records, and resumes the approved action.</p></div></div></div></section><section id='trust'><div class='container'><div class='sec-head'><div><span class='eyebrow'>Trust & safety</span><h2>Designed so <em>nothing surprising</em> leaves your account.</h2></div><div class='meta'>Conservative defaults. Fail-closed checks. Copy-once credentials.</div></div><div class='trust-list'><div><b>Request-bound grants</b><p>Approval is tied to the exact action envelope and request hash.</p></div><div><b>Human approval required</b><p>The agent waits at checkout before paid work resumes.</p></div><div><b>Credential shown once</b><p>Agent credentials are not revealed again on ordinary dashboard pages.</p></div><div><b>Risk-checked checkout</b><p>Checkout creation runs policy risk checks and fails closed when disallowed.</p></div><div><b>Per-agent identity</b><p>Every credential maps to a stable agent bot_id and owner.</p></div><div><b>Reason-backed ledger</b><p>Spend events preserve why the agent asked and what happened next.</p></div></div></div></section><section id='compare' data-animate='native'><div class='container'><div class='sec-head'><div><span class='eyebrow'>Compare</span><h2>Same problem, <em>different shape.</em></h2></div><div class='meta'>Stripe, x402, pay.sh and DIY primitives can be useful. Payjent is the human approval gate around them.</div></div><div class='compare'><table><thead><tr><th>Capability</th><th>Payjent</th><th>Stripe</th><th>x402/buildx402</th><th>pay.sh</th><th>DIY</th></tr></thead><tbody><tr><td>Human approval at payment time</td><td><span class='yes'>● native</span></td><td><span class='no'>○ build it</span></td><td><span class='no'>○ not the focus</span></td><td><span class='no'>○ external</span></td><td><span class='no'>○ build it</span></td></tr><tr><td>Request-bound single-use grant</td><td><span class='yes'>● native</span></td><td><span class='no'>○ custom</span></td><td><span class='no'>○ custom</span></td><td><span class='no'>○ custom</span></td><td><span class='no'>○ build it</span></td></tr><tr><td>Reason-backed agent ledger</td><td><span class='yes'>● native</span></td><td><span class='no'>○ payments only</span></td><td><span class='no'>○ protocol only</span></td><td><span class='no'>○ rail/tool</span></td><td><span class='no'>○ build it</span></td></tr><tr><td>Rail options</td><td><span class='yes'>● rail-aware</span></td><td><span class='no'>○ Stripe</span></td><td><span class='no'>○ x402</span></td><td><span class='no'>○ pay.sh</span></td><td><span class='no'>○ depends</span></td></tr></tbody></table></div></div></section><section id='register' class='final'><div class='container'><span class='eyebrow'>Ready to ship</span><h2>Register an <em>agent.</em></h2><p>Two minutes. One credential. Shown exactly once. Drop it into your agent's secret store and Payjent gates the next paid action.</p><div class='hero-cta' style='justify-content:center'><a class='btn' href='__PRIMARY__'>Register your agent →</a><a class='btn ghost' href='/docs/agent-payjent-self-setup.md'>Read setup guide</a></div></div></section></main><script>
const scenarios=[{id:'research',agent:'Atlas / research',user:'@erik',steps:[['msg','user','Find me the 2025 IEA energy outlook PDF and summarize chapter 4.'],['msg','agent','Found it on the IEA store. It is a paywalled PDF — $12.50. I will request payment approval before purchase.'],['ledger','quote','iea.org · world-energy-outlook-2025.pdf','$12.50','pending','QUOTE'],['msg','system','Payjent approval requested · waiting on @erik'],['approve','iea.org','World Energy Outlook 2025 (PDF)','$12.50','$50.00 / day'],['ledger','grant','single-use grant · request-bound','$12.50','captured','APPROVED'],['msg','user','approved'],['msg','agent','Paid. Downloading now…'],['ledger','capture','iea.org · receipt #ec5b','$12.50','captured','PAID'],['msg','agent','Done. Chapter 4 covers grid-scale storage; here is the summary…']]},{id:'travel',agent:'Lark / travel',user:'@erik',steps:[['msg','user','Book the 7am SFO→JFK on Friday, aisle seat under $480.'],['msg','agent','Held UA 286: $462, 14C. Asking you to approve before I confirm.'],['ledger','quote','united.com · UA 286 SFO→JFK','$462.00','pending','HOLD'],['msg','system','Payjent approval requested · spend cap $1,500 / wk'],['approve','united.com','UA 286 · 1× economy · 14C','$462.00','$1,500 / wk'],['ledger','grant','single-use grant · request-bound','$462.00','captured','APPROVED'],['msg','user','go'],['msg','agent','Confirmed. Conf code XQ7P2K. Itinerary in your inbox.'],['ledger','capture','united.com · ticket XQ7P2K','$462.00','captured','PAID']]},{id:'credits',agent:'Hex / dev',user:'@erik',steps:[['msg','user','Run the full eval suite against the new model.'],['msg','agent','That will cost about $28 in OpenAI credits and $4.20 in Anthropic. Bundling both via Payjent.'],['ledger','quote','openai.com · credits','$28.00','pending','QUOTE'],['ledger','quote','anthropic.com · credits','$4.20','pending','QUOTE'],['msg','system','Payjent approval requested · 2 line items · $32.20 total'],['approve','2 vendors · bundled','OpenAI + Anthropic credits','$32.20','$200 / day'],['ledger','grant','single-use grant · bundled','$32.20','captured','APPROVED'],['msg','user','yep'],['msg','agent','Eval running. ETA 6 minutes.'],['ledger','capture','openai.com · receipt','$28.00','captured','PAID'],['ledger','capture','anthropic.com · receipt','$4.20','captured','PAID']]}];let si=0,idx=0,paused=false,timer;function money(s){return parseFloat(String(s).replace(/[^0-9.]/g,''))||0}function render(){const sc=scenarios[si];document.getElementById('demo-agent').textContent=sc.agent+' · '+sc.user;document.getElementById('demo-ledger-title').textContent='Live ledger · '+sc.id+'.payjent';const chat=document.getElementById('demo-chat'),led=document.getElementById('demo-ledger');if(idx===0){chat.innerHTML='';led.innerHTML=''}for(;idx<=Math.min(window.stepN||0,sc.steps.length-1);idx++){const st=sc.steps[idx];if(st[0]==='msg'){const dots=st[1]==='agent'?`<span class='typing-dots' aria-hidden='true'><i></i><i></i><i></i></span>`:'';chat.insertAdjacentHTML('beforeend',`<div class='msg ${st[1]}'>${dots}<span class='who'>${st[1]==='user'?sc.user:st[1]==='agent'?sc.agent:'system'}</span><div class='body'>${st[2]}</div></div>`)}else if(st[0]==='approve'){chat.insertAdjacentHTML('beforeend',`<div class='msg approval'><div class='approve-card'><div class='ah'><span>Payjent · approve payment</span><span>req_8af2</span></div><div class='ab'><div class='row'><span>Merchant</span><b>${st[1]}</b></div><div class='row'><span>Item</span><b>${st[2]}</b></div><div class='row'><span>Amount</span><b>${st[3]}</b></div><div class='row'><span>Within cap</span><b>${st[4]}</b></div></div><div class='actions'><button>Decline</button><button class='yes'>Approve once</button></div></div></div>`)}else{led.insertAdjacentHTML('beforeend',`<div class='ledger-row ${st[4]}'><div class='ts'>00:0${idx}</div><div class='what'><div class='a'>${st[2]}</div><div class='b'>${st[1]} · <span class='badge'>${st[5]}</span></div></div><div class='amt'>${st[3]}</div>${st[1]==='grant'?`<span class='grant-stamp'>single-use</span>`:''}</div>`)}chat.scrollTop=chat.scrollHeight}const visible=sc.steps.slice(0,idx);const ledger=visible.filter(x=>x[0]==='ledger');const grants=ledger.filter(x=>x[1]==='grant').length;const captured=ledger.filter(x=>x[1]==='capture').reduce((a,x)=>a+money(x[3]),0);const pending=ledger.filter(x=>x[1]==='quote'&&!ledger.some(y=>y[1]==='capture')).reduce((a,x)=>a+money(x[3]),0);document.getElementById('demo-grants').textContent=grants;document.getElementById('demo-captured').textContent='$'+captured.toFixed(2);document.getElementById('demo-pending').textContent=pending?'$'+pending.toFixed(2):'—';document.getElementById('demo-total').textContent='$'+(captured+pending).toFixed(2);document.getElementById('demo-progress').style.transform='scaleX('+Math.min(1,idx/sc.steps.length)+')'}function reset(n){si=n;idx=0;window.stepN=0;document.querySelectorAll('.sc-dot').forEach((b,i)=>b.classList.toggle('active',i===si));render()}function tick(){if(!paused){window.stepN=(window.stepN||0)+1;render();if(window.stepN>=scenarios[si].steps.length){setTimeout(()=>reset((si+1)%scenarios.length),1200)}}timer=setTimeout(tick,850)}document.getElementById('pause-demo').onclick=()=>{paused=!paused;document.getElementById('pause-demo').textContent=paused?'▶ Play':'❚❚ Pause'};document.getElementById('replay-demo').onclick=()=>reset(si);document.querySelectorAll('.sc-dot').forEach(b=>b.onclick=()=>reset(Number(b.dataset.s)));const motionOK=!window.matchMedia('(prefers-reduced-motion: reduce)').matches;if(motionOK&&'IntersectionObserver'in window){const once=new IntersectionObserver((entries)=>{entries.forEach(e=>{if(!e.isIntersecting)return;const el=e.target.id==='how'?e.target.querySelector('.steps'):e.target.querySelector('.compare');if(el)el.classList.add('is-visible');once.unobserve(e.target)})},{threshold:.35});document.querySelectorAll('[data-animate]').forEach(el=>once.observe(el))}else{document.querySelectorAll('.steps,.compare').forEach(el=>el.classList.add('is-visible'))};reset(0);tick();</script></body></html>""".replace("__PRIMARY__", primary)

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
    ps, q, grant, fulfillment = _find_session_bundle(session, payment_session_id)
    breakdown = "".join(
        f"<li>{_html_escape(item.get('label', 'item'))}: {_html_escape(_format_money(int(item.get('amount_minor', 0)), q.currency))}</li>"
        for item in q.cost_breakdown
    ) or "<li>No line-item breakdown supplied.</li>"
    status_words = "Paid — one-time grant issued; agent will resume automatically" if grant else "Waiting for human approval and payment"
    resumes = _html_escape(q.execution_envelope.get("description") or q.execution_envelope.get("command_preview") or q.request_summary)
    mock_form = ""
    if settings.effective_mock_provider_enabled and ps.status != "paid":
        mock_form = f"""<section><h2>Dev mock payment</h2><p>This page does not reveal operator credentials. In local development, complete payment through the authenticated mock-pay API with an operator key kept outside the browser.</p><pre><code>curl -X POST http://localhost:8000/api/v1/payment-sessions/{_html_escape(ps.id)}/mock-pay \
  -H 'X-Payjent-Bot-Key: &lt;operator-key&gt;'</code></pre></section>"""
    return f"""<!doctype html><html><head><title>Payjent checkout · Approve paid agent action</title>{_DASHBOARD_CSS}</head><body><main><section class='hero'><div class='eyebrow'>Human approval document</div><h1>Approve this exact paid action?</h1><p class='muted'>Key question: should this agent resume this exact paid action after payment?</p></section><div class='grid'><div class='card'><h3>Agent request</h3><p>{_html_escape(q.request_summary)}</p><p class='fine'>External user: <code>{_html_escape(q.external_user_id)}</code><br>Request hash: <code>{_html_escape(q.request_hash)}</code></p></div><div class='card'><h3>Amount</h3><div class='stat'>{_html_escape(_format_money(q.amount_minor, q.currency))}</div><ul>{breakdown}</ul></div><div class='card'><h3>Status</h3><p><b>{_html_escape(status_words)}</b></p><p class='fine'>Payment session: <code>{_html_escape(ps.id)}</code><br>Payment provider/status: {_html_escape(ps.provider)} / {_html_escape(ps.status)}<br>Grant state: {_html_escape(_grant_state(grant))}</p></div><div class='card'><h3>What resumes after payment</h3><p>{resumes}</p><p class='fine'>Approval creates a one-time grant bound to this stored request. Raw grant and payment tokens are not shown on this page.</p></div></div><section><h2>Approval terms</h2><ul><li>Human approval is required before Payjent marks this action ready.</li><li>The grant is single-use and tied to the exact request hash above.</li><li>Downstream rails may still impose their own authorization, settlement, availability, or rejection behavior; Payjent records the checkpoint and does not guarantee a third-party rail outcome.</li><li>Fulfillment events recorded so far: {len(fulfillment)}.</li></ul><p><a class='btn' href="/status/{_html_escape(ps.id)}">View status</a></p></section>{mock_form}</main></body></html>"""


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


_DASHBOARD_CSS = """<style>
:root{--paper:#fafaf7;--paper2:#f1efe8;--paper3:#e6e3d9;--ink:#0c0c0a;--ink2:#3a3935;--ink3:#74716a;--accent:#1947e5;--ok:#0e7a3b;--warn:#a8731f;--danger:#b51f1f}*{box-sizing:border-box}body{margin:0;min-height:100vh;background:var(--paper);color:var(--ink);font-family:Inter,ui-sans-serif,system-ui,-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;-webkit-font-smoothing:antialiased}a{color:var(--accent);text-decoration:none}main{max-width:1280px;margin:0 auto;padding:0 28px 84px}.mono{font-family:ui-monospace,SFMono-Regular,Menlo,monospace}.topbar{height:60px;display:flex;align-items:center;gap:18px;border-bottom:1px solid var(--ink);margin:0 -28px 36px;padding:0 28px}.topbar form{margin-left:auto}.brand{display:flex;align-items:center;gap:10px;color:var(--ink);font-weight:700}.brand-mark{width:24px;height:24px;border-radius:6px;background:var(--accent);color:#fff;display:grid;place-items:center;font-family:monospace}.logout{border:0;background:transparent;color:var(--ink2);font:600 14px inherit;cursor:pointer}.logout:hover{color:var(--accent)}.hero{padding:0 0 30px;border-bottom:1px solid var(--paper3);margin-bottom:28px}.eyebrow{display:block;font:700 11px/1.2 ui-monospace,Menlo,monospace;letter-spacing:.16em;text-transform:uppercase;color:var(--accent);margin-bottom:12px}h1{font-size:clamp(42px,6vw,70px);line-height:.96;letter-spacing:-.055em;margin:0 0 14px}h1 em,h2 em{font-family:Georgia,serif;font-style:italic;font-weight:400;color:var(--accent)}h2{font-size:clamp(32px,4vw,46px);line-height:1;letter-spacing:-.045em;margin:0 0 10px}h3{font-size:18px;letter-spacing:-.02em;margin:0}.muted,p{color:var(--ink2);line-height:1.5}.fine{font:12px/1.5 ui-monospace,Menlo,monospace;color:var(--ink3)}.btn,button[type=submit]{display:inline-flex;align-items:center;justify-content:center;gap:8px;padding:10px 15px;border:1px solid var(--ink);border-radius:8px;background:var(--paper);color:var(--ink);font-weight:700;cursor:pointer}.btn:hover,button[type=submit]:hover{background:var(--ink);color:var(--paper);text-decoration:none}.btn.accent,button[type=submit]{background:var(--accent);border-color:var(--accent);color:#fff}.btn.dark{background:var(--ink);color:var(--paper)}.cta-banner{margin-top:28px;border:1px solid var(--ink);border-radius:14px;background:var(--ink);color:var(--paper);display:grid;grid-template-columns:1.45fr .9fr;overflow:hidden}.cta-banner>div:first-child{padding:30px 34px}.cta-banner p{color:rgba(250,250,247,.74);max-width:680px}.cta-banner .eyebrow{color:#9bb5ff}.runbook{background:#080806;border-left:1px solid #333;padding:24px;color:#cfcdc4;font-size:12px;line-height:1.9}.runbook b{color:#9bb5ff;margin-right:10px}.kpi-row{display:grid;grid-template-columns:repeat(4,1fr);border:1px solid var(--paper3);border-radius:12px;background:#fff;overflow:hidden;margin:28px 0}.kpi{padding:20px;border-left:1px solid var(--paper3)}.kpi:first-child{border-left:0}.lbl,.sub{font:11px ui-monospace,Menlo,monospace;text-transform:uppercase;letter-spacing:.08em;color:var(--ink3)}.stat{font-size:32px;font-weight:700;letter-spacing:-.03em;margin:8px 0;color:var(--ink)}.stat.small{font-size:20px;line-height:1.2}.dash-layout{display:grid;grid-template-columns:1.6fr .9fr;gap:24px}.panel{background:#fff;border:1px solid var(--paper3);border-radius:12px;overflow:hidden}.panel.wide{grid-column:1/-1}.ph{display:flex;align-items:center;justify-content:space-between;gap:12px;padding:14px 18px;background:var(--paper2);border-bottom:1px solid var(--paper3)}.pb{padding:18px}.pb.flat{padding:0}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:16px}.grid.compact .card{border:1px solid var(--paper3);border-radius:10px;padding:16px}.card{background:#fff}.pill{display:inline-flex;color:var(--accent);font:11px ui-monospace,Menlo,monospace;text-transform:uppercase;letter-spacing:.08em;margin-bottom:8px}.pill.ok{color:var(--ok)}.pill.warn{color:var(--warn)}.event{padding:14px 18px;border-bottom:1px solid var(--paper3);font-size:14px}.event:last-child{border-bottom:0}form{display:grid;gap:11px;margin-top:14px}label{font:700 11px ui-monospace,Menlo,monospace;text-transform:uppercase;letter-spacing:.08em;color:var(--ink2)}input,select{width:100%;padding:11px 12px;border:1px solid var(--paper3);border-radius:8px;background:var(--paper);font:14px inherit;color:var(--ink)}input:focus{outline:2px solid rgba(25,71,229,.18);border-color:var(--accent)}pre{white-space:pre-wrap;overflow:auto;background:var(--paper2);border:1px solid var(--paper3);border-radius:10px;padding:14px}code{font-family:ui-monospace,Menlo,monospace;background:var(--paper2);padding:.1rem .25rem;border-radius:4px}table{width:100%;border-collapse:collapse}th,td{padding:12px;border-bottom:1px solid var(--paper3);text-align:left;vertical-align:top;font-size:13px}th{font:11px ui-monospace,Menlo,monospace;text-transform:uppercase;color:var(--ink3)}.auth-wrap{min-height:100vh;display:grid;place-items:center;padding:28px}.auth-card{max-width:560px;background:#fff;border:1px solid var(--paper3);border-radius:14px;padding:30px}.error,.warnbox{border:1px solid var(--danger);color:var(--danger);background:#fff;padding:12px;border-radius:8px}@media(max-width:900px){main{padding:0 18px 60px}.topbar{margin:0 -18px 28px;padding:0 18px}.cta-banner,.dash-layout,.kpi-row{grid-template-columns:1fr}.runbook{border-left:0;border-top:1px solid #333}.kpi{border-left:0;border-top:1px solid var(--paper3)}}
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



def _policy_defaults_html(x402_caps: str | None = None) -> str:
    cap_line = f"<li>x402 cap guidance from configured rail: {x402_caps}</li>" if x402_caps else "<li>x402 caps are rail configuration guidance when present; they do not replace human approval for Payjent paid actions.</li>"
    return f"""<ul>
<li>Exact request binding: approval is tied to the stored request hash and execution envelope.</li>
<li>Human approval required: the agent waits at checkout before paid work resumes.</li>
<li>Single-use grant: paid grants unlock the original action once, then consumption is recorded.</li>
{cap_line}
<li>Blocked risk policy: checkout creation calls <code>assess_checkout_risk</code> and fails closed when the request is disallowed.</li>
</ul><p class='fine'>Policy controls MVP: these defaults are enforced or communicated by current Payjent code and are not editable yet.</p>"""

def _grant_state(grant: Grant | None) -> str:
    if not grant:
        return "unissued"
    return "consumed" if grant.consumed_at else "available"

def _lifecycle_rows(session: Session, quotes: list[Quote]) -> str:
    rows = []
    for q in quotes:
        ps = session.exec(select(PaymentSession).where(PaymentSession.quote_id == q.id).order_by(PaymentSession.created_at.desc())).first()
        grants = session.exec(select(Grant).where(Grant.quote_id == q.id).order_by(Grant.created_at.desc())).all()
        grant = grants[0] if grants else None
        fulfills = session.exec(select(FulfillmentEvent).where(FulfillmentEvent.quote_id == q.id).order_by(FulfillmentEvent.created_at.desc())).all()
        spends = session.exec(select(SpendLedgerEntry).where(SpendLedgerEntry.quote_id == q.id).order_by(SpendLedgerEntry.created_at.desc())).all()
        fulfill_status = f"{len(fulfills)} / {fulfills[0].status}" if fulfills else "0 / not reported"
        spend_status = ", ".join(f"{s.status} { _format_money(s.amount_minor, s.currency)}" for s in spends[:2]) if spends else "none"
        rows.append(f"<tr><td><code>{_html_escape(q.id)}</code><br><span class='muted'>{_html_escape(q.request_summary)}</span></td><td>{_html_escape(q.status)}</td><td>{_html_escape(ps.provider if ps else 'none')}<br><span class='muted'>{_html_escape(ps.status if ps else 'no session')}</span></td><td>{_html_escape(_grant_state(grant))}</td><td>{_html_escape(fulfill_status)}</td><td>{_html_escape(spend_status)}</td></tr>")
    return "".join(rows) or "<tr><td colspan='6'>No paid actions yet.</td></tr>"

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
    return f"""<!doctype html><html><head><title>Payjent dashboard</title>{_DASHBOARD_CSS}</head><body><main><div class='topbar'><a class='brand' href='/'><span class='brand-mark'>P</span><span>payjent</span></a><span class='muted'>Signed in as <b>{_html_escape(account.email)}</b></span><form method='post' action='/auth/logout'><button class='logout' type='submit'>Log out</button></form></div><section class='hero'><div class='eyebrow'>Payment operations</div><h1>Agent <em>command</em> center</h1><p class='muted'>Register agents, issue copy-once credentials, watch paid action requests, and keep reasoning-backed spend records without exposing raw bot keys or payment tokens.</p><div class='cta-banner'><div><span class='eyebrow'>Primary action</span><h2>Register an <em>agent.</em></h2><p>Mint one credential bound to one agent identity. It is shown exactly once, then Payjent gates the agent's next paid action.</p><p><a class='btn accent' href='#register-agent'>Register your agent →</a><a class='btn dark' href='/docs/agent-payjent-self-setup.md'>Read setup guide</a></p></div><div class='runbook mono'><div><b>1</b> Sign in to dashboard</div><div><b>2</b> Register stable bot_id and Copy one-time credential</div><div><b>3</b> Copy credential once</div><div><b>4</b> Send setup guide to agent</div><div><b>5</b> Configure Stripe Connect, x402, and integration snippets on agent detail</div><div><b>6</b> Confirm approval gate resumes exact action</div></div></div></section><div class='kpi-row'><div class='kpi'><div class='lbl'>Agents</div><div class='stat'>{len(agents)}</div><p class='fine'>Registered identities</p></div><div class='kpi'><div class='lbl'>Paid action volume</div><div class='stat small'>{_format_money_totals(paid_totals)}</div><p class='fine'>Grouped by currency from recent paid / fulfilled quotes.</p></div><div class='kpi'><div class='lbl'>Downstream spend</div><div class='stat small'>{_format_money_totals(spend_totals)}</div><p class='fine'>Authorized or captured ledger</p></div><div class='kpi'><div class='lbl'>Recent requests</div><div class='stat'>{len(quotes)}</div><p class='fine'>Latest action quotes</p></div></div><div class='dash-layout'><section class='panel'><div class='ph'><h3>Agent-owner quickstart</h3><span class='sub'>latest quotes</span></div><div class='pb flat'>{interactions}</div></section><aside class='panel' id='register-agent'><div class='ph'><h3>Register agent</h3><span class='sub'>credential shown once</span></div><div class='pb'><p class='muted'>Use this authenticated form. No operator API key or curl is needed in the browser. Store the copy-once value as <code>X-Payjent-Bot-Key</code>.</p>{register_form}<p class='fine'>After submit, copy the generated Payjent credential immediately. Do not paste it into chat.</p></div></aside><section class='panel'><div class='ph'><h3>Registered agents</h3><span class='sub'>{len(agents)} total</span></div><div class='pb grid compact'>{cards}</div></section><aside class='panel'><div class='ph'><h3>Policy defaults</h3><span class='sub'>workspace</span></div><div class='pb'>{_policy_defaults_html()}</div></aside><section class='panel'><div class='ph'><h3>Spend reasoning trail</h3><span class='sub'>reason → vendor → amount</span></div><div class='pb flat'>{spend_events}</div></section><section class='panel wide'><div class='ph'><h3>Paid-action lifecycle ledger</h3><span class='sub'>quote → payment → grant → fulfillment</span></div><div class='pb'><table><thead><tr><th>Action</th><th>Quote</th><th>Payment</th><th>Grant</th><th>Fulfillment</th><th>Spend</th></tr></thead><tbody>{_lifecycle_rows(session, quotes)}</tbody></table></div></section></div></main></body></html>"""



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
    x402_caps = None
    for rail in rails:
        if rail.rail == 'x402':
            cfg = rail.config_json or {}
            max_request = cfg.get('max_per_request_minor')
            max_call = cfg.get('max_per_call_minor')
            if max_request is not None or max_call is not None:
                x402_caps = f"max/request {max_request if max_request is not None else 'unset'} minor units; max/call {max_call if max_call is not None else 'unset'} minor units"
    lifecycle = _lifecycle_rows(session, quotes)
    stripe_cmd = f"curl -X POST /api/v1/agents/{agent.id}/stripe-connect/start -H 'X-Payjent-Bot-Key: &lt;operator-key&gt;'"
    x402_cmd = f"curl -X POST /api/v1/agents/{agent.id}/x402/configure -H 'X-Payjent-Bot-Key: &lt;operator-key&gt;' -H 'Content-Type: application/json' -d '{{\"network\":\"base-sepolia\",\"pay_to\":\"0xTEST_PAY_TO\",\"max_per_request_minor\":900,\"max_per_call_minor\":250,\"enabled\":true}}'"
    credential_form = f"""<form method='post' action='/dashboard/agents/{_html_escape(agent.id)}/credentials'><p class='muted'>Credentials are copy-once. Existing values cannot be revealed. Create another credential only if the agent needs a fresh private key; store it as <code>X-Payjent-Bot-Key</code>.</p><button type='submit'>Create another credential</button></form>"""
    return f"<!doctype html><html><head><title>{_html_escape(agent.name)} · Payjent</title>{_DASHBOARD_CSS}</head><body><main><div class='topbar'><a href='/dashboard'>← Dashboard</a><form method='post' action='/auth/logout'><button class='logout' type='submit'>Log out</button></form></div><section class='hero'><span class='pill'>{_html_escape(agent.status)}</span><h1>{_html_escape(agent.name)}</h1><p class='muted'>{_html_escape(agent.platform)} · <code>{_html_escape(agent.bot_id)}</code></p></section><div class='grid'><div class='card'><h3>Current policy defaults</h3>{_policy_defaults_html(x402_caps)}</div><div class='card'><h3>Smoke-test setup</h3><ol class='checklist'><li>Keep the Payjent agent credential in the agent secret store.</li><li>Give the agent <a href='/docs/agent-payjent-self-setup.md'>the setup guide</a>.</li><li>Run the demo smoke test and confirm payment creates a one-time resumable action.</li></ol></div></div><div class='grid'>{rail_cards}</div><div class='grid'><div class='card'><h3>Agent credential</h3>{credential_form}</div><div class='card'><h3>Stripe Connect</h3><p class='muted'>Local/test starts return a simulated account link; production fails closed until live OAuth is configured.</p><pre><code>{_html_escape(stripe_cmd)}</code></pre></div><div class='card'><h3>x402 rail configuration</h3><p class='muted'>Stores only non-secret network, pay_to, facilitator URL, and caps.</p><pre><code>{_html_escape(x402_cmd)}</code></pre></div></div><div class='card'><h3>Integration snippet</h3><pre><code>{_html_escape(_integration_snippet(agent))}</code></pre></div><div class='card'><h3>Recent payments / spend ledger</h3><h3>Spend ledger entries</h3><p>{len(quotes)} recent quotes</p><ul>{ledger}</ul></div><div class='card'><h3>Paid-action lifecycle ledger</h3><table><thead><tr><th>Action</th><th>Quote</th><th>Payment</th><th>Grant</th><th>Fulfillment</th><th>Spend</th></tr></thead><tbody>{lifecycle}</tbody></table></div></main></body></html>"


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
