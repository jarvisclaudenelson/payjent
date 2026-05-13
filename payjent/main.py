from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
import hmac
import ipaddress
from secrets import token_urlsafe
import socket
import time
from typing import Any
from urllib.parse import parse_qs, parse_qsl, urlparse
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
from .artifacts import artifact_pointer, create_artifact, scrub_artifact_value
from .auth import (
    DASHBOARD_SESSION_COOKIE,
    create_bot_credential,
    create_dashboard_session_cookie,
    generate_api_key,
    get_account_from_cookie,
    hash_api_key,
    hash_password,
    normalize_email,
    require_bot_credential,
    require_operator_credential,
    verify_password,
)
from .config import CANONICAL_PUBLIC_BASE_URL, Settings, get_settings
from .db import (
    WORKOS_UNUSABLE_PASSWORD_HASH,
    account_password_hash_nullable,
    get_session,
    init_db,
)
from .models import (
    Account,
    AgentInstallLink,
    AgentProfile,
    BotCredential,
    ExecutionArtifact,
    FulfillmentEvent,
    Grant,
    PaymentSession,
    Quote,
    ToolExecution,
    RailConnection,
    ResumeEvent,
    SpendLedgerEntry,
    TaskBudget,
    TaskBudgetLedgerEntry,
    WebhookDeliveryAttempt,
)
from .money import quote_hash, validate_breakdown
from .pricing import FEE_POLICY, attach_pricing_allocation
from .providers.base import issue_receipt_and_grant
from .providers.exa import ExaProviderError, ExaProviderNotConfigured, run_deep_search as run_exa_deep_search
from .providers.elevenlabs import ElevenLabsProviderError, ElevenLabsProviderNotConfigured, run_text_to_speech as run_elevenlabs_text_to_speech
from .providers.fal import FalProviderError, FalProviderNotConfigured, run_image_generate as run_fal_image_generate
from .providers.firecrawl import FirecrawlProviderError, FirecrawlProviderNotConfigured, run_scrape as run_firecrawl_scrape
from .providers.link import LinkCredentialRequest as LinkProviderCredentialRequest
from .providers.link import (
    create_link_spend_request as create_link_provider_spend_request,
)
from .providers.link import retrieve_link_status as retrieve_link_provider_status
from .providers.link import validate_credential_type
from .providers.mock import complete_mock_payment
from .providers.decal import create_decal_checkout_session, retrieve_decal_checkout_session
from .providers.paysh import FAL_LEGACY_SERVICE_FQN, FAL_MPP_TEMPO_BASE_URL, build_execution_envelope as build_paysh_execution_envelope
from .providers.premium_actions import EXECUTION_BOUNDARY as PREMIUM_PRESET_EXECUTION_BOUNDARY, get_preset, list_presets
from .providers.stripe import (
    create_stripe_checkout_session,
    create_stripe_refund,
    parse_stripe_event,
    verify_stripe_signature,
)
from .rails import normalize_spend_rail
from .settlement_rails import list_settlement_rail_manifests, normalize_settlement_rail, settlement_rail_manifest
from .toolbox import FAL_EXTERNAL_RUNTIME_GUIDANCE, FAL_EXTERNAL_RUNTIME_OPT_IN_FIELD, STRIPE_MINIMUM_CHARGE_MINOR_BY_CURRENCY, build_tool_quote, get_tool as get_toolbox_tool, list_tools as list_toolbox_tools
from .risk import assess_checkout_risk
from .readiness import enforce_readiness, readiness_record, safe_metadata
from .schemas import (
    AgentRead,
    AgentActionFailRequest,
    AgentActionFailResponse,
    AgentRegisterRequest,
    AgentRegisterResponse,
    AgentActionCompleteResponse,
    AgentActionConsumeRequest,
    AgentActionCreate,
    AgentActionCreateResponse,
    AgentActionExecutionEnvelope,
    AgentActionStatusResponse,
    BigQueryPaidQueryCreate,
    ExecutionReadinessCheckRequest,
    ExecutionReadinessRequest,
    ExecutionReadinessResponse,
    FulfillmentCreate,
    FulfillmentRead,
    GrantPresentation,
    GrantVerifyResponse,
    ExecutionArtifactListResponse,
    ExecutionArtifactRead,
    HostedSmokeBootstrapRequest,
    HostedSmokeBootstrapResponse,
    HostedSmokeStatusRequest,
    HostedSmokeStatusResponse,
    LinkCredentialApproval,
    LinkCredentialRequest,
    LinkPollResponse,
    MockPayResponse,
    PaymentSessionRead,
    PaymentSessionRefundCreate,
    PaymentSessionRefundResponse,
    PayShPremiumActionCreate,
    PayShPremiumActionCreateResponse,
    PremiumActionCreate,
    PremiumActionPresetActionCreate,
    PremiumActionCreateResponse,
    PurchaseFulfillmentCreate,
    QuoteCreate,
    QuoteRead,
    RailConnectionRead,
    AgentSettlementRailsResponse,
    SettlementRailConfigureRequest,
    SettlementRailManifest,
    ResumeEventAckResponse,
    ResumeEventListResponse,
    ResumeEventRead,
    SpendAuthorizationCreate,
    SpendAuthorizationRead,
    SpendCaptureRequest,
    StripeConnectStartResponse,
    TaskBudgetCreate,
    TaskBudgetFundResponse,
    TaskBudgetRead,
    ToolboxQuoteCreate,
    ToolboxQuoteRead,
    ToolboxCheckoutRequest,
    ToolboxCheckoutResponse,
    ToolExecutionCreate,
    ToolExecutionRead,
    ToolExecutionCompleteRequest,
    ToolExecutionFailRequest,
    X402ConfigureRequest,
    X402PaidActionCreate,
    X402PaidActionCreateResponse,
)
from .signing import PAYJENT_SIGNATURE_HEADER, PAYJENT_TIMESTAMP_HEADER, sign_webhook_payload, verify_signature


@asynccontextmanager
async def lifespan(_app: FastAPI):
    get_settings().validate_runtime_guardrails()
    if get_session not in _app.dependency_overrides:
        init_db()
    yield


app = FastAPI(title="Payjent", lifespan=lifespan, docs_url="/api-docs", redoc_url="/redoc")

DOCS_DIR = Path(__file__).resolve().parents[1] / "docs"


@app.get("/docs/agent-payjent-self-setup.md", response_class=FileResponse)
def agent_payjent_self_setup_doc():
    path = DOCS_DIR / "agent-payjent-self-setup.md"
    if not path.exists():
        raise HTTPException(404, "document not found")
    return FileResponse(path, media_type="text/markdown; charset=utf-8", filename="agent-payjent-self-setup.md")


@app.get("/docs/decal-checkout.md", response_class=FileResponse)
def decal_checkout_doc():
    path = DOCS_DIR / "decal-checkout.md"
    if not path.exists():
        raise HTTPException(404, "document not found")
    return FileResponse(path, media_type="text/markdown; charset=utf-8", filename="decal-checkout.md")


@app.get("/docs/c3po-payjent-self-setup.md", response_class=FileResponse)
def c3po_payjent_self_setup_doc_redirect():
    return RedirectResponse("/docs/agent-payjent-self-setup.md", status_code=308)




@app.get("/docs", response_class=HTMLResponse)
def docs_index():
    return HTMLResponse("""<!doctype html><html><head><title>Payjent docs</title><meta name='viewport' content='width=device-width,initial-scale=1'></head><body><main style='font-family:system-ui;max-width:760px;margin:48px auto;padding:0 20px'><h1>Payjent agent setup</h1><p>Agent-readable setup guide for integrating paid action approvals.</p><p><a href='/docs/agent-payjent-self-setup.md'>Open /docs/agent-payjent-self-setup.md</a></p></main></body></html>""")


_EXACT_PRICING_POLICY = {
    "rule": "exact_provider_quote_required",
    "description": "Before creating a Payjent paid action, obtain the exact provider/merchant quoted price and provide a matching cost_breakdown. Optional operator fees are allowed only as explicit labeled line items. Do not use placeholder, default, demo, test, minimum, top-up, hidden, or silently injected amounts. If the exact price is unknown, do not create the paid action; tell the user Payjent is awaiting an exact provider quote.",
    "amount_minor": "Must equal the exact provider/merchant quoted total plus any explicit operator fee line items, in minor units.",
    "cost_breakdown": "Required and must sum to amount_minor; operator fees must be separate explicit line items and are not provider prices.",
    "unknown_price_behavior": "fail_closed_await_exact_provider_quote",
    "forbidden_placeholders": ["$1.00", "100 minor units", "default amount", "test amount", "minimum", "top-up", "stripe minimum", "hidden fee"],
    "operator_fee_policy": FEE_POLICY,
}


def _tool_descriptors(*, x402_available: bool | None = None) -> list[dict]:
    create_amount_requirements = {"amount_minor": "exact provider/merchant quoted total plus explicit operator fee line items only", "cost_breakdown": "required; must match amount_minor; operator fees must be separately labeled", "fail_closed_if_unknown": True, "no_hidden_or_default_fees": True}
    tools = [
        {"name": "payjent.list_capabilities", "endpoint": "/api/v1/agent-capabilities", "method": "GET", "description": "List installed agent-specific paid tool capabilities."},
        {"name": "payjent.create_paid_action", "endpoint": "/api/v1/agent-actions", "method": "POST", "description": "Create a payment-gated action only after obtaining an exact provider/merchant quote; discovery is free, execution resumes only after payment.", "pricing_policy": _EXACT_PRICING_POLICY, "amount_requirements": create_amount_requirements},
        {"name": "payjent.create_premium_action", "endpoint": "/api/v1/premium-actions", "method": "POST", "description": "Provider-neutral premium action primitive for any external provider-backed paid action. Requires exact provider quote up front; creates a request-bound Payjent payment/grant and neutral execution envelope. Payjent authorizes payment/spend only; the agent executes externally after Payjent authorization. Safe HTTPS target_url/service_url is optional when provider/body/provider_metadata fully describe a provider-backed action. Do not include Authorization, Cookie, or API-key headers.", "pricing_policy": _EXACT_PRICING_POLICY, "amount_requirements": create_amount_requirements, "execution_boundary": "agent_executes_after_payjent_authorization"},
        {"name": "payjent.list_premium_action_presets", "endpoint": "/api/v1/premium-action-presets", "method": "GET", "description": "List catalog-only premium provider presets, including required inputs, quote basis, secret policy, and execution boundary. Payjent stores safe payment-gated envelopes only; provider credentials stay agent-side and execution happens after Payjent authorization.", "preset_ids": [p["id"] for p in list_presets()]},
        {"name": "payjent.create_premium_action_from_preset", "endpoint": "/api/v1/premium-action-presets/{preset_id}/actions", "method": "POST", "description": "Create a payment-gated provider action from a preset. Payjent stores a safe execution envelope only; provider API keys remain agent-side.", "pricing_policy": _EXACT_PRICING_POLICY, "amount_requirements": create_amount_requirements, "execution_boundary": "agent_executes_after_payjent_authorization"},
        {"name": "payjent.fail_action_request_refund", "endpoint": "/api/v1/agent-actions/{action_id}/fail", "method": "POST", "description": "Mark provider execution failed and optionally request a refund for a paid, bot-scoped, unfulfilled action. Idempotent enough to avoid duplicate refunds."},
        {"name": "payjent.create_x402_paid_action", "endpoint": "/api/v1/premium-actions/x402", "method": "POST", "description": "Generic primitive for any x402/pay.sh-compatible paid URL. Requires an exact provider quote up front; creates a request-bound Payjent payment/grant and x402 execution envelope. Flow: create action, user pays through the active Payjent checkout rail, agent polls/status, agent consumes payment token/grant, agent calls payjent.authorize_x402_spend for the exact action budget, then agent executes the downstream x402 call with a funded external runtime. Payjent never POSTs the target URL or stores Authorization/Cookie/API-key headers. For PaySponge gateways, use SpongeWallet.paidFetch/x402Fetch or spongewallet CLI with agent-side SPONGE_API_KEY; Payjent's checkout checkpoint does not itself satisfy the downstream HTTP 402 challenge.", "pricing_policy": _EXACT_PRICING_POLICY, "amount_requirements": create_amount_requirements, "execution_boundary": "agent_executes_after_spend_authorization"},
        {"name": "payjent.create_pay_sh_premium_action", "endpoint": "/api/v1/premium-actions/pay-sh", "method": "POST", "description": "Backward-compatible legacy alias for payjent.create_x402_paid_action. Create a premium pay.sh/x402 action envelope gated by Payjent only after obtaining an exact provider/merchant quote. Payjent does not POST service_url or execute the downstream task; payjent_fulfillment_callback/payjent_managed_execution are legacy flags and are forced false. Agents must use a funded pay.sh/x402 runtime, and PaySponge endpoints require SpongeWallet/spongewallet rather than plain paycurl.", "pricing_policy": _EXACT_PRICING_POLICY, "amount_requirements": create_amount_requirements},
        {"name": "payjent.create_bigquery_paid_query", "endpoint": "/api/v1/premium-actions/pay-sh/bigquery-query", "method": "POST", "description": "Preset for the real pay.sh public catalog BigQuery gateway service solana-foundation/google/bigquery resource jobs. Creates a pay.sh/x402 action for POST https://bigquery.google.gateway-402.com/bigquery/v2/projects/{project_id}/queries with body {query,useLegacySql}. User pays through Payjent first; agent consumes grant, calls payjent.authorize_x402_spend with capture=true, then agent executes externally using a funded pay.sh/x402 runtime. Payjent does not execute BigQuery.", "pricing_policy": _EXACT_PRICING_POLICY, "amount_requirements": create_amount_requirements, "preset": {"provider": "pay_sh", "service_fqn": "solana-foundation/google/bigquery", "resource": "jobs", "gateway": "https://bigquery.google.gateway-402.com/bigquery/v2", "method": "POST", "path_template": "/projects/{project_id}/queries", "execution_boundary": "agent_executes_after_spend_authorization"}},
        {"name": "payjent.create_purchase_fulfillment", "endpoint": "/api/v1/purchase-actions", "method": "POST", "description": "Create an Amazon-style merchant purchase/procurement handoff only after obtaining an exact merchant quote. The human pays through Payjent; Payjent verifies payment and sends a signed, verified POST fulfillment callback to an allowlisted procurement executor. The executor buys from Amazon or the merchant using its configured procurement/payment method. Payjent does not send funds to the agent and does not directly pay Amazon unless the downstream executor/provider rail does that.", "pricing_policy": _EXACT_PRICING_POLICY, "amount_requirements": create_amount_requirements, "requires_fulfillment_callback": True},
        {"name": "payjent.check_payment", "endpoint": "/api/v1/agent-actions/{action_id}/status", "method": "GET", "description": "Check whether a paid action is awaiting payment, ready, or consumed."},
        {"name": "payjent.resume_paid_action", "endpoint": "/api/v1/agent-actions/{action_id}/start", "method": "POST", "description": "Consume the exact paid grant and resume the request-bound action."},
        {"name": "payjent.complete_action", "endpoint": "/api/v1/agent-actions/{action_id}/complete", "method": "POST", "description": "Report completion/fulfillment for a paid action."},
        {"name": "payjent.authorize_x402_spend", "endpoint": "/api/v1/grants/{grant_id}/spend-authorizations", "method": "POST", "description": "After consuming a paid Payjent grant, authorize/capture request-bound downstream x402/pay.sh spend for the exact action/budget. The agent uses this authorization to execute externally; Payjent does not call the service_url.", "required_rail": "x402"},
    ]
    tools.extend(
        {
            **tool,
            "name": f"payjent.toolbox.{tool['tool_id']}",
            "endpoint": f"/api/v1/toolbox/{tool['tool_id']}",
            "quote_endpoint": f"/api/v1/toolbox/{tool['tool_id']}/quote",
            "checkout_endpoint": f"/api/v1/toolbox/{tool['tool_id']}/checkout",
            "execution_endpoint": f"/api/v1/toolbox/{tool['tool_id']}/executions",
            "method": "GET/POST",
        }
        for tool in list_toolbox_tools()
    )
    if x402_available is not None:
        for tool in tools:
            if tool.get("required_rail") == "x402":
                tool["available"] = x402_available
                if not x402_available:
                    tool["setup_hint"] = "Configure and enable the x402 rail for this agent."
            else:
                tool["available"] = True
    return tools


def _safe_rail_config_summary(rail: RailConnection) -> dict:
    cfg = dict(rail.config_json or {})
    allowed = {
        "network", "networks", "currency", "currencies", "enabled", "mode", "status",
        "facilitator_url", "max_per_request_minor", "max_per_call_minor",
        "wallet_provider", "provider_account", "merchant_allowlist", "spend_limit_minor",
        "readiness_checks", "runtime_ready", "can_execute_without_device_auth", "provider_connected",
    }
    redacted = {key: value for key, value in cfg.items() if key in allowed}
    for key in cfg:
        lowered = key.lower()
        if any(secret_word in lowered for secret_word in ("secret", "token", "key", "password", "credential", "private")):
            redacted[key] = "redacted"
    return redacted


def _premium_tool_discovery(base_url: str, *, x402_available: bool | None = None) -> dict:
    presets = list_presets()
    create_endpoint_template = f"{base_url}/api/v1/premium-action-presets/{{preset_id}}/actions"
    common_required_action_fields = [
        "bot_id",
        "external_user_id",
        "request_hash",
        "amount_minor",
        "currency",
        "cost_breakdown",
        "input",
    ]
    recommended_paths: dict[str, list[dict[str, Any]]] = {}
    for preset in presets:
        recommended_paths.setdefault(preset["provider"], []).append(
            {
                "preset_id": preset["id"],
                "use_case": preset["task_type"],
                "provider": preset["provider"],
                "preset_catalog_url": f"{base_url}/api/v1/premium-action-presets",
                "create_endpoint_template": create_endpoint_template,
                "method": "POST",
                "required_action_fields": common_required_action_fields,
                "required_input_fields": preset["required_input_fields"],
                "optional_input_fields": preset["optional_input_fields"],
                "quote_basis": preset["quote_basis"],
                "execution_boundary": preset["execution_boundary"],
                "provider_api_credential_policy": "agent-side private credential only; do not send provider secrets to Payjent or users",
            }
        )
    discovery = {
        "premium_action_presets_url": f"{base_url}/api/v1/premium-action-presets",
        "premium_action_preset_count": len(presets),
        "execution_boundary": "Payjent stores safe payment-gated envelopes and does not execute provider API calls; the installed agent executes the provider call in its own runtime after Payjent authorization.",
        "provider_api_credential_policy": "agent-side private credential only; never put provider credentials, auth headers, cookies, tokens, or private keys in Payjent envelopes or chat.",
        "pricing_policy": _EXACT_PRICING_POLICY,
        "agent_next_steps": [
            "Fetch GET /.well-known/payjent-tools.json.",
            "If Payjent is installed for this agent, call authenticated GET /api/v1/agent-capabilities using the agent's private Payjent credential.",
            "List premium presets with GET /api/v1/premium-action-presets.",
            "Obtain the exact provider quote for the requested provider action before creating any Payjent action.",
            "Optional operator fees must be explicit cost_breakdown line items (for example, 'agent operator fee'); never hide fees in provider prices or add default fees.",
            "POST /api/v1/premium-action-presets/{preset_id}/actions with exact amount_minor, matching cost_breakdown, request binding, and required input fields.",
            "Send the returned payment/status link to the user; do not expose grants or payment tokens.",
            "Poll/resume the action after payment using authenticated status/consume endpoints and the original request binding.",
            "Execute the provider call in the agent runtime using agent-side private provider credentials.",
            "Complete the Payjent action with safe receipt metadata, or fail it and request/refuse refund according to provider outcome.",
        ],
        "premium_tool_quickstart": [
            "manifest -> authenticated capabilities -> presets -> exact provider quote -> create preset action -> user payment/status link -> poll/resume -> agent executes provider -> complete/fail/refund"
        ],
        "recommended_premium_paths": recommended_paths,
        "creation_template": {
            "endpoint": create_endpoint_template,
            "method": "POST",
            "path_params": {"preset_id": "one of the ids in recommended_premium_paths"},
            "required_fields": common_required_action_fields,
            "input_fields_by_preset": {preset["id"]: preset["required_input_fields"] for preset in presets},
            "secret_fields": "none; provider credentials remain agent-side only",
        },
    }
    if x402_available is not None:
        discovery["installed_agent_readiness"] = {
            "payjent_credential_present": True,
            "premium_presets_available": bool(presets),
            "x402_spend_authorization_available": x402_available,
        }
    return discovery


def _discovery_manifest(base_url: str) -> dict:
    premium_discovery = _premium_tool_discovery(base_url)
    return {
        "name": "Payjent",
        "version": "v0",
        "description": "Payjent spend control for agent tasks: human-approved task budgets, execution readiness, auto-resume, and refund-default fulfillment tracking.",
        "public_base_url": CANONICAL_PUBLIC_BASE_URL,
        "docs_url": f"{CANONICAL_PUBLIC_BASE_URL}/docs/agent-payjent-self-setup.md",
        "authenticated_capabilities_url": f"{base_url}/api/v1/agent-capabilities",
        "toolbox_url": f"{CANONICAL_PUBLIC_BASE_URL}/api/v1/toolbox",
        "toolbox_tool_count": len(list_toolbox_tools()),
        "premium_action_presets_url": premium_discovery["premium_action_presets_url"],
        "premium_action_preset_count": premium_discovery["premium_action_preset_count"],
        "premium_tool_discovery": premium_discovery,
        "auth": {
            "header": "X-Payjent-Bot-Key",
            "credential_install": "Credentials are installed via one-time Agent Install Link and must not be pasted in chat.",
        },
        "pricing_policy": _EXACT_PRICING_POLICY,
        "active_payment_rail": {
            "provider": "decal_when_configured",
            "description": "In production, Decal hosted checkout is the primary active payment rail when PAYJENT_CHECKOUT_PROVIDER=decal and required deployment settings are configured. Agents must send the returned payment_prompt/payment_url to the user and wait for paid status before resuming. Stripe remains a legacy fallback only when explicitly configured.",
        },
        "tools": _tool_descriptors(),
        "agent_guidance": [
            "For normal/default FAL image generation, use toolbox tool fal.image.generate; it is the Payjent-managed FAL route.",
            "Use paysh.fal_image only as an advanced external pay.sh/x402 fallback and only when the request arguments include external_runtime=true.",
            "Do not ask users for secrets, API keys, wallet mnemonics, grants, or payment tokens in chat.",
            "Use dashboard/platform connections, an agent install link, or a private secret store for Payjent credentials and provider credentials.",
            "Public Payjent URLs use https://payjent.com; do not advertise preview or testserver hosts to users.",
            "Provider credentials and wallet runtimes are agent-side requirements unless a provider connection is configured in Payjent.",
        ],
        "capabilities": {
            "task_budgets": "Every paid action carries an exact task budget and matching cost_breakdown; unknown prices fail closed.",
            "execution_readiness": "Authenticated status/capabilities tell the agent when payment and rail prerequisites are ready; public pages hide raw secrets and tokens.",
            "auto_resume": "After payment, agents poll status or receive a signed callback, then resume the stored request-bound envelope.",
            "refund_default_behavior": "If paid downstream execution fails, Payjent requests a refund by default unless the agent explicitly opts out.",
        },
        "security_invariants": [
            "request-bound approvals and grants",
            "paid-before-execute",
            "exact provider/merchant quoted price required; optional operator fees only as explicit separate cost_breakdown line items; no hidden/default fees",
            "no raw grants, credentials, or payment tokens in chat",
            "exact request resume",
        ],
    }


@app.get("/.well-known/payjent-tools.json")
def well_known_payjent_tools(request: Request, settings: Settings = Depends(get_settings)):
    return _discovery_manifest(_public_base_url(request, settings))


@app.get("/.well-known/payjent-agent-setup")
def well_known_payjent_agent_setup():
    return RedirectResponse("/docs/agent-payjent-self-setup.md", status_code=308)


@app.get("/api/v1/toolbox")
def toolbox_list():
    return {"tools": list_toolbox_tools(), "count": len(list_toolbox_tools())}


def _toolbox_quote_or_404(tool_id: str, payload: ToolboxQuoteCreate) -> tuple[dict[str, Any], dict[str, Any]]:
    tool = get_toolbox_tool(tool_id)
    if not tool:
        raise HTTPException(status_code=404, detail="tool not found")
    _reject_secret_argument_keys(payload.arguments)
    _enforce_paysh_fal_external_runtime_opt_in(tool_id, payload.arguments)
    if tool_id == "firecrawl.scrape":
        _sanitize_toolbox_arguments(tool_id, payload.arguments)
    try:
        toolbox_quote = build_tool_quote(
            tool,
            bot_id=payload.bot_id,
            external_user_id=payload.external_user_id,
            arguments=payload.arguments,
            amount_minor=payload.amount_minor,
            currency=payload.currency,
            cost_breakdown=[item.model_dump() for item in payload.cost_breakdown] if payload.cost_breakdown is not None else None,
        )
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    if payload.request_hash and payload.request_hash != toolbox_quote["request_hash"]:
        raise HTTPException(status_code=409, detail="request_hash does not match recomputed toolbox quote")
    return tool, toolbox_quote


_SECRET_ARGUMENT_MARKERS = ("secret", "token", "api_key", "apikey", "authorization", "cookie", "password", "private_key", "credential", "grant")
_EXECUTABLE_URL_KEY_MARKERS = ("target_url", "service_url", "callback", "webhook", "api_url")


def _enforce_paysh_fal_external_runtime_opt_in(tool_id: str, arguments: dict[str, Any]) -> None:
    if tool_id != "paysh.fal_image":
        return
    if arguments.get(FAL_EXTERNAL_RUNTIME_OPT_IN_FIELD) is True:
        return
    raise HTTPException(
        status_code=422,
        detail={
            "code": "external_runtime_opt_in_required",
            "tool_id": "paysh.fal_image",
            "recommended_tool_id": "fal.image.generate",
            "required_argument": FAL_EXTERNAL_RUNTIME_OPT_IN_FIELD,
            "guidance": FAL_EXTERNAL_RUNTIME_GUIDANCE,
        },
    )


def _secret_like_marker(value: Any) -> bool:
    normalized = str(value).lower().replace("-", "_")
    return any(marker in normalized for marker in _SECRET_ARGUMENT_MARKERS)


def _reject_secret_like_url_parts(parsed: Any) -> None:
    if parsed.username or parsed.password:
        raise HTTPException(status_code=422, detail="toolbox URL arguments may not include userinfo")
    for key, _value in parse_qsl(parsed.query, keep_blank_values=True):
        if _secret_like_marker(key):
            raise HTTPException(status_code=422, detail="toolbox URL arguments may not include secret-like query keys")


def _reject_secret_argument_keys(value: Any, path: str = "arguments") -> None:
    if isinstance(value, dict):
        for k, v in value.items():
            normalized = str(k).lower().replace("-", "_")
            if _secret_like_marker(normalized):
                raise HTTPException(status_code=422, detail=f"toolbox arguments may not include secret-like key: {path}.{k}")
            _reject_secret_argument_keys(v, f"{path}.{k}")
    elif isinstance(value, list):
        for idx, item in enumerate(value):
            _reject_secret_argument_keys(item, f"{path}[{idx}]")


def _public_https_url_parts(raw: str) -> tuple[str, dict[str, str]]:
    parsed = urlparse(raw)
    if parsed.scheme.lower() != "https" or not parsed.hostname:
        raise HTTPException(status_code=422, detail="toolbox URL arguments must be public HTTPS URLs")
    _reject_secret_like_url_parts(parsed)
    host = parsed.hostname.lower()
    try:
        ip = ipaddress.ip_address(host)
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
            raise HTTPException(status_code=422, detail="toolbox URL arguments must be public HTTPS URLs")
    except ValueError:
        if host in {"localhost", "metadata.google.internal"} or host.endswith(".local"):
            raise HTTPException(status_code=422, detail="toolbox URL arguments must be public HTTPS URLs")
    path = parsed.path or "/"
    netloc = host if parsed.port is None else f"{host}:{parsed.port}"
    canonical_url = f"https://{netloc}{path}"
    if parsed.query:
        canonical_url = f"{canonical_url}?{parsed.query}"
    return canonical_url, {"scheme": "https", "host": host}


def _public_https_url_summary(raw: str) -> dict[str, str]:
    _canonical_url, summary = _public_https_url_parts(raw)
    return summary


def _internal_https_url_summary(raw: str) -> dict[str, str]:
    canonical_url, summary = _public_https_url_parts(raw)
    return {**summary, "canonical_url": canonical_url}


def _redact_toolbox_arguments_for_read(tool_id: str, value: Any) -> Any:
    if tool_id == "firecrawl.scrape" and isinstance(value, dict):
        redacted = dict(value)
        url = redacted.get("url")
        if isinstance(url, dict):
            redacted["url"] = {k: v for k, v in url.items() if k in {"scheme", "host"}}
        return redacted
    return value


def _sanitize_toolbox_arguments(tool_id: str, value: Any, path: str = "arguments") -> Any:
    if isinstance(value, dict):
        sanitized: dict[str, Any] = {}
        for k, v in value.items():
            key = str(k)
            normalized = key.lower().replace("-", "_")
            if _secret_like_marker(normalized):
                raise HTTPException(status_code=422, detail=f"toolbox arguments may not include secret-like key: {path}.{key}")
            if isinstance(v, str) and (normalized == "url" or normalized.endswith("_url") or any(marker in normalized for marker in _EXECUTABLE_URL_KEY_MARKERS)):
                if tool_id == "firecrawl.scrape" and normalized == "url":
                    sanitized[key] = _internal_https_url_summary(v)
                else:
                    raise HTTPException(status_code=422, detail=f"toolbox arguments may not include executable URL field: {path}.{key}")
            else:
                sanitized[key] = _sanitize_toolbox_arguments(tool_id, v, f"{path}.{key}")
        return sanitized
    if isinstance(value, list):
        return [_sanitize_toolbox_arguments(tool_id, item, f"{path}[{idx}]") for idx, item in enumerate(value)]
    return value


def _toolbox_execution_envelope(tool: dict[str, Any], toolbox_quote: dict[str, Any], arguments: dict[str, Any]) -> dict[str, Any]:
    sanitized_arguments = _sanitize_toolbox_arguments(tool["tool_id"], arguments)
    argument_summary = _redact_toolbox_arguments_for_read(tool["tool_id"], sanitized_arguments)
    return {
        "tool_id": tool["tool_id"],
        "toolbox_request_hash": toolbox_quote["request_hash"],
        "provider_type": toolbox_quote["provider_type"],
        "execution_mode": toolbox_quote["execution_mode"],
        "argument_summary": argument_summary,
        "payment_options": toolbox_quote["payment_options"],
        "recommended_payment_rail": toolbox_quote["recommended_payment_rail"],
        "execution_boundary": "agent_executes_after_payjent_authorization",
        "execution_caveat": toolbox_quote["execution_caveat"],
        "arbitrary_url_execution": False,
    }


def _create_quote_for_toolbox(payload: ToolboxQuoteCreate, tool: dict[str, Any], toolbox_quote: dict[str, Any], session: Session) -> Quote:
    cost_breakdown = toolbox_quote["cost_breakdown"]
    canonical = {
        "bot_id": payload.bot_id,
        "external_user_id": payload.external_user_id,
        "request_summary": f"Toolbox action: {tool['tool_id']}",
        "request_hash": toolbox_quote["request_hash"],
        "amount_minor": toolbox_quote["amount_minor"],
        "currency": toolbox_quote["currency"].upper(),
        "cost_breakdown": cost_breakdown,
        "execution_envelope": attach_pricing_allocation(_toolbox_execution_envelope(tool, toolbox_quote, payload.arguments), cost_breakdown),
        "callback_url": None,
    }
    q = Quote(id=f"quote_{uuid4().hex}", quote_hash=quote_hash(canonical), **canonical)
    session.add(q); session.commit(); session.refresh(q)
    return q


def _scrub_secret_metadata(value: Any) -> Any:
    secret_markers = ("secret", "token", "api_key", "apikey", "authorization", "cookie", "password", "private_key", "credential", "grant")
    if isinstance(value, dict):
        return {str(k): ("redacted" if any(m in str(k).lower().replace("-", "_") for m in secret_markers) else _scrub_secret_metadata(v)) for k, v in value.items()}
    if isinstance(value, list):
        return [_scrub_secret_metadata(v) for v in value]
    return value


def _execution_to_read(execution: ToolExecution) -> ToolExecutionRead:
    data = ToolExecutionRead.model_validate(execution, from_attributes=True)
    data.arguments_json = _redact_toolbox_arguments_for_read(execution.tool_id, data.arguments_json)
    return data


@app.get("/api/v1/toolbox/executions/{execution_id}", response_model=ToolExecutionRead)
def toolbox_get_execution(execution_id: str, session: Session = Depends(get_session), credential: BotCredential = Depends(require_bot_credential)):
    execution = session.get(ToolExecution, execution_id)
    if not execution:
        raise HTTPException(404, "tool execution not found")
    _enforce_bot_scope(credential, execution.bot_id)
    return _execution_to_read(execution)


@app.post("/api/v1/toolbox/executions/{execution_id}/complete", response_model=ToolExecutionRead)
def toolbox_complete_execution(execution_id: str, payload: ToolExecutionCompleteRequest, session: Session = Depends(get_session), credential: BotCredential = Depends(require_bot_credential)):
    execution = session.get(ToolExecution, execution_id)
    if not execution:
        raise HTTPException(404, "tool execution not found")
    _enforce_bot_scope(credential, execution.bot_id)
    if execution.status not in {"ready_to_execute", "executing", "paid"}:
        raise HTTPException(status_code=409, detail="tool execution must be paid before completion")
    execution.status = "succeeded"
    execution.result_metadata_json = _scrub_secret_metadata(payload.result_metadata)
    execution.updated_at = datetime.now(timezone.utc)
    session.add(execution); session.commit(); session.refresh(execution)
    return _execution_to_read(execution)


@app.post("/api/v1/toolbox/executions/{execution_id}/fail", response_model=ToolExecutionRead)
def toolbox_fail_execution(execution_id: str, payload: ToolExecutionFailRequest, session: Session = Depends(get_session), credential: BotCredential = Depends(require_bot_credential)):
    execution = session.get(ToolExecution, execution_id)
    if not execution:
        raise HTTPException(404, "tool execution not found")
    _enforce_bot_scope(credential, execution.bot_id)
    execution.status = "failed"
    execution.error_metadata_json = _scrub_secret_metadata(payload.error_metadata)
    execution.updated_at = datetime.now(timezone.utc)
    session.add(execution); session.commit(); session.refresh(execution)
    return _execution_to_read(execution)


def _artifact_to_read(artifact: ExecutionArtifact) -> ExecutionArtifactRead:
    return ExecutionArtifactRead(
        artifact_id=artifact.artifact_id,
        execution_id=artifact.execution_id,
        kind=artifact.kind,
        mime_type=artifact.mime_type,
        size_bytes=artifact.size_bytes,
        storage_backend=artifact.storage_backend,
        checksum_sha256=artifact.checksum_sha256,
        metadata_json=scrub_artifact_value(artifact.metadata_json or {}),
        created_at=artifact.created_at.isoformat(),
        content_base64=artifact.content_base64,
        text_payload=artifact.text_payload,
        payload_json=scrub_artifact_value(artifact.payload_json),
    )


@app.get("/api/v1/toolbox/executions/{execution_id}/artifacts", response_model=ExecutionArtifactListResponse)
def toolbox_list_execution_artifacts(execution_id: str, session: Session = Depends(get_session), credential: BotCredential = Depends(require_bot_credential)):
    execution = session.get(ToolExecution, execution_id)
    if not execution:
        raise HTTPException(404, "tool execution not found")
    _enforce_bot_scope(credential, execution.bot_id)
    artifacts = session.exec(select(ExecutionArtifact).where(ExecutionArtifact.execution_id == execution_id).order_by(ExecutionArtifact.created_at)).all()
    return ExecutionArtifactListResponse(artifacts=[artifact_pointer(a) for a in artifacts])


@app.get("/api/v1/toolbox/executions/{execution_id}/artifacts/{artifact_id}", response_model=ExecutionArtifactRead)
def toolbox_get_execution_artifact(execution_id: str, artifact_id: str, session: Session = Depends(get_session), credential: BotCredential = Depends(require_bot_credential)):
    execution = session.get(ToolExecution, execution_id)
    if not execution:
        raise HTTPException(404, "tool execution not found")
    _enforce_bot_scope(credential, execution.bot_id)
    artifact = session.get(ExecutionArtifact, artifact_id)
    if not artifact or artifact.execution_id != execution_id:
        raise HTTPException(404, "artifact not found")
    return _artifact_to_read(artifact)


@app.post("/api/v1/toolbox/executions/{execution_id}/run", response_model=ToolExecutionRead)
def toolbox_run_execution(execution_id: str, session: Session = Depends(get_session), settings: Settings = Depends(get_settings), credential: BotCredential = Depends(require_bot_credential)):
    execution = session.get(ToolExecution, execution_id)
    if not execution:
        raise HTTPException(404, "tool execution not found")
    _enforce_bot_scope(credential, execution.bot_id)
    if execution.tool_id not in {"exa.deep_search", "firecrawl.scrape", "elevenlabs.text_to_speech", "fal.image.generate"}:
        raise HTTPException(status_code=501, detail="managed execution adapter not implemented for tool")
    if execution.status == "executing":
        raise HTTPException(status_code=409, detail="tool execution is already executing")
    if execution.status not in {"ready_to_execute", "paid"}:
        raise HTTPException(status_code=409, detail="tool execution must be paid before run")

    now = datetime.now(timezone.utc)
    execution.status = "executing"
    execution.updated_at = now
    session.add(execution); session.commit(); session.refresh(execution)
    try:
        if execution.tool_id == "exa.deep_search":
            result = run_exa_deep_search(execution.arguments_json or {}, api_key=settings.exa_api_key)
        elif execution.tool_id == "firecrawl.scrape":
            result = run_firecrawl_scrape(execution.arguments_json or {}, api_key=settings.firecrawl_api_key)
        elif execution.tool_id == "fal.image.generate":
            result = run_fal_image_generate(execution.arguments_json or {}, api_key=settings.fal_api_key)
        else:
            result = run_elevenlabs_text_to_speech(execution.arguments_json or {}, api_key=settings.elevenlabs_api_key)
    except (ExaProviderNotConfigured, FirecrawlProviderNotConfigured, ElevenLabsProviderNotConfigured, FalProviderNotConfigured):
        execution.status = "failed"
        execution.error_metadata_json = {"code": "provider_not_configured", "message": "managed provider is not configured"}
        execution.updated_at = datetime.now(timezone.utc)
        session.add(execution); session.commit(); session.refresh(execution)
        raise HTTPException(status_code=503, detail="provider_not_configured")
    except ValueError as exc:
        execution.status = "failed"
        execution.error_metadata_json = {"code": "invalid_arguments", "message": str(exc)}
        execution.updated_at = datetime.now(timezone.utc)
        session.add(execution); session.commit(); session.refresh(execution)
        raise HTTPException(status_code=422, detail=str(exc))
    except (ExaProviderError, FirecrawlProviderError, ElevenLabsProviderError, FalProviderError):
        execution.status = "failed"
        execution.error_metadata_json = {"code": "provider_execution_failed", "message": "managed provider execution failed"}
        execution.updated_at = datetime.now(timezone.utc)
        session.add(execution); session.commit(); session.refresh(execution)
        return _execution_to_read(execution)

    artifacts = []
    if execution.tool_id == "fal.image.generate":
        safe_images = []
        for image in (result.get("images") or []):
            if not isinstance(image, dict):
                continue
            content_bytes = image.get("content_bytes")
            metadata = {"provider": "fal"}
            if image.get("url"):
                metadata["source_url_hosted_public_https"] = True
            if isinstance(content_bytes, (bytes, bytearray)):
                artifact = create_artifact(session, execution_id=execution.id, kind="image", mime_type=image.get("mime_type") or "image/png", content_bytes=bytes(content_bytes), metadata=metadata)
            else:
                artifact = create_artifact(session, execution_id=execution.id, kind="json", mime_type="application/json", json_payload={"delivery_mode": "metadata_only", "source_url_available": bool(image.get("url"))}, metadata=metadata)
            artifacts.append(artifact)
            safe_images.append({k: v for k, v in image.items() if k not in {"content_bytes", "url"}})
        result["images"] = safe_images
    execution.status = "succeeded"
    execution.result_metadata_json = _scrub_secret_metadata(result)
    if artifacts:
        execution.result_metadata_json["artifacts"] = [artifact_pointer(a) for a in artifacts]
    execution.error_metadata_json = {}
    execution.updated_at = datetime.now(timezone.utc)
    session.add(execution); session.commit(); session.refresh(execution)
    if execution.quote_id and execution.payment_session_id:
        q = session.get(Quote, execution.quote_id)
        ps = session.get(PaymentSession, execution.payment_session_id)
        if q and ps:
            event = _enqueue_resume_event(session, q, ps, settings, "managed")
            event.payload = scrub_artifact_value({**event.payload, "managed_execution": {"execution_id": execution.id, "tool_id": execution.tool_id, "status": execution.status, "artifacts": [artifact_pointer(a) for a in artifacts]}})
            timestamp, signature = sign_webhook_payload(event.payload, settings.signing_secret)
            event.signature_timestamp = timestamp
            event.signature = signature
            event.updated_at = datetime.now(timezone.utc)
            session.add(event); session.commit()
    return _execution_to_read(execution)


@app.get("/api/v1/toolbox/{tool_id}")
def toolbox_detail(tool_id: str):
    tool = get_toolbox_tool(tool_id)
    if not tool:
        raise HTTPException(status_code=404, detail="tool not found")
    return tool


@app.post("/api/v1/toolbox/{tool_id}/quote", response_model=ToolboxQuoteRead)
def toolbox_quote(tool_id: str, payload: ToolboxQuoteCreate, credential: BotCredential = Depends(require_bot_credential)):
    _enforce_bot_scope(credential, payload.bot_id)
    _tool, toolbox_quote = _toolbox_quote_or_404(tool_id, payload)
    return toolbox_quote


@app.post("/api/v1/toolbox/{tool_id}/checkout", response_model=ToolboxCheckoutResponse)
def toolbox_checkout(tool_id: str, payload: ToolboxCheckoutRequest, idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"), provider: str | None = Header(default=None, alias="X-Payjent-Provider"), readiness_mode: str = Header(default="advisory", alias="X-Payjent-Readiness-Mode"), session: Session = Depends(get_session), settings: Settings = Depends(get_settings), credential: BotCredential = Depends(require_bot_credential)):
    _enforce_bot_scope(credential, payload.bot_id)
    tool, toolbox_quote = _toolbox_quote_or_404(tool_id, payload)
    _enforce_managed_provider_ready(tool_id, settings, mode=readiness_mode)
    if idempotency_key:
        for existing in session.exec(select(PaymentSession).where(PaymentSession.idempotency_key == idempotency_key)).all():
            existing_quote = session.get(Quote, existing.quote_id)
            envelope = existing_quote.execution_envelope if existing_quote else {}
            if (
                existing_quote
                and existing_quote.bot_id == payload.bot_id
                and envelope.get("tool_id") == tool_id
                and envelope.get("toolbox_request_hash") == toolbox_quote["request_hash"]
                and existing_quote.request_hash == toolbox_quote["request_hash"]
            ):
                return ToolboxCheckoutResponse(status="checkout_created", quote=quote_to_read(existing_quote), payment_session=session_to_read(existing), payment_url=existing.checkout_url, toolbox_quote=toolbox_quote)
    q = _create_quote_for_toolbox(payload, tool, toolbox_quote, session)
    try:
        ps = _create_checkout_for_quote(q, idempotency_key=idempotency_key, provider=provider, session=session, settings=settings)
    except Exception:
        session.delete(q)
        session.commit()
        raise
    return ToolboxCheckoutResponse(status="checkout_created", quote=quote_to_read(q), payment_session=session_to_read(ps), payment_url=ps.checkout_url, toolbox_quote=toolbox_quote)


@app.post("/api/v1/toolbox/{tool_id}/executions", response_model=ToolExecutionRead)
def toolbox_create_execution(tool_id: str, payload: ToolExecutionCreate, session: Session = Depends(get_session), credential: BotCredential = Depends(require_bot_credential)):
    _enforce_bot_scope(credential, payload.bot_id)
    _tool, toolbox_quote = _toolbox_quote_or_404(tool_id, payload)
    quote_id = payload.quote_id
    payment_session_id = payload.payment_session_id
    status = "payment_required"
    if payment_session_id:
        ps = session.get(PaymentSession, payment_session_id)
        if not ps:
            raise HTTPException(404, "payment session not found")
        q = session.get(Quote, ps.quote_id)
        if not q:
            raise HTTPException(404, "quote not found")
        _enforce_bot_scope(credential, q.bot_id)
        if q.bot_id != payload.bot_id or q.external_user_id != payload.external_user_id:
            raise HTTPException(422, "payment session quote scope does not match toolbox execution")
        if q.request_hash != toolbox_quote["request_hash"]:
            raise HTTPException(409, "payment session quote does not match recomputed toolbox quote")
        if quote_id and quote_id != q.id:
            raise HTTPException(409, "quote_id does not match payment session quote")
        quote_id = q.id
        status = "ready_to_execute" if ps.status == "paid" else "payment_required"
    elif quote_id:
        q = session.get(Quote, quote_id)
        if not q:
            raise HTTPException(404, "quote not found")
        _enforce_bot_scope(credential, q.bot_id)
        if q.bot_id != payload.bot_id or q.external_user_id != payload.external_user_id:
            raise HTTPException(422, "quote scope does not match toolbox execution")
        if q.request_hash != toolbox_quote["request_hash"]:
            raise HTTPException(409, "quote does not match recomputed toolbox quote")
    sanitized_arguments = _sanitize_toolbox_arguments(tool_id, payload.arguments)
    execution = ToolExecution(id=f"texec_{uuid4().hex}", tool_id=tool_id, bot_id=payload.bot_id, external_user_id=payload.external_user_id, quote_id=quote_id, payment_session_id=payment_session_id, amount_minor=toolbox_quote["amount_minor"], currency=toolbox_quote["currency"], request_hash=toolbox_quote["request_hash"], arguments_json=sanitized_arguments, status=status)
    session.add(execution); session.commit(); session.refresh(execution)
    return _execution_to_read(execution)

@app.get("/", response_class=HTMLResponse)
def landing_page(settings: Settings = Depends(get_settings)):
    return HTMLResponse(_landing_page_html(settings))


@app.get("/demo", response_class=HTMLResponse)
@app.get("/hackathon", response_class=HTMLResponse)
def demo_page(settings: Settings = Depends(get_settings)):
    return HTMLResponse(_demo_page_html(settings))


def _html_escape(value) -> str:
    import html
    return html.escape(str(value), quote=True)


def _format_money(amount_minor: int, currency: str) -> str:
    return f"{amount_minor / 100:.2f} {currency.upper()}"


_STATUS_PAGE_CSS = """<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover"><style>
:root{--bg:#f6f9fc;--card:#fff;--ink:#061b31;--muted:#64748d;--label:#273951;--line:#e5edf5;--accent:#533afd;--accent2:#ea2261;--ok:#108c3d;--okbg:rgba(21,190,83,.16);--warn:#9b6829;--shadow:rgba(50,50,93,.25) 0 30px 45px -30px,rgba(0,0,0,.1) 0 18px 36px -18px}*{box-sizing:border-box}html{background:var(--bg)}body{margin:0;min-height:100vh;color:var(--ink);font-family:'Source Sans 3',Inter,ui-sans-serif,system-ui,-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;-webkit-font-smoothing:antialiased;background:radial-gradient(circle at 15% -10%,rgba(83,58,253,.16),transparent 30rem),radial-gradient(circle at 95% 0%,rgba(234,34,97,.12),transparent 24rem),var(--bg)}main{width:min(960px,100%);margin:0 auto;padding:40px 20px 56px}.shell{display:grid;gap:18px}.brand{display:flex;align-items:center;justify-content:space-between;gap:16px;margin-bottom:22px}.logo{display:flex;align-items:center;gap:10px;font-weight:600;letter-spacing:-.01em}.mark{width:34px;height:34px;border-radius:9px;background:linear-gradient(135deg,var(--accent),#7b61ff);box-shadow:rgba(83,58,253,.25) 0 14px 28px -12px;display:grid;place-items:center;color:white;font-weight:700}.domain{color:var(--muted);font-size:14px}.hero{padding:30px 28px;border:1px solid var(--line);border-radius:16px;background:rgba(255,255,255,.86);box-shadow:var(--shadow);backdrop-filter:blur(14px)}.eyebrow{font-size:13px;text-transform:uppercase;letter-spacing:.11em;color:var(--accent);font-weight:700;margin-bottom:10px}h1{font-size:clamp(34px,7vw,54px);line-height:1.02;letter-spacing:-1.2px;font-weight:300;margin:0 0 12px;color:var(--ink)}p{font-size:17px;line-height:1.48;margin:0}.muted{color:var(--muted)}.resume-card{margin-top:22px;padding:18px;border:1px solid rgba(83,58,253,.18);border-radius:14px;background:linear-gradient(180deg,rgba(83,58,253,.07),rgba(255,255,255,.74))}.resume-card h2{font-size:20px;font-weight:500;letter-spacing:-.02em;margin:0 0 8px}.prompt-wrap{display:grid;grid-template-columns:minmax(0,1fr) auto;gap:10px;align-items:stretch;margin-top:14px}.prompt-box{width:100%;min-height:92px;border:1px solid #d7e1ef;border-radius:12px;background:white;color:var(--ink);font:600 15px/1.45 ui-monospace,SFMono-Regular,Menlo,Monaco,Consolas,'Liberation Mono',monospace;padding:13px;resize:vertical}.copy-btn{border:0;border-radius:12px;background:var(--ink);color:white;font-weight:700;padding:0 18px;cursor:pointer;box-shadow:rgba(6,27,49,.22) 0 12px 22px -12px}.copy-btn:focus-visible,.btn:focus-visible{outline:3px solid rgba(83,58,253,.32);outline-offset:2px}.copy-hint{display:block;margin-top:8px;color:var(--muted);font-size:14px}.grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:16px}.card{border:1px solid var(--line);background:var(--card);border-radius:14px;padding:20px;box-shadow:rgba(23,23,23,.06) 0 10px 26px}.card h2,.card h3{font-weight:400;letter-spacing:-.02em;margin:0 0 10px;color:var(--ink)}.status-pill{display:inline-flex;align-items:center;gap:8px;border:1px solid rgba(21,190,83,.35);background:var(--okbg);color:var(--ok);border-radius:999px;padding:6px 10px;font-weight:700}.dot{width:8px;height:8px;border-radius:999px;background:#15be53;box-shadow:0 0 0 4px rgba(21,190,83,.15)}.kv{display:grid;gap:10px;margin-top:14px}.row{display:grid;grid-template-columns:150px minmax(0,1fr);gap:12px;align-items:start}.label{font-size:13px;text-transform:uppercase;letter-spacing:.08em;color:var(--muted);font-weight:700}.value{min-width:0;overflow-wrap:anywhere;color:var(--label)}code{font-family:'Source Code Pro',ui-monospace,SFMono-Regular,Menlo,monospace;background:#f3f6fb;border:1px solid #e8eef7;border-radius:6px;padding:2px 5px;font-size:.9em}.timeline{list-style:none;margin:14px 0 0;padding:0;display:grid;gap:10px}.timeline li{display:flex;gap:10px;align-items:flex-start;color:var(--label)}.check{width:22px;height:22px;border-radius:999px;background:var(--okbg);color:var(--ok);display:grid;place-items:center;font-size:14px;flex:0 0 auto}.actions{display:flex;flex-wrap:wrap;gap:10px;margin-top:18px}.btn{display:inline-flex;align-items:center;justify-content:center;min-height:42px;padding:10px 15px;border-radius:8px;background:var(--accent);color:white;text-decoration:none;font-weight:700;box-shadow:rgba(83,58,253,.24) 0 12px 24px -10px}.btn.secondary{background:#fff;color:var(--accent);border:1px solid #d6d9fc;box-shadow:none}.fine{font-size:14px;color:var(--muted);line-height:1.45}.linkbox{border:1px solid #d6d9fc;background:#fbfcff;border-radius:12px;padding:16px}pre{white-space:pre-wrap;overflow:auto;background:#061b31;color:#fff;border-radius:10px;padding:14px}@media(max-width:700px){main{padding:24px 14px 42px}.brand{align-items:flex-start}.hero{padding:24px 20px;border-radius:14px}.grid{grid-template-columns:1fr}.row{grid-template-columns:1fr;gap:3px}.domain{display:none}.actions{display:grid}.btn{width:100%}.prompt-wrap{grid-template-columns:1fr}.copy-btn{min-height:44px}}
</style>"""


def _checkout_provider(settings: Settings) -> str:
    return (settings.checkout_provider or "mock").lower()


def _managed_provider_readiness(settings: Settings) -> dict[str, bool]:
    return {
        "exa.deep_search": bool(settings.exa_api_key),
        "firecrawl.scrape": bool(settings.firecrawl_api_key),
        "elevenlabs.text_to_speech": bool(settings.elevenlabs_api_key),
        "fal.image.generate": bool(settings.fal_api_key),
    }


def _premium_preset_readiness(settings: Settings) -> list[dict[str, Any]]:
    managed = _managed_provider_readiness(settings)
    provider_ready = {
        "exa": managed.get("exa.deep_search", False),
        "firecrawl": managed.get("firecrawl.scrape", False),
        "elevenlabs": managed.get("elevenlabs.text_to_speech", False),
        "fal": managed.get("fal.image.generate", False),
        # Agent-side execution presets do not require Payjent to store provider secrets.
        "replicate": True,
        "browserbase": True,
        "perplexity": True,
    }
    rows = []
    for preset in list_presets():
        provider = str(preset.get("provider") or "unknown")
        ready = bool(provider_ready.get(provider, True))
        rows.append({
            "id": preset.get("id"),
            "name": preset.get("name"),
            "provider": provider,
            "ready": ready,
            "hint": "Ready for Payjent-managed execution." if ready and provider in {"exa", "firecrawl", "elevenlabs", "fal"} else (
                "Agent must connect provider credentials in its private runtime/settings before executing after payment."
            ),
        })
    return rows


def _database_mode(database_url: str | None) -> str:
    normalized = (database_url or "").strip().lower()
    if normalized.startswith("sqlite:"):
        return "sqlite"
    if normalized.startswith(("postgresql:", "postgres:")):
        return "postgres"
    return "unknown"


def _production_guardrails_summary(settings: Settings) -> dict[str, Any]:
    checks = [
        "non_default_signing_material_in_production",
        "https_public_base_url_in_production",
        "configured_checkout_credentials_when_required",
    ]
    try:
        settings.validate_runtime_guardrails()
    except RuntimeError:
        return {"safe": False, "checks": checks}
    return {"safe": True, "checks": checks}


def _enforce_managed_provider_ready(tool_id: str, settings: Settings, *, mode: str = "advisory") -> None:
    if mode.strip().lower() not in {"enforced", "strict"}:
        return
    readiness = _managed_provider_readiness(settings).get(tool_id)
    if readiness is False:
        raise HTTPException(status_code=503, detail={"code": "provider_not_configured", "tool_id": tool_id, "readiness_mode": "enforced"})


def _payment_readiness(settings: Settings) -> dict:
    provider = _checkout_provider(settings)
    stripe_secret_configured = bool(settings.stripe_secret_key)
    stripe_webhook_configured = bool(settings.stripe_webhook_secret)
    public_base_url_configured = bool(settings.public_base_url)
    database_configured = bool(settings.database_url)
    production_persistent_database_configured = settings.production_persistent_database_configured if settings.is_production else database_configured
    managed_providers = _managed_provider_readiness(settings)
    decal_api_key_configured = bool(settings.decal_api_key)
    decal_payment_destination_configured = bool(settings.decal_payment_destination)
    active_payment_ready = (
        provider == "decal" and decal_api_key_configured and decal_payment_destination_configured and public_base_url_configured and production_persistent_database_configured
    ) or (
        provider == "stripe" and stripe_secret_configured and stripe_webhook_configured and public_base_url_configured and production_persistent_database_configured
    )
    return {
        "active_payment_ready": active_payment_ready,
        "checkout_provider": provider,
        "decal_api_key_configured": decal_api_key_configured,
        "decal_payment_destination_configured": decal_payment_destination_configured,
        "decal_public_base_url_configured": public_base_url_configured,
        "decal_database_configured": production_persistent_database_configured,
        "stripe_secret_configured": stripe_secret_configured,
        "stripe_webhook_configured": stripe_webhook_configured,
        "public_base_url_configured": public_base_url_configured,
        "database_configured": database_configured,
        "production_persistent_database_configured": production_persistent_database_configured,
        "managed_provider_readiness": managed_providers,
        "managed_provider_ready": all(managed_providers.values()),
    }


@app.get("/api/v1/payment-readiness")
def payment_readiness(settings: Settings = Depends(get_settings)):
    return _payment_readiness(settings)


@app.get("/api/v1/status")
def public_operational_status(settings: Settings = Depends(get_settings)):
    managed = _managed_provider_readiness(settings)
    provider = _checkout_provider(settings)
    return {
        "product": "Payjent",
        "public_base_url": CANONICAL_PUBLIC_BASE_URL,
        "checkout": {
            "provider_safe_mode": provider == "mock",
            "provider": "mock" if provider == "mock" else "configured_external",
        },
        "toolbox_count": len(list_toolbox_tools()),
        "premium_preset_count": len(list_presets()),
        "managed_provider_configured": {
            "fal": managed["fal.image.generate"],
            "exa": managed["exa.deep_search"],
            "firecrawl": managed["firecrawl.scrape"],
            "elevenlabs": managed["elevenlabs.text_to_speech"],
        },
        "database_mode": _database_mode(settings.database_url),
        "exact_quote_policy": {
            "rule": _EXACT_PRICING_POLICY["rule"],
            "unknown_price_behavior": _EXACT_PRICING_POLICY["unknown_price_behavior"],
            "summary": "exact provider/merchant quoted price and matching cost_breakdown required; unknown prices fail closed",
        },
        "production_guardrails": _production_guardrails_summary(settings),
    }


def _primary_cta(settings: Settings) -> str:
    return "/auth/workos/login" if workos_auth.workos_configured(settings) else "/auth/register"


def _landing_page_html(settings: Settings) -> str:
    primary = _primary_cta(settings)
    return """<!doctype html><html lang='en'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><title>Payjent — The payment gate for agent work</title><meta name='description' content='Payjent is the human-approved checkpoint for paid agent work: quote, approval, single-use grant, and receipt-backed ledger.'><style>
:root{--paper:#fafaf7;--paper2:#f1efe8;--paper3:#e6e3d9;--ink:#0c0c0a;--ink2:#3a3935;--ink3:#74716a;--accent:#1947e5;--ok:#0e7a3b;--warn:#a8731f;--danger:#b51f1f}*{box-sizing:border-box}html{scroll-behavior:smooth}body{margin:0;background:var(--paper);color:var(--ink);font-family:Inter,ui-sans-serif,system-ui,-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;-webkit-font-smoothing:antialiased}a{color:inherit;text-decoration:none}.mono{font-family:'IBM Plex Mono','SFMono-Regular',Menlo,monospace}.container{max-width:1200px;margin:0 auto;padding:0 32px}.ribbon{background:var(--ink);color:var(--paper);border-bottom:1px solid var(--ink);font:700 11px/1.2 ui-monospace,Menlo,monospace;letter-spacing:.08em;text-transform:uppercase;padding:8px 0}.ribbon .container{display:flex;justify-content:space-between;gap:24px}.ribbon a{color:#9bb5ff}.nav{position:sticky;top:0;z-index:10;background:var(--paper);border-bottom:1px solid var(--ink)}.nav-row{height:60px;display:flex;align-items:center;gap:28px}.brand{display:flex;align-items:center;gap:10px;font-weight:700;letter-spacing:-.02em}.mark{width:24px;height:24px;border-radius:6px;background:var(--accent);color:#fff;display:grid;place-items:center;font-family:monospace}.navlinks{display:flex;gap:24px;color:var(--ink2);font-size:14px}.navlinks a:hover{color:var(--accent)}.nav-cta{margin-left:auto;display:flex;gap:10px}.btn{display:inline-flex;align-items:center;justify-content:center;gap:8px;padding:10px 16px;border:1px solid var(--ink);border-radius:8px;font-weight:600;font-size:14px;background:var(--paper);color:var(--ink)}.btn:hover{background:var(--ink);color:var(--paper)}.btn.accent{background:var(--accent);border-color:var(--accent);color:#fff}.btn.accent:hover{background:var(--ink);border-color:var(--ink)}.btn.ghost{border-color:transparent;color:var(--ink2)}.eyebrow{font:700 11px/1.2 ui-monospace,Menlo,monospace;letter-spacing:.16em;text-transform:uppercase;color:var(--accent)}.kicker{display:inline-flex;align-items:center;gap:8px;font:700 11px/1.2 ui-monospace,Menlo,monospace;text-transform:uppercase;letter-spacing:.12em;color:var(--ink2)}.dot{width:6px;height:6px;border-radius:50%;background:var(--accent)}.hero{padding:64px 0 0;border-bottom:1px solid var(--ink)}.hero-grid{display:grid;grid-template-columns:.95fr 1.05fr;gap:48px;align-items:center;padding-bottom:64px}.hero h1{font-size:76px;line-height:.96;letter-spacing:-.045em;margin:18px 0 22px}.hero h1 em,.sec-head h2 em,.card h3 em,.final h2 em{font-family:Georgia,serif;font-style:italic;font-weight:400;color:var(--accent)}.hero p{font-size:18px;line-height:1.5;color:var(--ink2);max-width:540px}.hero-cta,.final-cta{display:flex;gap:12px;flex-wrap:wrap}.hero-meta{display:flex;gap:20px;flex-wrap:wrap;margin-top:24px;font:500 12px/1.2 ui-monospace,Menlo,monospace;color:var(--ink3)}.hero-meta span:before{content:'● ';color:var(--accent)}.demo{background:var(--paper);border:1px solid var(--ink);border-radius:14px;overflow:hidden;height:560px;display:grid;grid-template-columns:1fr 1fr;box-shadow:0 24px 48px -24px rgba(12,12,10,.18)}.demo-col{display:flex;flex-direction:column;min-width:0}.demo-col+.demo-col{border-left:1px solid var(--ink)}.demo-hd{display:flex;justify-content:space-between;align-items:center;gap:14px;padding:11px 14px;border-bottom:1px solid var(--ink);font:700 11px/1.2 ui-monospace,Menlo,monospace;text-transform:uppercase;letter-spacing:.06em;background:var(--paper2);color:var(--ink2)}.demo-hd span:first-child{min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.live{color:var(--accent);white-space:nowrap}.chat{flex:1;overflow:hidden;padding:18px;display:flex;flex-direction:column;gap:10px;background:linear-gradient(180deg,#fff 0%,var(--paper) 100%)}.msg{max-width:88%;font-size:14px;line-height:1.45;animation:slide .35s ease-out;color:var(--ink)}.who{display:block;font:700 10px/1.2 ui-monospace,Menlo,monospace;letter-spacing:.1em;text-transform:uppercase;color:#5f5b54;margin-bottom:4px}.body{background:#fff;border:1px solid #d8d3c7;padding:10px 13px;border-radius:10px;box-shadow:0 1px 0 rgba(12,12,10,.03)}.user{align-self:flex-end;text-align:right}.user .body{background:var(--ink);color:var(--paper);border-color:var(--ink)}.system{align-self:center}.system .body{background:transparent;border:1px dashed rgba(12,12,10,.25);font:700 11px/1.2 ui-monospace,Menlo,monospace;color:var(--ink3);border-radius:999px}.approve-card{background:#fff;border:1px solid var(--ink);border-radius:12px;overflow:hidden;box-shadow:0 8px 24px -12px rgba(12,12,10,.25);text-align:left}.approve-card .ah{padding:9px 13px;border-bottom:1px solid var(--ink);display:flex;justify-content:space-between;background:var(--accent);color:#fff;font:700 10px/1.2 ui-monospace,Menlo,monospace;text-transform:uppercase}.approve-card .ab{padding:12px 13px}.approve-card .row{display:flex;justify-content:space-between;padding:4px 0;font:500 12px/1.3 ui-monospace,Menlo,monospace;color:var(--ink2);gap:12px}.approve-card .actions{display:flex;border-top:1px solid var(--paper3)}.approve-card button{flex:1;padding:11px;border:0;background:#fff;font:700 11px/1.2 ui-monospace,Menlo,monospace;text-transform:uppercase}.approve-card .yes{background:var(--ink);color:#fff}.ledger{flex:1;display:flex;flex-direction:column;font:500 12px/1.2 ui-monospace,Menlo,monospace}.ledger-meta{display:grid;grid-template-columns:repeat(3,1fr);border-bottom:1px solid var(--ink)}.ledger-meta>div{padding:11px 14px}.lbl{font-size:10px;letter-spacing:.08em;text-transform:uppercase;color:var(--ink3);margin-bottom:4px}.val{font:600 18px/1 Inter,sans-serif}.ledger-list{flex:1;overflow:hidden}.ledger-row{display:grid;grid-template-columns:54px 1fr auto;gap:10px;padding:10px 14px;border-bottom:1px solid var(--paper3);animation:slide .35s ease-out}.badge{font-size:9.5px;letter-spacing:.1em;text-transform:uppercase;border:1px solid currentColor;padding:2px 6px;border-radius:4px}.replay{display:flex;align-items:center;gap:14px;padding:9px 14px;border-top:1px solid var(--ink);background:var(--paper2);font:700 11px/1.2 ui-monospace,Menlo,monospace}.replay button{background:none;border:0;font:inherit;cursor:pointer}.bar{flex:1;height:3px;background:var(--paper3);border-radius:99px;overflow:hidden}.bar-fill{height:100%;background:var(--accent);transform-origin:left}.sc-dot{width:18px;height:6px;border:0;border-radius:99px;background:var(--paper3);margin-left:6px}.sc-dot.active{background:var(--accent)}.stats{display:grid;grid-template-columns:repeat(4,1fr);border-bottom:1px solid var(--ink)}.stats>div{padding:28px 32px;border-left:1px solid var(--paper3)}.stats>div:first-child{border-left:0}.stats .n{font-size:44px;letter-spacing:-.03em;font-weight:600}.stats .l{font:700 11px/1.3 ui-monospace,Menlo,monospace;letter-spacing:.08em;text-transform:uppercase;color:var(--ink3);margin-top:10px}section{padding:104px 0;border-bottom:1px solid var(--ink)}.sec-head{display:flex;justify-content:space-between;align-items:flex-end;margin-bottom:56px;gap:32px}.sec-head h2{font-size:56px;line-height:1;letter-spacing:-.035em;margin:14px 0 0;max-width:800px}.meta{font:700 11px/1.45 ui-monospace,Menlo,monospace;letter-spacing:.06em;text-transform:uppercase;color:var(--ink3);text-align:right}.steps{display:grid;grid-template-columns:1fr 1fr;gap:16px;border:0;max-width:1200px;margin:0 auto;padding:0 32px;width:100%}.step{position:relative;display:grid;grid-template-columns:58px 1fr;grid-template-rows:auto auto 1fr;gap:8px 18px;min-height:0;padding:22px;border:1px solid var(--paper3);border-left:4px solid var(--accent);border-radius:22px;background:linear-gradient(135deg,rgba(255,255,255,.78),rgba(250,250,247,.2));box-shadow:0 18px 40px rgba(12,12,10,.05)}.step .num{grid-row:1/4;width:44px;height:44px;border-radius:999px;background:var(--ink);color:var(--paper);display:grid;place-items:center;letter-spacing:.04em}.step h3{font-size:23px;line-height:1.08;letter-spacing:-.02em;margin:0}.step p{font-size:14.5px;line-height:1.55;color:var(--ink2);margin:0}.step .detail{margin-top:8px;border-top:1px dashed var(--paper3);padding-top:12px;font:500 11px/1.4 ui-monospace,Menlo,monospace;color:var(--ink3)}.wedge-grid{display:grid;grid-template-columns:repeat(3,1fr);border-top:1px solid var(--ink);border-bottom:1px solid var(--ink)}.wedge{padding:30px 26px;min-height:285px;border-left:1px solid var(--paper3);display:flex;flex-direction:column;gap:12px}.wedge:first-child{border-left:0}.tag{font:700 11px/1.2 ui-monospace,Menlo,monospace;letter-spacing:.1em;color:var(--accent);text-transform:uppercase}.wedge h3,.int-card h3{font-size:28px;line-height:1.05;letter-spacing:-.025em;margin:0}.wedge p,.int-card p,.trust-list p{font-size:14.5px;line-height:1.55;color:var(--ink2);margin:0}.vs{margin-top:auto;border-top:1px dashed var(--paper3);padding-top:14px;font:500 11px/1.4 ui-monospace,Menlo,monospace;color:var(--ink3)}.integrate-grid{display:grid;grid-template-columns:1fr 1fr;gap:24px}.int-card,.receipt,.compare{background:#fff;border:1px solid var(--ink);border-radius:14px;box-shadow:0 24px 48px -24px rgba(12,12,10,.18);overflow:hidden}.int-card{padding:32px;display:flex;flex-direction:column;gap:14px}.int-prompt,.int-cred{border:1px solid var(--paper3);border-radius:10px;background:var(--paper);padding:14px 16px}.int-prompt-hd{margin:-14px -16px 12px;padding:8px 14px;border-bottom:1px solid var(--paper3);background:var(--paper2);font:700 10px/1.2 ui-monospace,Menlo,monospace;text-transform:uppercase;color:var(--ink3)}.int-list{list-style:none;padding:0;margin:4px 0 0;font:500 12px/1.6 ui-monospace,Menlo,monospace;color:var(--ink3)}.int-list li:before{content:'› ';color:var(--accent)}.int-cred .row{display:flex;justify-content:space-between;gap:16px;padding:3px 0;font:500 12.5px/1.35 ui-monospace,Menlo,monospace;color:var(--ink3)}.int-cred .row b{color:var(--ink);font-weight:700;text-align:right}.chat .body{color:var(--ink)!important;background:#fffefb!important;border-color:#8f8678!important}.chat .msg{opacity:1;animation:none!important}.chat .system .body{color:var(--ink)!important;background:#fffefb!important;border-color:#8f8678!important}.approve-card,.approve-card *{color:var(--ink)}.approve-card{background:#fffefb!important}.approve-card .row{color:var(--ink)!important}.approve-card .ah,.approve-card .ah *,.approve-card .yes{color:#fff!important}.ledger-row,.ledger-row *{color:var(--ink)!important;animation:none!important}.ledger-row div[style]{color:var(--ink)!important}.trust-grid{display:grid;grid-template-columns:1.1fr .9fr;gap:64px;align-items:center}.trust-list{list-style:none;margin:0;padding:0;border-top:1px solid var(--paper3)}.trust-list li{display:grid;grid-template-columns:auto 1fr;gap:18px;padding:22px 0;border-bottom:1px solid var(--paper3)}.receipt .rh{background:var(--ink);color:var(--paper);padding:14px 18px;display:flex;justify-content:space-between;font:700 11px/1.2 ui-monospace,Menlo,monospace;text-transform:uppercase}.receipt .rb{padding:18px 20px;font:500 12.5px/1.7 ui-monospace,Menlo,monospace;color:var(--ink2)}.receipt .row{display:flex;justify-content:space-between;gap:12px}.receipt b{color:var(--ink)}.stamp{display:inline-flex;margin-top:8px;color:var(--ok);border:1px solid var(--ok);border-radius:999px;padding:3px 8px;font-size:10px;text-transform:uppercase}.compare table{width:100%;border-collapse:collapse;font:500 13px/1.35 ui-monospace,Menlo,monospace}.compare th,.compare td{padding:14px 18px;text-align:left;border-bottom:1px solid var(--paper3);border-right:1px solid var(--paper3)}.compare th{background:var(--paper2);text-transform:uppercase;font-size:11px}.compare .payjent{background:var(--accent);color:#fff}.yes{color:var(--ok);font-weight:700}.no{color:var(--ink3)}.partial{color:var(--warn)}.final{padding:140px 0;text-align:center;background:var(--ink);color:var(--paper)}.final h2{font-size:96px;line-height:.95;letter-spacing:-.045em;margin:18px 0 28px}.final p{font-size:17px;color:rgba(250,250,247,.7);margin:0 auto 40px;max-width:590px}.final-cta{justify-content:center}.final .btn{background:var(--paper);color:var(--ink);border-color:var(--paper)}.final .btn.ghost{background:transparent;color:var(--paper);border-color:rgba(250,250,247,.25)}footer{padding:40px 0;background:var(--ink);color:rgba(250,250,247,.55);font-size:13px}.foot-row{display:flex;justify-content:space-between;gap:24px}.typing-dots:after{content:'…';animation:dots 1.2s steps(4,end) infinite}@keyframes slide{from{opacity:0;transform:translateY(6px)}to{opacity:1;transform:none}}@keyframes dots{0%{content:''}25%{content:'.'}50%{content:'..'}75%,100%{content:'...'}}@media (prefers-reduced-motion:reduce){*,*:before,*:after{animation:none!important;transition:none!important;scroll-behavior:auto!important}}@media(max-width:900px){.hero-grid,.trust-grid,.integrate-grid{grid-template-columns:1fr}.stats,.steps,.wedge-grid{grid-template-columns:1fr}.navlinks{display:none}.demo{grid-template-columns:1fr;height:auto}.demo-col+.demo-col{border-left:0;border-top:1px solid var(--ink)}}
</style></head><body><div class='ribbon'><div class='container'><span>Payjent · the payment gate for agent work</span><span>Private beta · <a href='__PRIMARY__'>request access →</a></span></div></div><nav class='nav'><div class='container nav-row'><a class='brand' href='/'><span class='mark'>P</span><span>payjent</span></a><div class='navlinks'><a href='#how'>How it works</a><a href='#wedge'>Why Payjent</a><a href='#trust'>Trust &amp; safety</a><a href='#integrate'>Integrate</a><a href='#compare'>Compare</a><a href='/demo'>Demo</a><a href='/dashboard'>Dashboard</a></div><div class='nav-cta'><a class='btn ghost' href='/dashboard'>Sign in</a><a class='btn accent' href='__PRIMARY__'>Register your agent →</a></div></div></nav><main><header class='hero'><div class='container hero-grid'><div><span class='kicker'><span class='dot'></span>Payment-gate agent actions · Human-in-the-loop payments for agents</span><h1>The <em>payment gate</em> for agent work.</h1><p>Payjent lets your agent ask a person to approve and pay for premium work — once, at the right moment — then resumes the exact action with a receipt-backed ledger.</p><div class='hero-cta'><a class='btn accent' href='__PRIMARY__'>Register your agent →</a><a class='btn' href='/docs/agent-payjent-self-setup.md'>Send/read setup guide</a></div><div class='hero-meta'><span>Request-bound approvals</span><span>Human approval before resume</span><span>Rail-aware: Stripe, x402, custom</span></div></div><div class='demo' aria-label='Live Payjent chat and ledger demo'><div class='demo-col'><div class='demo-hd'><span id='demo-agent'>Atlas / research · @erik</span><span class='live'>● Live</span></div><div class='chat' id='demo-chat'><div class='msg system'><div class='body typing-dots'>Preparing scenario</div></div></div></div><div class='demo-col'><div class='demo-hd'><span id='demo-ledger-title'>Live ledger · research.payjent</span><span id='demo-total'>$0.00</span></div><div class='ledger'><div class='ledger-meta'><div><div class='lbl'>Approvals</div><div class='val' id='demo-grants'>0</div></div><div><div class='lbl'>Captured</div><div class='val' id='demo-captured'>$0.00</div></div><div><div class='lbl'>Pending</div><div class='val' id='demo-pending'>—</div></div></div><div class='ledger-list' id='demo-ledger'><div style='padding:24px 14px;color:var(--ink3);font-size:11px;text-transform:uppercase'>Awaiting first event…</div></div><div class='replay'><button id='pauseBtn'>❚❚ Pause</button><button id='replayBtn'>↺ Replay</button><div class='bar'><div class='bar-fill' id='barFill'></div></div><span id='dots'></span></div></div></div></div></div><div class='stats'><div><div class='n'>4</div><div class='l'>Payment lifecycle primitives</div></div><div><div class='n'>1×</div><div class='l'>Use per approval. No replay.</div></div><div><div class='n'>0</div><div class='l'>Credentials in the chat window</div></div><div><div class='n'>3</div><div class='l'>Rail families Payjent can record</div></div></div></header><section id='how'><div class='container'><div class='sec-head'><div><span class='eyebrow'>How it works</span><h2>Quote, <em>approve</em>, grant, capture.</h2></div><div class='meta'>Four primitives.<br>One human checkpoint.<br>Idempotent end-to-end.</div></div></div><div class='steps'><div class='step'><div class='num'>01</div><h3>Agent quotes the work</h3><p>Your agent tells Payjent what action it wants to take and at what price. Payjent binds the quote to the exact request and execution envelope.</p><div class='detail'>Bound to action · vendor · amount · prompt hash</div></div><div class='step'><div class='num'>02</div><h3>Human approves once</h3><p>Payjent presents an approval card over the chat surface, email, Slack, or webhook. A person approves a specific amount for a specific request.</p><div class='detail'>Delivered in chat · Slack · email · webhook</div></div><div class='step'><div class='num'>03</div><h3>Single-use access issued</h3><p>Approval creates a request-bound, single-use authorization. It only unlocks the original action — never replayed or re-scoped.</p><div class='detail'>uses = 1 · short TTL · request-hash bound</div></div><div class='step'><div class='num'>04</div><h3>Action resumes, receipt logged</h3><p>The agent resumes the exact action it quoted. Payjent records the checkpoint and ledger trail; it does not execute downstream pay.sh.</p><div class='detail'>Receipt · reason trail · auditable ledger</div></div></div></section><section id='wedge'><div class='container'><div class='sec-head'><div><span class='eyebrow'>Why Payjent</span><h2>The wedge is <em>where money meets intent.</em></h2></div><div class='meta'>Three things competitors<br>structurally don't optimize for.</div></div></div><div class='wedge-grid'><div class='wedge'><span class='tag'>Wedge · 01</span><h3>The <em>checkpoint</em> agents never had.</h3><p>Other agent payment stories start with a budget and assume the agent may spend. Payjent treats payment as a deliberate human checkpoint bound to one action and one amount.</p><div class='vs'>Not auto-spend with a budget → approval bound to intent</div></div><div class='wedge'><span class='tag'>Wedge · 02</span><h3>One registration. <em>Many premium actions.</em></h3><p>Register an agent once, point it at the machine-readable setup guide, and route paid actions through one approval and receipt system instead of scattering vendor secrets.</p><div class='vs'>Not per-vendor secret sprawl → one governed agent identity</div></div><div class='wedge'><span class='tag'>Wedge · 03</span><h3>Receipts that <em>survive an audit.</em></h3><p>Each spend record keeps the prompt, quote, human decision, payment session, capture, and fulfillment evidence together so finance can see why money moved.</p><div class='vs'>Not ledger lines without context → reason-backed lifecycle records</div></div></div></section><section id='trust'><div class='container'><div class='sec-head'><div><span class='eyebrow'>Trust &amp; safety</span><h2>Designed so <em>nothing surprising</em> ever leaves your account.</h2></div><div class='meta'>Conservative defaults.<br>Auditable by construction.</div></div><div class='trust-grid'><ul class='trust-list'><li><div class='num'>01</div><div><h4>Request-bound approvals</h4><p>Approval is tied to the exact action envelope. Re-quoting requires re-approval.</p></div></li><li><div class='num'>02</div><div><h4>Single-use by default</h4><p>Access is minted with one use and short TTLs. Replay attempts fail closed.</p></div></li><li><div class='num'>03</div><div><h4>Per-agent caps &amp; policies</h4><p>Daily caps, vendor blocks, category policies, and risk checks communicate or enforce conservative limits.</p></div></li><li><div class='num'>04</div><div><h4>Credentials shown once</h4><p>Bot keys are revealed exactly once at registration, then hidden from dashboards, logs, and support flows.</p></div></li></ul><div class='receipt'><div class='rh'><span>receipt · ec5b</span><span>verified</span></div><div class='rb'><div class='row'><span>request</span><b>req 8af2</b></div><div class='row'><span>authorization</span><b>single-use · hidden</b></div><div class='row'><span>agent</span><b>atlas / research</b></div><div class='row'><span>approver</span><b>@erik</b></div><div class='row'><span>vendor</span><b>iea.org</b></div><div class='row'><span>item</span><b>WEO 2025 (PDF)</b></div><hr><div class='row'><span>quoted</span><b>$12.50</b></div><div class='row'><span>captured</span><b>$12.50</b></div><hr><div class='row'><span>total</span><b style='font-size:24px'>$12.50</b></div><span class='stamp'>● paid · single-use</span></div></div></div></div></section><section id='integrate'><div class='container'><div class='sec-head'><div><span class='eyebrow'>Integrate</span><h2>Two steps. <em>No SDK snippets on the landing page.</em></h2></div><div class='meta'>Point your agent at us.<br>Register it in the dashboard.</div></div><div class='integrate-grid'><div class='int-card'><div class='num'>01</div><h3>Point your agent at <em>the setup contract</em></h3><p>Tell Claude, Cursor, Copilot, or your own agent to integrate with Payjent and give it the canonical setup path. The agent reads the contract and wires itself up.</p><div class='int-prompt'><div class='int-prompt-hd'>prompt your agent</div>Integrate with Payjent for paid actions. Read /docs/agent-payjent-self-setup.md or /.well-known/payjent-agent-setup and wire up quote, approval, and capture.</div><ul class='int-list'><li>Works with any agent framework</li><li>Machine-readable self-setup endpoint</li><li>No raw tokens on public pages</li></ul></div><div class='int-card'><div class='num'>02</div><h3>Register the agent in your <em>dashboard</em></h3><p>Mint a credential bound to one agent identity. Drop it into your agent secret store. From then on, paid actions flow through the human approval gate and ledger.</p><div class='int-cred'><div class='row'><span>agent</span><b>atlas-research</b></div><div class='row'><span>credential</span><b>shown once · never again</b></div><div class='row'><span>scope</span><b>workspace · USD · daily cap</b></div></div><a class='btn accent' href='__PRIMARY__' style='margin-top:18px'>Open dashboard →</a></div></div></div></section><section id='compare'><div class='container'><div class='sec-head'><div><span class='eyebrow'>Compare</span><h2>Same problem, <em>different shape.</em></h2></div><div class='meta'>Honest read on<br>what each is built for.</div></div><div class='compare'><table><thead><tr><th></th><th class='payjent'>Payjent</th><th>Stripe Agent SDK</th><th>x402 / buildx402</th><th>pay.sh</th><th>DIY</th></tr></thead><tbody><tr><td>Human approval at the moment of payment</td><td><span class='yes'>● native</span></td><td><span class='no'>○ no</span></td><td><span class='no'>○ no</span></td><td><span class='partial'>◐ partial</span></td><td><span class='no'>○ build it</span></td></tr><tr><td>Request-bound, single-use approvals</td><td><span class='yes'>● native</span></td><td><span class='no'>○ no</span></td><td><span class='partial'>◐ partial</span></td><td><span class='no'>○ no</span></td><td><span class='no'>○ build it</span></td></tr><tr><td>Reason-backed ledger (prompt → receipt)</td><td><span class='yes'>● native</span></td><td><span class='no'>○ no</span></td><td><span class='no'>○ no</span></td><td><span class='no'>○ no</span></td><td><span class='no'>○ build it</span></td></tr><tr><td>Works across Stripe, x402, and custom rails</td><td><span class='yes'>● rail-aware</span></td><td><span class='partial'>◐ one rail</span></td><td><span class='partial'>◐ one rail</span></td><td><span class='partial'>◐ one rail</span></td><td><span class='partial'>◐ maybe</span></td></tr><tr><td>Per-agent caps, vendor blocks, daily limits</td><td><span class='yes'>● native</span></td><td><span class='partial'>◐ partial</span></td><td><span class='no'>○ no</span></td><td><span class='partial'>◐ partial</span></td><td><span class='no'>○ build it</span></td></tr></tbody></table></div><p class='mono' style='font-size:12.5px;color:var(--ink3);letter-spacing:.04em'>※ Stripe, x402, pay.sh and others ship useful primitives — Payjent is the human checkpoint and receipt layer around paid agent work. Payjent does not execute downstream pay.sh.</p></div></section><section id='register' class='final'><div class='container'><span class='eyebrow' style='color:rgba(250,250,247,.55)'>Ready to ship</span><h2>Register an <em>agent.</em></h2><p>Two minutes. One credential. Shown exactly once. Drop it into your agent secret store and Payjent will gate the next paid action.</p><div class='final-cta'><a class='btn' href='__PRIMARY__'>Register an agent →</a><a class='btn ghost' href='/docs/agent-payjent-self-setup.md'>Read the docs</a></div><div class='mono' style='margin-top:48px;display:flex;gap:32px;justify-content:center;font-size:11px;letter-spacing:.08em;text-transform:uppercase;color:rgba(250,250,247,.5)'><span>● Sandbox mode available</span><span>● No public tokens</span><span>● Stripe Connect or x402 underneath</span></div></div></section></main><footer><div class='container foot-row'><div><span class='mark' style='display:inline-grid;vertical-align:middle;margin-right:10px'>P</span>© 2026 Payjent · Built for agents that pay.</div><div><a href='/docs/agent-payjent-self-setup.md'>Docs</a> · <a href='/.well-known/payjent-agent-setup'>Agent setup</a> · <a href='/dashboard'>Dashboard</a></div></div></footer><script>
const scenarios=[{id:'research',agent:'Atlas / research',user:'@erik',steps:[['msg','user','Find me the 2025 IEA energy outlook PDF and summarize chapter 4.'],['msg','agent','Found it on the IEA store. It is a paywalled PDF — $12.50. I will request payment approval before purchase.'],['ledger','quote','iea.org · world-energy-outlook-2025.pdf','$12.50','pending','QUOTE'],['msg','system','Payjent approval requested · waiting on @erik'],['approve','iea.org','World Energy Outlook 2025 (PDF)','$12.50','$50.00 / day'],['ledger','auth','single-use approval · request-bound','$12.50','captured','APPROVED'],['msg','user','approved'],['msg','agent','Paid. Downloading now…'],['ledger','capture','iea.org · receipt #ec5b','$12.50','captured','PAID'],['msg','agent','Done. Chapter 4 covers grid-scale storage; here is the summary…']]},{id:'travel',agent:'Lark / travel',user:'@erik',steps:[['msg','user','Book the 7am SFO→JFK on Friday, aisle seat under $480.'],['msg','agent','Held UA 286: $462, 14C. Asking you to approve before I confirm.'],['ledger','quote','united.com · UA 286 SFO→JFK','$462.00','pending','HOLD'],['msg','system','Payjent approval requested · spend cap $1,500 / wk'],['approve','united.com','UA 286 · 1× economy · 14C','$462.00','$1,500 / wk'],['ledger','auth','single-use approval · request-bound','$462.00','captured','APPROVED'],['msg','user','go'],['msg','agent','Confirmed. Conf code XQ7P2K. Itinerary in your inbox.'],['ledger','capture','united.com · ticket XQ7P2K','$462.00','captured','PAID']]},{id:'credits',agent:'Hex / dev',user:'@erik',steps:[['msg','user','Run the full eval suite against the new model.'],['msg','agent','That will cost about $28 in OpenAI credits and $4.20 in Anthropic. Bundling both via Payjent.'],['ledger','quote','openai.com · credits','$28.00','pending','QUOTE'],['ledger','quote','anthropic.com · credits','$4.20','pending','QUOTE'],['msg','system','Payjent approval requested · 2 line items · $32.20 total'],['approve','2 vendors · bundled','OpenAI + Anthropic credits','$32.20','$200 / day'],['ledger','auth','multi-vendor approval · hidden','$32.20','captured','APPROVED'],['msg','user','yep'],['msg','agent','Eval running. ETA 6 minutes.'],['ledger','capture','openai.com · receipt','$28.00','captured','PAID'],['ledger','capture','anthropic.com · receipt','$4.20','captured','PAID']]}];
let si=0,tick=0,paused=false,timer=null,transitionTimer=null;const times=[200,1100,2000,2700,3400,5400,5700,6300,7000,7700];const chat=document.getElementById('demo-chat'),ledger=document.getElementById('demo-ledger');function money(s){return parseFloat(s.replace(/[^0-9.]/g,''))||0}function render(){const sc=scenarios[si],visible=sc.steps.filter((_,i)=>times[i]<=tick);document.getElementById('demo-agent').textContent=sc.agent+' · '+sc.user;document.getElementById('demo-ledger-title').textContent='Live ledger · '+sc.id+'.payjent';chat.innerHTML='';ledger.innerHTML='';let captured=0,pending=0,approvals=0;visible.forEach((st,i)=>{if(st[0]=='msg'){chat.insertAdjacentHTML('beforeend',`<div class='msg ${st[1]}'><span class='who'>${st[1]=='user'?sc.user:st[1]=='agent'?sc.agent:'system'}</span><div class='body'>${st[2]}</div></div>`)}else if(st[0]=='approve'){chat.insertAdjacentHTML('beforeend',`<div class='msg'><span class='who'>Payjent</span><div class='approve-card'><div class='ah'><span>Payjent · approve payment</span><span>req 8af2</span></div><div class='ab'><div class='row'><span>Merchant</span><b>${st[1]}</b></div><div class='row'><span>Item</span><b>${st[2]}</b></div><div class='row'><span>Amount</span><b>${st[3]}</b></div><div class='row'><span>Within cap</span><b>${st[4]}</b></div></div><div class='actions'><button>Decline</button><button class='yes'>Approve once</button></div></div></div>`)}else{if(st[1]=='capture')captured+=money(st[3]);if(st[1]=='quote')pending+=money(st[3]);if(st[1]=='auth')approvals++;ledger.insertAdjacentHTML('beforeend',`<div class='ledger-row'><div>0${Math.floor(times[i]/1000)}:${String(times[i]%1000).padStart(3,'0').slice(0,2)}</div><div><div>${st[2]}</div><div style='color:var(--ink3)'>${st[1]} · <span class='badge'>${st[5]}</span></div></div><div>${st[3]}</div></div>`)}});if(!visible.some(x=>x[0]=='ledger'))ledger.innerHTML="<div style='padding:24px 14px;color:var(--ink3);font-size:11px;text-transform:uppercase'>Awaiting first event…</div>";document.getElementById('demo-grants').textContent=approvals;document.getElementById('demo-captured').textContent='$'+captured.toFixed(2);document.getElementById('demo-pending').textContent=pending?'$'+pending.toFixed(2):'—';document.getElementById('demo-total').textContent='$'+(captured+pending).toFixed(2);document.getElementById('barFill').style.transform='scaleX('+Math.min(1,tick/9500)+')';chat.scrollTop=chat.scrollHeight}function reset(n){if(transitionTimer){clearTimeout(transitionTimer);transitionTimer=null}si=n;tick=0;render();document.querySelectorAll('.sc-dot').forEach((d,i)=>d.classList.toggle('active',i==si))}function queueNextScenario(){if(transitionTimer)return;transitionTimer=setTimeout(()=>{transitionTimer=null;reset((si+1)%scenarios.length)},350)}function loop(){if(!paused){tick+=250;if(tick>9500)queueNextScenario();render()}}document.getElementById('pauseBtn').onclick=()=>{paused=!paused;document.getElementById('pauseBtn').textContent=paused?'▶ Play':'❚❚ Pause'};document.getElementById('replayBtn').onclick=()=>reset(si);document.getElementById('dots').innerHTML=scenarios.map((s,i)=>`<button class='sc-dot ${i==0?'active':''}' title='${s.id}' onclick='reset(${i})'></button>`).join('');if('IntersectionObserver'in window){new IntersectionObserver(()=>{},{}).observe(document.body)}timer=setInterval(loop,250);render();
</script></body></html>""".replace("__PRIMARY__", primary)

def _marketing_css() -> str:
    return """<style>
:root{--bg:#f7f5ef;--panel:#fffefa;--ink:#11110f;--muted:#69665f;--line:#dedad0;--accent:#2457ff;--accent2:#0d9f6e;--soft:#eef2ff}*{box-sizing:border-box}html,body{margin:0;max-width:100%;overflow-x:hidden}body{background:radial-gradient(circle at 20% 0,#fff 0,#f7f5ef 34%,#f1eee5 100%);color:var(--ink);font-family:Inter,ui-sans-serif,system-ui,-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;-webkit-font-smoothing:antialiased}a{color:inherit;text-decoration:none}.container{width:min(1120px,calc(100% - 40px));margin:0 auto}.nav{position:sticky;top:0;z-index:5;background:rgba(247,245,239,.82);backdrop-filter:blur(18px);border-bottom:1px solid var(--line)}.nav-row{height:64px;display:flex;align-items:center;gap:22px}.brand{display:flex;align-items:center;gap:10px;font-weight:760;letter-spacing:-.03em}.mark{width:28px;height:28px;border-radius:9px;background:#11110f;color:#fff;display:grid;place-items:center}.navlinks{display:flex;gap:18px;margin-left:auto;color:var(--muted);font-size:14px}.btn{display:inline-flex;align-items:center;justify-content:center;border:1px solid var(--ink);border-radius:999px;padding:11px 16px;font-weight:650;font-size:14px;background:transparent}.btn.primary{background:var(--ink);color:#fff}.btn.blue{background:var(--accent);border-color:var(--accent);color:#fff}.hero{padding:88px 0 54px}.eyebrow{font:750 11px/1 ui-monospace,SFMono-Regular,Menlo,monospace;letter-spacing:.14em;text-transform:uppercase;color:var(--accent)}h1{font-size:clamp(46px,8vw,88px);line-height:.94;letter-spacing:-.07em;margin:18px 0;max-width:920px}h2{font-size:clamp(28px,4vw,46px);letter-spacing:-.045em;line-height:1;margin:0 0 12px}h3{margin:0 0 8px;font-size:18px;letter-spacing:-.02em}p{color:var(--muted);font-size:18px;line-height:1.55}.lead{font-size:clamp(19px,2vw,24px);max-width:760px}.actions{display:flex;gap:12px;flex-wrap:wrap;margin-top:28px}.grid{display:grid;grid-template-columns:repeat(3,1fr);gap:16px;margin:28px 0}.card{background:rgba(255,254,250,.76);border:1px solid var(--line);border-radius:24px;padding:24px;box-shadow:0 20px 70px rgba(17,17,15,.06)}.story{display:grid;grid-template-columns:1fr 1fr;gap:18px;align-items:stretch;margin:42px auto}.phone{background:#101113;color:#fff;border-radius:34px;padding:18px;box-shadow:0 28px 90px rgba(17,17,15,.24)}.bubble{padding:13px 15px;border-radius:18px;margin:10px 0;max-width:88%;font-size:14px;line-height:1.35}.user{background:#2457ff;margin-left:auto}.agent{background:#26282d}.payjent{background:#fff;color:#111;border:1px solid #d9d9d9}.artifact{margin-top:14px;border-radius:22px;padding:16px;background:linear-gradient(135deg,#f8fafc,#dbeafe 34%,#111827 35%,#111827 100%);color:#fff;min-height:170px;display:flex;flex-direction:column;justify-content:space-between;overflow:hidden}.artifact-art{height:86px;border-radius:18px;background:radial-gradient(circle at 28% 38%,#93c5fd 0 12%,transparent 13%),radial-gradient(circle at 70% 32%,#fbbf24 0 9%,transparent 10%),linear-gradient(135deg,#1d4ed8,#0f172a 60%,#020617);box-shadow:inset 0 0 0 1px rgba(255,255,255,.14)}.artifact-caption{font-size:13px;color:#dbeafe}.ledger-row{display:flex;justify-content:space-between;gap:18px;padding:14px 0;border-bottom:1px solid var(--line);color:var(--muted)}.ledger-row b{color:var(--ink)}.pill{display:inline-flex;border:1px solid var(--line);border-radius:999px;padding:6px 10px;color:var(--muted);font-size:12px;font-weight:700}.section{padding:54px 0}.steps{counter-reset:s;display:grid;grid-template-columns:repeat(4,1fr);gap:14px}.step:before{counter-increment:s;content:counter(s);display:grid;place-items:center;width:30px;height:30px;border-radius:50%;background:var(--soft);color:var(--accent);font-weight:800;margin-bottom:16px}.footer{padding:52px 0;color:var(--muted);border-top:1px solid var(--line)}@media(max-width:820px){.navlinks{display:none}.hero{padding-top:52px}.story,.grid,.steps{grid-template-columns:1fr}h1{font-size:48px}.container{width:min(100% - 28px,1120px)}}
</style>"""


def _demo_page_html(settings: Settings) -> str:
    primary = _primary_cta(settings)
    return f"""<!doctype html><html lang='en'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><title>Payjent hackathon demo</title><meta name='description' content='Hackathon demo: user asks an agent, Payjent gates approval, Decal task budget readiness and FAL execution produce a result artifact.'>{_marketing_css()}</head><body><nav class='nav'><div class='container nav-row'><a class='brand' href='/'><span class='mark'>P</span><span>payjent</span></a><div class='navlinks'><a href='/'>Home</a><a href='/dashboard'>Dashboard</a></div><a class='btn primary' href='{primary}'>Register agent</a></div></nav><main><section class='hero'><div class='container'><div class='eyebrow'>Hackathon demo · FAL image action</div><h1>Ask the agent. Approve the spend. Receive the artifact.</h1><p class='lead'>This is Payjent's agent-commerce loop without public setup instructions or code snippets: the agent calculates runtime price, Payjent gets human approval, Decal/task-budget readiness is checked, then FAL executes and returns the image artifact.</p><div class='actions'><a class='btn blue' href='{primary}'>Register an agent</a><a class='btn' href='/dashboard'>Open dashboard</a></div></div></section><section class='container story'><div class='phone'><div class='bubble user'>Make a cinematic product image of a Payjent card terminal in a robot workshop.</div><div class='bubble agent'>FAL image generation is available. Estimated runtime price: $0.38. Your task budget is ready through Decal.</div><div class='bubble payjent'><b>Payjent approval</b><br>Provider: FAL · Action: image generation · Budget: $0.38 · Resume only this request.</div><div class='bubble user'>Approve once.</div><div class='bubble agent'>Payment checkpoint complete. Executing FAL now…</div><div class='bubble agent'>Done — artifact attached and receipt recorded.</div><div class='artifact'><div class='artifact-art' aria-hidden='true'></div><div><b>Returned artifact</b><div class='artifact-caption'>FAL image generation · receipt recorded · grant consumed once</div></div></div></div><div class='card'><span class='pill'>Live story board</span><h2>The path judges should see.</h2><div class='ledger-row'><b>1 User asks agent</b><span>natural language request</span></div><div class='ledger-row'><b>2 Agent prices runtime</b><span>FAL · $0.38 estimate</span></div><div class='ledger-row'><b>3 Payjent asks approval</b><span>human approves once</span></div><div class='ledger-row'><b>4 Decal/task budget ready</b><span>budget + payment checkpoint</span></div><div class='ledger-row'><b>5 Provider executes</b><span>FAL returns artifact</span></div></div></section><section class='section'><div class='container grid'><div class='card'><h3>Approval record</h3><p>Payjent stores the request summary, amount, budget, receipt state, and resumable action status.</p></div><div class='card'><h3>No raw secrets</h3><p>The dashboard uses Agent Install Links; credentials are not shown on public pages.</p></div><div class='card'><h3>Agent-readable setup</h3><p>Owners tell their agent to install Payjent and let it discover capabilities.</p></div></div></section></main><footer class='footer'><div class='container'>Payjent hackathon demo · <a href='/'>Home</a></div></footer></body></html>"""

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
    checkout_cta = ""
    if ps.provider == "mock" and ps.status != "paid" and settings.effective_mock_provider_enabled:
        checkout_cta = f"""<section><h2>Complete payment</h2><p>This checkout can be completed from the browser without exposing operator credentials, payment tokens, or raw grant IDs.</p><form method="post" action="/pay/{_html_escape(ps.id)}/mock-pay"><button class="btn" type="submit">Approve and pay {_html_escape(_format_money(q.amount_minor, q.currency))}</button></form><p class="fine">Payjent will issue a single-use grant for the exact stored request after approval.</p></section>"""
    elif ps.provider in {"stripe", "decal"} and ps.status != "paid" and ps.checkout_url and ps.checkout_url.startswith("https://"):
        provider_name = "Decal" if ps.provider == "decal" else "Stripe"
        checkout_cta = f"""<section><h2>Complete secure payment</h2><p>Continue to {provider_name} hosted checkout to pay securely. Payjent will resume the exact stored agent action after payment is confirmed.</p><p><a class="btn" href="{_html_escape(ps.checkout_url)}" rel="noopener noreferrer">Continue to secure payment</a></p><p class="fine">Payjent does not show raw grants, payment tokens, or credentials on this page.</p></section>"""
    return f"""<!doctype html><html><head><title>Payjent checkout · Approve paid agent action</title>{_DASHBOARD_CSS}</head><body><main><section class='hero'><div class='eyebrow'>Human approval document</div><h1>Approve this exact paid action?</h1><p class='muted'>Key question: should this agent resume this exact paid action after payment?</p></section><div class='grid'><div class='card'><h3>Agent request</h3><p>{_html_escape(q.request_summary)}</p><p class='fine'>External user: <code>{_html_escape(q.external_user_id)}</code><br>Request hash: <code>{_html_escape(q.request_hash)}</code></p></div><div class='card'><h3>Task budget</h3><div class='stat'>{_html_escape(_format_money(q.amount_minor, q.currency))}</div><p class='fine'>Spend control: this approval covers only the exact stored task budget below.</p><ul>{breakdown}</ul></div><div class='card'><h3>Execution readiness</h3><p><b>{_html_escape(status_words)}</b></p><p class='fine'>Payment session: <code>{_html_escape(ps.id)}</code><br>Payment state: {_html_escape(ps.status)}<br>Grant state: {_html_escape(_grant_state(grant))}</p></div><div class='card'><h3>Auto-resume</h3><p>{resumes}</p><p class='fine'>Approval creates a one-time grant bound to this stored request. Raw grant and payment tokens are not shown on this page.</p></div></div><section><h2>Approval terms</h2><ul><li>Human approval is required before Payjent marks this action ready.</li><li>The grant is single-use and tied to the exact request hash above.</li><li>Downstream rails may still impose their own authorization, settlement, availability, or rejection behavior; Payjent records the checkpoint and does not guarantee a third-party rail outcome.</li><li>Agent-side provider credentials or wallet runtime are required unless a provider connection is configured in Payjent.</li><li>If paid downstream execution fails, Payjent requests a refund by default unless the agent explicitly opts out.</li><li>Fulfillment events recorded so far: {len(fulfillment)}.</li></ul><p><a class='btn' href="/status/{_html_escape(ps.id)}">View status</a></p></section>{checkout_cta}</main></body></html>"""


@app.post("/pay/{payment_session_id}/mock-pay")
def browser_mock_pay(payment_session_id: str, session: Session = Depends(get_session), settings: Settings = Depends(get_settings)):
    if not settings.effective_mock_provider_enabled:
        raise HTTPException(404, "payment session not found")
    ps = session.get(PaymentSession, payment_session_id)
    if not ps or ps.provider != "mock":
        raise HTTPException(404, "payment session not found")
    if ps.status != "paid":
        q = session.get(Quote, ps.quote_id)
        if not q:
            raise HTTPException(404, "quote not found")
        complete_mock_payment(session, q, ps, settings.signing_secret, settings.grant_ttl_seconds)
        _deliver_agent_action_callback(session, q, ps, settings, "mock")
    return RedirectResponse(url=f"/pay/{ps.id}", status_code=303)


@app.get("/status", response_class=HTMLResponse)
def status_index():
    return f"""<!doctype html><html><head><title>Payjent status · Paid agent action</title>{_STATUS_PAGE_CSS}</head><body><main><div class='brand'><div class='logo'><span class='mark'>P</span><span>Payjent</span></div><span class='domain'>payjent.com</span></div><section class='hero'><div class='eyebrow'>Payment status</div><h1>Track a paid agent action.</h1><p class='muted'>Open <code>/status/{{payment_session_id}}</code> to view payment, grant, and fulfillment state for an approved action.</p></section></main></body></html>"""


@app.get("/healthz")
def healthz(session: Session = Depends(get_session)):
    bind = session.get_bind()
    backend = bind.dialect.name
    ok = False
    for attempt in range(3):
        try:
            session.exec(text("select 1")).one()
            ok = True
            break
        except Exception:
            rollback = getattr(session, "rollback", None)
            if callable(rollback):
                rollback()
            if attempt < 2:
                time.sleep(0.05 * (attempt + 1))
    if not ok:
        return JSONResponse(
            status_code=503,
            content={"status": "unhealthy", "database": {"ok": False, "backend": backend}},
        )
    return {"status": "ok", "database": {"ok": True, "backend": backend}}


@app.get("/status/{payment_session_id}", response_class=HTMLResponse)
def status_page(payment_session_id: str, checkout: str | None = None, session: Session = Depends(get_session), settings: Settings = Depends(get_settings)):
    ps, q, grant, fulfillment = _find_session_bundle(session, payment_session_id)
    if checkout == "success" and ps.provider == "decal" and ps.status != "paid" and ps.provider_session_id:
        try:
            verified = retrieve_decal_checkout_session(ps.provider_session_id, settings)
            _validate_decal_paid_session(session, ps, verified)
            _issue_paid_session(session, ps, settings, provider="decal")
            ps, q, grant, fulfillment = _find_session_bundle(session, payment_session_id)
        except Exception:
            session.rollback()
            ps, q, grant, fulfillment = _find_session_bundle(session, payment_session_id)
    def _public_status_label(raw: str) -> str:
        normalized = (raw or "").lower()
        labels = {
            "quoted": "Quoted — awaiting checkout",
            "checkout_created": "Awaiting payment",
            "pending": "Awaiting payment",
            "requires_payment": "Awaiting payment",
            "paid": "Payment complete",
            "executing": "Action in progress",
            "resumed": "Agent resumed — action in progress",
            "fulfilled": "Fulfilled — action succeeded",
            "succeeded": "Succeeded — action fulfilled",
            "failed": "Action needs attention",
            "refund_pending": "Refund requested — pending",
            "refund_requested": "Refund requested — pending",
            "refunded": "Refunded",
        }
        return labels.get(normalized, normalized.replace("_", " ").title() or "Status unavailable")

    fulfillment_items = "".join(f"<li><span class='check'>•</span><span>{_html_escape(_public_status_label(ev.status))}</span></li>" for ev in fulfillment) or "<li><span class='check'>•</span><span>No fulfillment has been recorded yet.</span></li>"
    grant_state = "Access has not been issued yet. Your agent is still awaiting payment confirmation."
    if grant:
        grant_state = "Access granted. Return to your agent so it can resume the original paid action."
        if grant.consumed_at is not None:
            grant_state = "Access used. Agent resumed the stored action and may still be working."
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
    fulfillment_statuses = {(ev.status or "").lower() for ev in fulfillment}
    has_failure = q.status == "failed" or "failed" in fulfillment_statuses
    has_refund = (
        ps.status in {"refund_pending", "refund_requested", "refunded"}
        or q.status in {"refund_pending", "refund_requested", "refunded"}
        or bool(fulfillment_statuses & {"refund_pending", "refund_requested", "refunded"})
    )
    has_success = q.status in {"fulfilled", "succeeded"} or bool(fulfillment_statuses & {"fulfilled", "succeeded"})
    action_in_progress = bool(grant and grant.consumed_at is not None and not has_success and not has_failure and not has_refund)
    if has_refund:
        checkout_label = "Refund requested — pending" if ps.status != "refunded" and q.status != "refunded" and "refunded" not in fulfillment_statuses else "Refunded"
    elif has_failure:
        checkout_label = "Action needs attention"
    elif has_success:
        checkout_label = "Fulfilled — action succeeded"
    elif action_in_progress:
        checkout_label = "Agent resumed — action in progress"
    else:
        checkout_label = "Payment complete" if ps.status == "paid" else "Awaiting payment"
    if has_refund:
        hero_copy = "The downstream action reported a failure or refund state, and a refund has been requested through Payjent."
    elif has_failure:
        hero_copy = "The payment checkpoint succeeded, but the downstream action reported a failure. Payjent will request a refund by default for paid failed actions."
    elif has_success:
        hero_copy = "The agent reported the paid action as fulfilled. Payjent records this status for the receipt."
    elif action_in_progress:
        hero_copy = "Your agent has used the paid access and resumed the stored action. Return to the agent for progress and results."
    elif ps.status == "paid":
        hero_copy = "Payment is complete. Return to your agent so it can resume the exact paid request."
    else:
        hero_copy = "Checkout is pending. Complete payment before returning to your agent to resume this paid action."
    access_copy = _html_escape(grant_state)
    resume_prompt = f"Payment is complete in Payjent for session {ps.id}. Please resume the paid action now using the original request."
    if grant and grant.consumed_at is not None:
        resume_prompt = f"Payment is complete in Payjent for session {ps.id}. Please check the paid action result and continue from the resumed work."
    resume_section = ""
    if ps.status == "paid" and grant:
        resume_section = f"""
        <div class='resume-card' aria-labelledby='resume-title'>
          <h2 id='resume-title'>Tell your agent to resume</h2>
          <p class='muted'>Copy this prompt back into your agent chat so it knows the payment is complete and should continue the original work.</p>
          <div class='prompt-wrap'>
            <textarea id='resume-prompt' class='prompt-box' readonly>{_html_escape(resume_prompt)}</textarea>
            <button class='copy-btn' type='button' onclick="navigator.clipboard?.writeText(document.getElementById('resume-prompt').value);this.textContent='Copied';">Copy prompt</button>
          </div>
          <span class='copy-hint'>No grant token or secret is shown here — the agent uses Payjent to resume the stored request.</span>
        </div>
        """
    primary_action = ""
    if ps.status != "paid":
        primary_action = f"<a class='btn' href='/pay/{_html_escape(ps.id)}'>Review approval</a>"
    payment_label = _public_status_label(ps.status)
    payment_state_copy = payment_label
    quote_label = _public_status_label(q.status)
    lifecycle_items = f"""
      <li><span class='check'>✓</span><span>Quoted — this action has a priced approval record.</span></li>
      <li><span class='check'>•</span><span>{_html_escape('Payment complete' if ps.status == 'paid' else 'Awaiting payment')}</span></li>
      <li><span class='check'>•</span><span>{_html_escape('Agent resumed — action in progress' if grant and grant.consumed_at is not None else ('Access granted — return to your agent' if grant else 'Access pending until payment completes'))}</span></li>
      <li><span class='check'>•</span><span>{_html_escape(checkout_label)}</span></li>
    """
    return f"""<!doctype html><html><head><title>Payjent status · {_html_escape(payment_label)}</title>{_STATUS_PAGE_CSS}</head><body><main><div class='brand'><div class='logo'><span class='mark'>P</span><span>Payjent</span></div><span class='domain'>payjent.com</span></div><div class='shell'><section class='hero'><div class='eyebrow'>Paid agent action</div><h1>{_html_escape(checkout_label)}</h1><p class='muted'>{_html_escape(hero_copy)}</p>{resume_section}<div class='actions'>{primary_action}<a class='btn secondary' href='https://payjent.com'>Payjent.com</a></div></section><section class='grid'><div class='card'><h2>Payment</h2><span class='status-pill'><span class='dot'></span>{_html_escape(payment_state_copy)}</span><div class='kv'><div class='row'><div class='label'>Session</div><div class='value'><code>{_html_escape(ps.id)}</code></div></div><div class='row'><div class='label'>Quote</div><div class='value'><code>{_html_escape(q.id)}</code> <span class='fine'>({_html_escape(quote_label)})</span></div></div><div class='row'><div class='label'>Amount</div><div class='value'>{_html_escape(_format_money(q.amount_minor, q.currency))}</div></div></div></div><div class='card'><h2>Access</h2><p>{access_copy}</p><ul class='timeline'><li><span class='check'>✓</span><span>Grant details stay hidden on this public page.</span></li><li><span class='check'>✓</span><span>Return to your agent; Payjent records checkpoints and does not run arbitrary provider actions from this page.</span></li></ul></div></section>{link_instructions}<section class='card'><h2>Lifecycle</h2><ul class='timeline'>{lifecycle_items}</ul></section><section class='card'><h2>Fulfillment</h2><ul class='timeline'>{fulfillment_items}</ul><p class='fine'>If the downstream action fails after payment, Payjent requests a refund by default unless the agent explicitly opts out.</p></section></div></main></body></html>"""


def _rail_to_read(r: RailConnection) -> RailConnectionRead:
    return RailConnectionRead(rail=r.rail, status=r.status, mode=r.mode, config=_safe_rail_config_summary(r))


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
        FulfillmentCreate(status="fulfilled", metadata={"smoke": True, "provider": "pay_sh", "settlement": "external_x402_runtime"}),
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
    rail = _upsert_rail(session, agent, "x402", "enabled" if payload.enabled else "disabled", "legacy", config)
    _upsert_rail(session, agent, "x402_cdp", "enabled" if payload.enabled else "disabled", "test", config)
    return _rail_to_read(rail)


@app.get("/api/v1/settlement-rails", response_model=list[SettlementRailManifest])
def list_settlement_rails():
    return [SettlementRailManifest(**manifest) for manifest in list_settlement_rail_manifests()]


@app.get("/api/v1/agents/{bot_id}/settlement-rails", response_model=AgentSettlementRailsResponse)
def get_agent_settlement_rails(bot_id: str, session: Session = Depends(get_session), credential: BotCredential = Depends(require_bot_credential)):
    _enforce_bot_scope(credential, bot_id)
    agent = session.exec(select(AgentProfile).where(AgentProfile.bot_id == bot_id)).first()
    connections: dict[str, RailConnectionRead] = {}
    if agent:
        rows = session.exec(select(RailConnection).where(RailConnection.agent_id == agent.id)).all()
        connections = {r.rail: _rail_to_read(r) for r in rows}
    return AgentSettlementRailsResponse(
        bot_id=bot_id,
        rails=[SettlementRailManifest(**manifest) for manifest in list_settlement_rail_manifests()],
        connections=connections,
        spend_instruction="Create/fund a task budget, create a paid action with task_budget_id, consume the grant, then POST /api/v1/grants/{grant_id}/spend-authorizations using one of the listed spend_authorization_rail values. The agent executes on the external rail; Payjent records authorization/capture/fulfillment evidence.",
    )


@app.post("/api/v1/agents/{agent_id}/settlement-rails", response_model=RailConnectionRead)
def configure_settlement_rail(agent_id: str, payload: SettlementRailConfigureRequest, session: Session = Depends(get_session), _credential: BotCredential = Depends(require_operator_credential)):
    agent = session.get(AgentProfile, agent_id)
    if not agent:
        raise HTTPException(404, "agent not found")
    try:
        rail_name = normalize_settlement_rail(payload.rail)
        manifest = settlement_rail_manifest(rail_name)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    config = {**manifest, **safe_metadata(payload.config), "enabled": payload.enabled}
    status = "disabled" if not payload.enabled else payload.status
    rail = _upsert_rail(session, agent, rail_name, status, payload.mode, config)
    return _rail_to_read(rail)


@app.post("/api/v1/agents/{bot_id}/settlement-rails/report", response_model=RailConnectionRead)
def report_agent_settlement_rail(bot_id: str, payload: SettlementRailConfigureRequest, session: Session = Depends(get_session), credential: BotCredential = Depends(require_bot_credential)):
    _enforce_bot_scope(credential, bot_id)
    agent = session.exec(select(AgentProfile).where(AgentProfile.bot_id == bot_id)).first()
    if not agent:
        agent = AgentProfile(id=f"agent_{uuid4().hex}", bot_id=bot_id, name=bot_id, platform="agent-runtime")
        session.add(agent); session.commit(); session.refresh(agent)
    try:
        rail_name = normalize_settlement_rail(payload.rail)
        manifest = settlement_rail_manifest(rail_name)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    config = {**manifest, **safe_metadata(payload.config), "enabled": payload.enabled}
    status = "disabled" if not payload.enabled else payload.status
    rail = _upsert_rail(session, agent, rail_name, status, payload.mode or "agent_reported", config)
    return _rail_to_read(rail)


@app.get("/api/v1/agents/{bot_id}/execution-readiness", response_model=ExecutionReadinessResponse)
def get_execution_readiness(bot_id: str, provider: str = "generic", session: Session = Depends(get_session), credential: BotCredential = Depends(require_bot_credential)):
    _enforce_bot_scope(credential, bot_id)
    return readiness_record(session, bot_id, provider)


@app.post("/api/v1/agents/{bot_id}/execution-readiness", response_model=ExecutionReadinessResponse)
def set_execution_readiness(bot_id: str, payload: ExecutionReadinessRequest, session: Session = Depends(get_session), credential: BotCredential = Depends(require_bot_credential)):
    _enforce_bot_scope(credential, bot_id)
    agent = session.exec(select(AgentProfile).where(AgentProfile.bot_id == bot_id)).first()
    if not agent:
        agent = AgentProfile(id=f"agent_{uuid4().hex}", bot_id=bot_id, name=bot_id, platform="agent-runtime")
        session.add(agent); session.commit(); session.refresh(agent)
    provider_slug = payload.provider.lower().replace("-", "_")
    safe_extra = safe_metadata(payload.metadata)
    rail = payload.rail or ("x402" if provider_slug in {"pay_sh", "paysh", "x402"} else f"provider:{provider_slug}")
    config = {
        "enabled": payload.status not in {"disabled", "not_ready"},
        "runtime_ready": payload.runtime_ready,
        "can_execute_without_device_auth": payload.can_execute_without_device_auth,
        "provider_connected": payload.provider_connected,
        "ready": payload.status in {"ready", "connected", "active"},
        "labels": payload.labels,
        **safe_extra,
    }
    _upsert_rail(session, agent, rail, "active" if config["ready"] else payload.status, "agent_reported", config)
    return readiness_record(session, bot_id, provider_slug)


@app.post("/api/v1/premium-actions/readiness-check", response_model=ExecutionReadinessResponse)
def premium_action_readiness_check(payload: ExecutionReadinessCheckRequest, session: Session = Depends(get_session), credential: BotCredential = Depends(require_bot_credential)):
    _enforce_bot_scope(credential, payload.bot_id)
    return enforce_readiness(session, bot_id=payload.bot_id, provider=payload.provider, readiness_mode=payload.readiness_mode, metadata=payload.execution_readiness)


def _integration_snippet(agent: AgentProfile) -> str:
    return f"""export PAYJENT_BASE_URL=http://localhost:8000
export PAYJENT_BOT_ID={agent.bot_id}
export PAYJENT_BOT_KEY=<shown-once-from-registration>
python -m payjent.demo discord-aggregator-stripe-smoke"""


def _create_agent_profile_from_form(form: dict[str, str], account: Account, session: Session, settings: Settings, *, issue_credential: bool = True) -> tuple[AgentProfile, str | None, bool]:
    name = (form.get("name") or "").strip()
    platform = (form.get("platform") or "").strip()
    bot_id = (form.get("bot_id") or "").strip()
    default_currency = (form.get("default_currency") or "USD").strip().upper() or "USD"
    callback_url = _validate_callback_url((form.get("callback_url") or "").strip() or None, settings)
    if not name or not platform or not bot_id:
        raise HTTPException(status_code=422, detail="agent name, platform, and bot_id are required")
    existing = session.exec(select(AgentProfile).where(AgentProfile.bot_id == bot_id)).first()
    if existing:
        if existing.owner_id != account.id:
            raise HTTPException(status_code=409, detail="bot_id is unavailable")
        if existing.status == "active":
            return existing, None, False
        existing.name = name
        existing.platform = platform
        existing.callback_url = callback_url
        existing.default_currency = default_currency
        existing.status = "active"
        session.add(existing)
        session.commit()
        session.refresh(existing)
        agent = existing
    else:
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
    if issue_credential and not session.exec(select(BotCredential).where(BotCredential.bot_id == agent.bot_id, BotCredential.role == "bot")).first():
        generated_key = generate_api_key()
        create_bot_credential(session, agent.bot_id, generated_key, settings.signing_secret, role="bot")
    return agent, generated_key, True


def _public_base_url(request: Request, settings: Settings) -> str:
    return (settings.canonical_public_base_url or settings.public_base_url or str(request.base_url).rstrip("/")).rstrip("/")


def _install_token_hash(token: str, settings: Settings) -> str:
    return hash_api_key(f"agent-install:{token}", settings.signing_secret)


def _safe_install_error() -> HTTPException:
    return HTTPException(status_code=404, detail="install link is invalid, expired, or already used")


def _as_aware_utc(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _credential_payload(agent: AgentProfile, api_key: str, request: Request, settings: Settings, scopes: list[str]) -> dict:
    return {
        "agent_id": agent.id,
        "bot_id": agent.bot_id,
        "payjent_base_url": _public_base_url(request, settings),
        "credential": {"type": "payjent_agent_api_key", "value": api_key, "header": "X-Payjent-Bot-Key"},
        "scopes": scopes,
        "policy": {"credential_scope": "agent", "single_agent_only": True, "store_privately": True, "do_not_paste_raw_credentials_in_chat": True},
    }


def _create_install_link(agent: AgentProfile, account: Account, request: Request, session: Session, settings: Settings, ttl_seconds: int = 900) -> tuple[AgentInstallLink, str]:
    if agent.status != "active":
        raise HTTPException(status_code=409, detail="deleted agents cannot create install links")
    ttl_seconds = max(60, min(int(ttl_seconds or 900), 3600))
    token = token_urlsafe(32)
    link = AgentInstallLink(
        id=f"ins_{uuid4().hex}", owner_id=account.id, agent_id=agent.id, bot_id=agent.bot_id,
        token_hash=_install_token_hash(token, settings),
        scopes=["quotes:create", "checkout:create", "agent-actions:create", "grants:consume"],
        expires_at=datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds),
    )
    session.add(link)
    session.commit()
    session.refresh(link)
    return link, f"{_public_base_url(request, settings)}/agent-install/{token}"


def _require_owned_agent(agent_id: str, account: Account, session: Session) -> AgentProfile:
    agent = session.get(AgentProfile, agent_id)
    if not agent or agent.owner_id != account.id:
        raise HTTPException(404, "agent not found")
    return agent


def _revoke_agent_credentials(session: Session, agent: AgentProfile) -> int:
    credentials = session.exec(select(BotCredential).where(BotCredential.bot_id == agent.bot_id, BotCredential.role == "bot")).all()
    for credential in credentials:
        session.delete(credential)
    return len(credentials)


def _invalidate_agent_install_links(session: Session, agent: AgentProfile) -> int:
    now = datetime.now(timezone.utc)
    links = session.exec(select(AgentInstallLink).where(AgentInstallLink.agent_id == agent.id, AgentInstallLink.consumed_at.is_(None))).all()
    for link in links:
        link.consumed_at = now
        session.add(link)
    return len(links)


def _credential_display_html(account: Account, agent: AgentProfile, bot_api_key: str | None, created: bool) -> str:
    key_block = ""
    if bot_api_key:
        key_block = f"""<div class='card'><h3>Copy this Payjent agent credential now</h3><p class='warnbox'><b>Shown once.</b> Payjent will not show this value again. Store it in the agent's private secret/tool store as <code>X-Payjent-Bot-Key</code> / Payjent agent credential. Do not paste it into chat.</p><pre><code>{_html_escape(bot_api_key)}</code></pre><p><a class='btn' href='/docs/agent-payjent-self-setup.md'>Open agent setup guide</a></p></div>"""
    else:
        key_block = """<div class='card'><h3>Existing agent found</h3><p class='muted'>This bot_id is already registered. Existing credentials are copy-once and cannot be revealed. If the original value was lost, create another credential from this agent's command view and store the new value privately.</p></div>"""
    status = "Agent registered" if created else "Agent already registered"
    return f"<!doctype html><html><head><title>{_html_escape(status)} · Payjent</title>{_DASHBOARD_CSS}</head><body><main><div class='topbar'><span class='muted'>Signed in as <b>{_html_escape(account.email)}</b></span><form method='post' action='/auth/logout'><button class='logout' type='submit'>Log out</button></form></div><section class='hero'><div class='eyebrow'>Unsafe manual recovery credential</div><h1>{_html_escape(status)}</h1><p class='muted'>{_html_escape(agent.name)} · <code>{_html_escape(agent.bot_id)}</code></p></section><div class='grid'>{key_block}<div class='card'><h3>Manual recovery next steps</h3><ol><li>Use this fallback only if Agent Install Link setup is unavailable.</li><li>Store it in the agent's private secret store as <code>X-Payjent-Bot-Key</code>.</li><li>Do not paste raw credentials in chat.</li><li>Return to the agent command view to configure rails.</li></ol><p><a class='btn' href='/dashboard/agents/{_html_escape(agent.id)}'>Open agent command view</a></p></div></div></main></body></html>"


def _install_link_display_html(account: Account, agent: AgentProfile, install_url: str, expires_at: datetime, created: bool) -> str:
    status = "Agent registered" if created else "Agent already registered"
    escaped_install_url = _html_escape(install_url)
    return f"<!doctype html><html><head><title>{_html_escape(status)} · Payjent</title>{_DASHBOARD_CSS}</head><body><main><div class='topbar'><span class='muted'>Signed in as <b>{_html_escape(account.email)}</b></span><form method='post' action='/auth/logout'><button class='logout' type='submit'>Log out</button></form></div><section class='hero'><div class='eyebrow'>One-time Agent Install Link</div><h1>{_html_escape(status)}</h1><p class='muted'>{_html_escape(agent.name)} · <code>{_html_escape(agent.bot_id)}</code></p></section><div class='grid'><div class='card'><h3>Give this install link to the target agent</h3><p class='warnbox'><b>Primary safe setup.</b> This single-use link expires at {_html_escape(expires_at.isoformat())}. It reveals no raw credential here; the credential is returned only to the agent when it redeems the link once.</p><p><button class='btn accent' type='button' data-copy-install-link='{escaped_install_url}'>Copy one-time install link</button></p><div class='copy-toast' data-copy-install-toast role='status' aria-live='polite' hidden>Link copied. Give it to the target agent.</div><p class='fine'>Readable fallback URL: <span class='mono'>{escaped_install_url}</span></p></div><div class='card'><h3>Next steps</h3><ol><li>Give only the one-time Agent Install Link to the target agent.</li><li>The agent redeems it once and stores the returned credential privately as <code>X-Payjent-Bot-Key</code>.</li><li>Send the agent this guide: <a href='/docs/agent-payjent-self-setup.md'>/docs/agent-payjent-self-setup.md</a>.</li><li>Return to the agent command view to configure rails.</li></ol><p><a class='btn' href='/dashboard/agents/{_html_escape(agent.id)}'>Open agent command view</a></p></div></div></main><script>(function(){{var button=document.querySelector('[data-copy-install-link]');var toast=document.querySelector('[data-copy-install-toast]');if(!button||!toast)return;function showToast(message){{toast.textContent=message;toast.hidden=false;window.clearTimeout(showToast.timer);showToast.timer=window.setTimeout(function(){{toast.hidden=true;}},3200);}}function fallbackCopy(text){{var area=document.createElement('textarea');area.value=text;area.setAttribute('readonly','');area.style.position='fixed';area.style.opacity='0';document.body.appendChild(area);area.select();try{{document.execCommand('copy');showToast('Link copied. Give it to the target agent.');}}catch(error){{showToast('Copy failed. Select the fallback URL below.');}}finally{{document.body.removeChild(area);}}}}button.addEventListener('click',function(){{var text=button.getAttribute('data-copy-install-link')||'';if(navigator.clipboard&&navigator.clipboard.writeText){{navigator.clipboard.writeText(text).then(function(){{showToast('Link copied. Give it to the target agent.');}},function(){{fallbackCopy(text);}});}}else{{fallbackCopy(text);}}}});}})();</script></body></html>"


_DASHBOARD_CSS = """<style>
:root{--paper:#fafaf7;--paper2:#f1efe8;--paper3:#e6e3d9;--ink:#0c0c0a;--ink2:#3a3935;--ink3:#74716a;--accent:#1947e5;--ok:#0e7a3b;--warn:#a8731f;--danger:#b51f1f}*{box-sizing:border-box}body{margin:0;min-height:100vh;background:var(--paper);color:var(--ink);font-family:Inter,ui-sans-serif,system-ui,-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;-webkit-font-smoothing:antialiased}a{color:var(--accent);text-decoration:none}main{max-width:1280px;margin:0 auto;padding:0 28px 84px}.mono{font-family:ui-monospace,SFMono-Regular,Menlo,monospace}.topbar{height:60px;display:flex;align-items:center;gap:18px;border-bottom:1px solid var(--ink);margin:0 -28px 36px;padding:0 28px}.topbar form{margin-left:auto}.brand{display:flex;align-items:center;gap:10px;color:var(--ink);font-weight:700}.brand-mark{width:24px;height:24px;border-radius:6px;background:var(--accent);color:#fff;display:grid;place-items:center;font-family:monospace}.logout{border:0;background:transparent;color:var(--ink2);font:600 14px inherit;cursor:pointer}.logout:hover{color:var(--accent)}.hero{padding:0 0 30px;border-bottom:1px solid var(--paper3);margin-bottom:28px}.eyebrow{display:block;font:700 11px/1.2 ui-monospace,Menlo,monospace;letter-spacing:.16em;text-transform:uppercase;color:var(--accent);margin-bottom:12px}h1{font-size:clamp(42px,6vw,70px);line-height:.96;letter-spacing:-.055em;margin:0 0 14px}h1 em,h2 em{font-family:Georgia,serif;font-style:italic;font-weight:400;color:var(--accent)}h2{font-size:clamp(32px,4vw,46px);line-height:1;letter-spacing:-.045em;margin:0 0 10px}h3{font-size:18px;letter-spacing:-.02em;margin:0}.muted,p{color:var(--ink2);line-height:1.5}.fine{font:12px/1.5 ui-monospace,Menlo,monospace;color:var(--ink3);overflow-wrap:anywhere}.copy-toast{display:inline-flex;align-items:center;margin:4px 0 10px;padding:8px 11px;border:1px solid var(--paper3);border-radius:999px;background:#fff;color:var(--ok);font:700 13px/1.3 inherit;box-shadow:0 8px 22px rgba(12,12,10,.08)}.copy-toast[hidden]{display:none}.btn,button[type=submit]{display:inline-flex;align-items:center;justify-content:center;gap:8px;padding:10px 15px;border:1px solid var(--ink);border-radius:8px;background:var(--paper);color:var(--ink);font-weight:700;cursor:pointer}.btn:hover,button[type=submit]:hover{background:var(--ink);color:var(--paper);text-decoration:none}.btn.accent,button[type=submit]{background:var(--accent);border-color:var(--accent);color:#fff}.btn.dark{background:var(--ink);color:var(--paper)}.cta-banner{margin-top:28px;border:1px solid var(--ink);border-radius:14px;background:var(--ink);color:var(--paper);display:grid;grid-template-columns:1.45fr .9fr;overflow:hidden}.cta-banner>div:first-child{padding:30px 34px}.cta-banner p{color:rgba(250,250,247,.74);max-width:680px}.cta-banner .eyebrow{color:#9bb5ff}.runbook{background:#080806;border-left:1px solid #333;padding:24px;color:#cfcdc4;font-size:12px;line-height:1.9}.runbook b{color:#9bb5ff;margin-right:10px}.kpi-row{display:grid;grid-template-columns:repeat(4,1fr);border:1px solid var(--paper3);border-radius:12px;background:#fff;overflow:hidden;margin:28px 0}.kpi{padding:20px;border-left:1px solid var(--paper3)}.kpi:first-child{border-left:0}.lbl,.sub{font:11px ui-monospace,Menlo,monospace;text-transform:uppercase;letter-spacing:.08em;color:var(--ink3)}.stat{font-size:32px;font-weight:700;letter-spacing:-.03em;margin:8px 0;color:var(--ink)}.stat.small{font-size:20px;line-height:1.2}.dash-layout{display:grid;grid-template-columns:1.6fr .9fr;gap:24px}.panel{background:#fff;border:1px solid var(--paper3);border-radius:12px;overflow:hidden}.panel.wide{grid-column:1/-1}.ph{display:flex;align-items:center;justify-content:space-between;gap:12px;padding:14px 18px;background:var(--paper2);border-bottom:1px solid var(--paper3)}.pb{padding:18px}.pb.flat{padding:0}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:16px}.grid.compact .card{border:1px solid var(--paper3);border-radius:10px;padding:16px}.card{background:#fff}.pill{display:inline-flex;color:var(--accent);font:11px ui-monospace,Menlo,monospace;text-transform:uppercase;letter-spacing:.08em;margin-bottom:8px}.pill.ok{color:var(--ok)}.pill.warn{color:var(--warn)}.event{padding:14px 18px;border-bottom:1px solid var(--paper3);font-size:14px}.event:last-child{border-bottom:0}form{display:grid;gap:11px;margin-top:14px}label{font:700 11px ui-monospace,Menlo,monospace;text-transform:uppercase;letter-spacing:.08em;color:var(--ink2)}input,select{width:100%;padding:11px 12px;border:1px solid var(--paper3);border-radius:8px;background:var(--paper);font:14px inherit;color:var(--ink)}input:focus{outline:2px solid rgba(25,71,229,.18);border-color:var(--accent)}pre{white-space:pre-wrap;overflow:auto;background:var(--paper2);border:1px solid var(--paper3);border-radius:10px;padding:14px}code{font-family:ui-monospace,Menlo,monospace;background:var(--paper2);padding:.1rem .25rem;border-radius:4px}table{width:100%;border-collapse:collapse}th,td{padding:12px;border-bottom:1px solid var(--paper3);text-align:left;vertical-align:top;font-size:13px}th{font:11px ui-monospace,Menlo,monospace;text-transform:uppercase;color:var(--ink3)}.auth-wrap{min-height:100vh;display:grid;place-items:center;padding:28px}.auth-card{max-width:560px;background:#fff;border:1px solid var(--paper3);border-radius:14px;padding:30px}.error,.warnbox{border:1px solid var(--danger);color:var(--danger);background:#fff;padding:12px;border-radius:8px}@media(max-width:900px){main{padding:0 18px 60px}.topbar{margin:0 -18px 28px;padding:0 18px}.cta-banner,.dash-layout,.kpi-row{grid-template-columns:1fr}.runbook{border-left:0;border-top:1px solid #333}.kpi{border-left:0;border-top:1px solid var(--paper3)}}
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

def _quote_lifecycle_stage(session: Session, q: Quote) -> tuple[str, str, PaymentSession | None, Grant | None]:
    ps = session.exec(select(PaymentSession).where(PaymentSession.quote_id == q.id).order_by(PaymentSession.created_at.desc())).first()
    grant = session.exec(select(Grant).where(Grant.quote_id == q.id).order_by(Grant.created_at.desc())).first()
    fulfillment = session.exec(select(FulfillmentEvent).where(FulfillmentEvent.quote_id == q.id).order_by(FulfillmentEvent.created_at.desc())).first()
    execution = session.exec(select(ToolExecution).where(ToolExecution.quote_id == q.id).order_by(ToolExecution.created_at.desc())).first()
    q_status = (q.status or "quoted").lower()
    ps_status = (ps.status if ps else "").lower()
    fulfillment_status = (fulfillment.status if fulfillment else "").lower()
    execution_status = (execution.status if execution else "").lower()
    if q_status in {"failed", "failure", "refunded", "refund_requested", "refund_pending", "canceled", "cancelled"} or ps_status in {"refund_pending", "refund_requested", "refunded"} or fulfillment_status in {"failed", "failure", "refund_requested", "refunded"} or execution_status in {"failed", "failure", "error"}:
        return "failed", "Failed or refund path active; review fulfillment/error events before retrying.", ps, grant
    if q_status in {"fulfilled", "succeeded", "success"} or fulfillment_status in {"fulfilled", "succeeded", "success"} or execution_status in {"succeeded", "success"}:
        return "succeeded", "Fulfillment evidence recorded.", ps, grant
    if q_status == "executing" or execution is not None or fulfillment_status in {"executing", "started"}:
        return "executing", "Payment cleared and work has started; wait for fulfillment result.", ps, grant
    if q_status == "paid" or ps_status == "paid" or grant is not None:
        return "paid", "Paid but no execution/fulfillment evidence yet; check agent resume worker.", ps, grant
    if ps is not None:
        return "checkout", "Checkout created but payment not marked paid yet; send/remind user payment link.", ps, grant
    return "quoted", "Quoted without checkout; create checkout before asking user to pay.", ps, grant


def _lifecycle_counts_and_rows(session: Session, quotes: list[Quote]) -> tuple[dict[str, int], str]:
    counts = {stage: 0 for stage in ["quoted", "checkout", "paid", "executing", "succeeded", "failed"]}
    rows = []
    for q in quotes:
        stage, hint, ps, grant = _quote_lifecycle_stage(session, q)
        counts[stage] += 1
        status_link = f"<a href='/status/{_html_escape(ps.id)}'>Public status</a>" if ps else ""
        refund_form = ""
        if stage == "failed" and ps:
            if (ps.status or "").lower() in {"refund_pending", "refund_requested", "refunded"} or (q.status or "").lower() in {"refund_pending", "refund_requested", "refunded"}:
                refund_form = "<span class='pill warn'>Refund requested</span>"
            else:
                refund_form = f"<form method='post' action='/dashboard/payment-sessions/{_html_escape(ps.id)}/refund'><button type='submit'>Request refund</button></form>"
        support = "<span class='pill warn'>Needs support</span>" if stage == "failed" else ""
        actions = "<br>".join(part for part in [support, refund_form, status_link] if part) or "<span class='muted'>—</span>"
        rows.append(f"<tr><td><code>{_html_escape(q.id)}</code><br><span class='muted'>{_html_escape(q.request_summary)}</span></td><td><span class='pill'>{_html_escape(stage)}</span></td><td>{_html_escape(_format_money(q.amount_minor, q.currency))}</td><td>{_html_escape(ps.provider if ps else 'none')}<br><span class='muted'>{_html_escape(ps.status if ps else 'no checkout')}</span></td><td>{_html_escape(_grant_state(grant))}</td><td><span class='muted'>{_html_escape(hint)}</span></td><td>{actions}</td></tr>")
    return counts, "".join(rows) or "<tr><td colspan='7'>No paid actions yet.</td></tr>"


def _lifecycle_rows(session: Session, quotes: list[Quote]) -> str:
    return _lifecycle_counts_and_rows(session, quotes)[1]


def _lifecycle_summary_html(session: Session, quotes: list[Quote], title: str = "Action status") -> str:
    counts, rows = _lifecycle_counts_and_rows(session, quotes)
    count_html = "".join(f"<div class='kpi'><div class='lbl'>{_html_escape(stage)}</div><div class='stat'>{counts[stage]}</div></div>" for stage in ["quoted", "checkout", "paid", "executing", "succeeded", "failed"])
    return f"<div class='card'><h3>{_html_escape(title)}</h3><p class='muted'>User-friendly paid action stages: quoted → checkout → paid → executing → succeeded/failed. Hints identify likely stuck states without exposing payment tokens, grant IDs, or secrets.</p><div class='kpi-row'>{count_html}</div><table><thead><tr><th>Action</th><th>Stage</th><th>Amount</th><th>Checkout</th><th>Grant</th><th>Status hint</th><th>Support</th></tr></thead><tbody>{rows}</tbody></table></div>"


def _premium_preset_readiness_html(settings: Settings) -> str:
    cards = "".join(
        f"<div class='event' data-preset-id='{_html_escape(row['id'])}'><b>{_html_escape(row['name'])}</b> · {_html_escape(row['provider'])} <span class='pill {'ok' if row['ready'] else 'warn'}'>{'ready' if row['ready'] else 'needs config'}</span><br><span class='muted'>{_html_escape(row['hint'])}</span></div>"
        for row in _premium_preset_readiness(settings)
    )
    return f"<section class='card'><div class='eyebrow'>Execution readiness</div><h2>Premium preset readiness</h2><p class='muted'>Safe readiness hints for FAL, Replicate, Browserbase, ElevenLabs, Exa, and other premium presets. No secret names, values, payment tokens, or provider session IDs are shown.</p>{cards}</section>"


def _launch_checklist_html(agent_count: int, active_payment_ready: bool) -> str:
    readiness = _html_escape("ready" if active_payment_ready else "needs configuration")
    return f"""<section class='card'><div class='eyebrow'>Launch checklist</div><h2>Launch readiness</h2><div class='grid'>
<div class='event'><b>Agent registered</b><br><span class='muted'>{agent_count} active agent(s) available for paid actions.</span></div>
<div class='event'><b>Install link / credential path</b><br><span class='muted'>Use one-time Agent Install Links first; manual recovery credentials stay copy-once and private.</span></div>
<div class='event'><b>Active checkout readiness</b><br><span class='muted'>Checkout provider status: {readiness}. No secret configuration values are shown.</span></div>
<div class='event'><b>Discovery manifest / status endpoint</b><br><span class='muted'>Expose /.well-known/payjent-tools.json and authenticated /api/v1/agent-capabilities for installed agents.</span></div>
<div class='event'><b>Paid action lifecycle evidence</b><br><span class='muted'>Dashboard tracks quoted, checkout, paid, executing, succeeded, and failed evidence.</span></div>
<div class='event'><b>Failure / refund path</b><br><span class='muted'>Failed actions remain visible for operator review, retry, support, or refund handling.</span></div>
</div></section>"""

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
    all_agents = session.exec(select(AgentProfile).where(AgentProfile.owner_id == account.id).order_by(AgentProfile.created_at.desc())).all()
    agents = [a for a in all_agents if a.status == "active"]
    deleted_agents = [a for a in all_agents if a.status != "active"]
    owned_bot_ids = [a.bot_id for a in all_agents]
    if owned_bot_ids:
        quotes = session.exec(select(Quote).where(Quote.bot_id.in_(owned_bot_ids)).order_by(Quote.created_at.desc()).limit(20)).all()
    else:
        quotes = []
    quote_ids = [q.id for q in quotes]
    spends = session.exec(select(SpendLedgerEntry).where(SpendLedgerEntry.quote_id.in_(quote_ids)).order_by(SpendLedgerEntry.created_at.desc()).limit(20)).all() if quote_ids else []
    sessions_by_quote = _sessions_by_quote_id(session, quote_ids)
    paid_totals = _money_totals_by_currency(quotes, {"paid", "fulfilled", "executing"})
    spend_totals = _money_totals_by_currency(spends, {"authorized", "captured"})
    cards = "".join(f"<div class='card' data-agent-id='{_html_escape(a.id)}'><span class='pill'>{_html_escape(a.platform)}</span><span class='pill ok'>{_html_escape(a.status)}</span><h3>{_html_escape(a.name)}</h3><p class='muted'><code>{_html_escape(a.bot_id)}</code></p><p><a class='btn' href='/dashboard/agents/{_html_escape(a.id)}'>Open command view</a></p></div>" for a in agents) or "<div class='card'><h3>No active agents</h3><p class='muted'>Use the Register agent form above. Payjent will generate a short-lived, single-use Agent Install Link instead of showing a raw credential.</p></div>"
    deleted_cards = "".join(f"<div class='event' data-deleted-agent-id='{_html_escape(a.id)}'><b>{_html_escape(a.name)}</b> · <code>{_html_escape(a.bot_id)}</code><br><span class='muted'>Status: {_html_escape(a.status)} — credentials revoked; audit history preserved. <a href='/dashboard/agents/{_html_escape(a.id)}'>View history</a></span></div>" for a in deleted_agents)
    deleted_section = f"<div class='card'><h3>Deleted agents</h3>{deleted_cards}</div>" if deleted_cards else ""
    interactions = "".join(f"<div class='event' data-quote-id='{_html_escape(q.id)}'><b>{_html_escape(q.status)}</b> · {_html_escape(_format_money(q.amount_minor, q.currency))}<br><span class='muted'>{_html_escape(q.request_summary)}</span><br><span class='muted'>How paid: {_html_escape(sessions_by_quote[q.id].provider)} / {_html_escape(sessions_by_quote[q.id].status)}</span></div>" if q.id in sessions_by_quote else f"<div class='event' data-quote-id='{_html_escape(q.id)}'><b>{_html_escape(q.status)}</b> · {_html_escape(_format_money(q.amount_minor, q.currency))}<br><span class='muted'>{_html_escape(q.request_summary)}</span><br><span class='muted'>How paid: no payment session yet</span></div>" for q in quotes[:6]) or "<div class='event'><b>No interactions yet</b><br><span class='muted'>Paid action requests will appear here when your agents create real Payjent quotes.</span></div>"
    spend_events = "".join(f"<div class='event' data-spend-id='{_html_escape(s.id)}'><b>{_html_escape(s.tool)} → {_html_escape(s.vendor)}</b> · {_html_escape(_format_money(s.amount_minor, s.currency))}<br><span class='muted'>{_html_escape(s.reason or 'No reason supplied by agent.')}</span></div>" for s in spends[:6]) or "<div class='event'><b>No downstream spend yet</b><br><span class='muted'>Reason-backed spend ledger entries appear only after an agent consumes a grant and requests spend authorization.</span></div>"
    register_form = """<form method='post' action='/dashboard/agents/register'><label>Agent name</label><input name='name' placeholder='Research assistant' required><label>Platform</label><input name='platform' placeholder='discord, web, slack, cli' required><label>Bot ID</label><input name='bot_id' placeholder='stable-agent-id' required><label>Default currency</label><input name='default_currency' value='USD' maxlength='3' required><label>Callback URL (optional)</label><input name='callback_url' type='url' placeholder='https://agent.example/callback'><button type='submit'>Register agent and create install link</button></form>"""
    launch_checklist = _launch_checklist_html(len(agents), bool(_payment_readiness(settings).get("active_payment_ready")))
    preset_readiness = _premium_preset_readiness_html(settings)
    lifecycle_overview = _lifecycle_summary_html(session, quotes[:10], "Action status overview")
    return f"""<!doctype html><html><head><title>Payjent dashboard</title>{_DASHBOARD_CSS}</head><body><main><div class='topbar'><a class='brand' href='/'><span class='brand-mark'>P</span><span>payjent</span></a><span class='muted'>Signed in as <b>{_html_escape(account.email)}</b></span><form method='post' action='/auth/logout'><button class='logout' type='submit'>Log out</button></form></div><section class='hero'><div class='eyebrow'>Payment operations</div><h1>Agent <em>command</em> center</h1><p class='muted'>Register agents, generate one-time Agent Install Links, watch paid action requests, and keep reasoning-backed spend records without exposing raw bot keys or payment tokens.</p><div class='cta-banner'><div><span class='eyebrow'>Primary action</span><h2>Register an <em>agent.</em></h2><p>Create a short-lived Agent Install Link for one agent identity. The raw credential is returned only to the agent during one successful redemption.</p><p><a class='btn accent' href='#register-agent'>Register your agent →</a><a class='btn dark' href='/docs/agent-payjent-self-setup.md'>Read setup guide</a></p></div><div class='runbook mono'><div><b>1</b> Sign in to dashboard</div><div><b>2</b> Register stable bot_id and generate install link</div><div><b>3</b> Agent redeems link once</div><div><b>4</b> Send setup guide to agent</div><div><b>5</b> Configure Stripe Connect, x402, and integration snippets on agent detail</div><div><b>6</b> Confirm approval gate resumes exact action</div></div></div></section><div class='kpi-row'><div class='kpi'><div class='lbl'>Agents</div><div class='stat'>{len(agents)}</div><p class='fine'>Registered identities</p></div><div class='kpi'><div class='lbl'>Paid action volume</div><div class='stat small'>{_format_money_totals(paid_totals)}</div><p class='fine'>Grouped by currency from recent paid / fulfilled quotes.</p></div><div class='kpi'><div class='lbl'>Downstream spend</div><div class='stat small'>{_format_money_totals(spend_totals)}</div><p class='fine'>Authorized or captured ledger</p></div><div class='kpi'><div class='lbl'>Recent requests</div><div class='stat'>{len(quotes)}</div><p class='fine'>Latest action quotes</p></div></div>{launch_checklist}{preset_readiness}{lifecycle_overview}<div class='dash-layout'><section class='panel'><div class='ph'><h3>Agent-owner quickstart</h3><span class='sub'>latest quotes</span></div><div class='pb flat'>{interactions}</div></section><aside class='panel' id='register-agent'><div class='ph'><h3>Register agent</h3><span class='sub'>install link generated</span></div><div class='pb'><p class='muted'>Use this authenticated form. Payjent will generate a short-lived, single-use Agent Install Link. The raw credential is returned only when the agent redeems that link once.</p>{register_form}<p class='fine'>Do not paste raw credentials in chat; share only the install link with the target agent.</p></div></aside><section class='panel'><div class='ph'><h3>Registered agents</h3><span class='sub'>{len(agents)} total</span></div><div class='pb grid compact'>{cards}</div>{deleted_section}</section><aside class='panel'><div class='ph'><h3>Policy defaults</h3><span class='sub'>workspace</span></div><div class='pb'>{_policy_defaults_html()}</div></aside><section class='panel'><div class='ph'><h3>Spend reasoning trail</h3><span class='sub'>reason → vendor → amount</span></div><div class='pb flat'>{spend_events}</div></section><section class='panel wide'><div class='ph'><h3>Paid-action lifecycle ledger</h3><span class='sub'>quote → payment → grant → fulfillment</span></div><div class='pb'><table><thead><tr><th>Action</th><th>Quote</th><th>Payment</th><th>Grant</th><th>Fulfillment</th><th>Spend</th></tr></thead><tbody>{_lifecycle_rows(session, quotes)}</tbody></table></div></section></div></main></body></html>"""



@app.post("/dashboard/agents/register", response_class=HTMLResponse)
async def dashboard_register_agent(request: Request, session: Session = Depends(get_session), settings: Settings = Depends(get_settings)):
    account = _require_dashboard_account(request, session, settings)
    if isinstance(account, RedirectResponse):
        return account
    form = await _form_fields(request)
    agent, _bot_api_key, created = _create_agent_profile_from_form(form, account, session, settings, issue_credential=False)
    link, install_url = _create_install_link(agent, account, request, session, settings)
    return HTMLResponse(_install_link_display_html(account, agent, install_url, link.expires_at, created))


@app.post("/dashboard/agents/install-links")
async def dashboard_create_agent_install_link(request: Request, session: Session = Depends(get_session), settings: Settings = Depends(get_settings)):
    account = _require_dashboard_account(request, session, settings)
    if isinstance(account, RedirectResponse):
        return account
    content_type = request.headers.get("content-type", "")
    wants_json = "application/json" in content_type
    form = await (request.json() if wants_json else _form_fields(request))
    agent_id = (form.get("agent_id") or "").strip()
    ttl_seconds = int(form.get("ttl_seconds") or 900)
    agent = session.get(AgentProfile, agent_id)
    if not agent or agent.owner_id != account.id:
        raise HTTPException(404, "agent not found")
    link, install_url = _create_install_link(agent, account, request, session, settings, ttl_seconds)
    if not wants_json:
        return HTMLResponse(_install_link_display_html(account, agent, install_url, link.expires_at, False))
    return JSONResponse({
        "install_link_id": link.id,
        "install_url": install_url,
        "agent_id": agent.id,
        "bot_id": agent.bot_id,
        "scopes": link.scopes,
        "expires_at": link.expires_at.isoformat(),
        "instructions": "Give this one-time install link only to the target agent. Do not paste raw credentials or tokens in chat.",
    })


@app.get("/agent-install/{token}", response_class=HTMLResponse)
def agent_install_landing(token: str, request: Request, session: Session = Depends(get_session), settings: Settings = Depends(get_settings)):
    link = session.exec(select(AgentInstallLink).where(AgentInstallLink.token_hash == _install_token_hash(token, settings))).first()
    now = datetime.now(timezone.utc)
    if not link or link.consumed_at or _as_aware_utc(link.expires_at) <= now:
        raise _safe_install_error()
    agent = session.get(AgentProfile, link.agent_id)
    if not agent or agent.status != "active":
        raise _safe_install_error()
    return HTMLResponse(f"""<!doctype html><html><head><title>Payjent agent install</title>{_DASHBOARD_CSS}</head><body><main><section class='hero'><div class='eyebrow'>One-time Agent Install Link</div><h1>Install Payjent for {_html_escape(agent.name)}</h1><p class='muted'>This private setup link is valid once and expires at {_html_escape(link.expires_at.isoformat())}. Redeem it only from the target agent and store the returned credential privately.</p></section><form method='post'><button type='submit'>Redeem once</button></form><p class='fine'>Payjent will not display raw credentials on this landing page. Do not paste raw credentials, tokens, or env lines in chat.</p></main></body></html>""")


@app.post("/agent-install/{token}")
def redeem_agent_install_link(token: str, request: Request, session: Session = Depends(get_session), settings: Settings = Depends(get_settings)):
    now = datetime.now(timezone.utc)
    claimed = session.exec(
        update(AgentInstallLink)
        .where(
            AgentInstallLink.token_hash == _install_token_hash(token, settings),
            AgentInstallLink.consumed_at.is_(None),
            AgentInstallLink.expires_at > now,
        )
        .values(consumed_at=now)
    )
    if claimed.rowcount != 1:
        session.rollback()
        raise _safe_install_error()
    session.commit()
    link = session.exec(select(AgentInstallLink).where(AgentInstallLink.token_hash == _install_token_hash(token, settings))).first()
    if not link:
        raise _safe_install_error()
    agent = session.get(AgentProfile, link.agent_id)
    if not agent or agent.owner_id != link.owner_id or agent.bot_id != link.bot_id or agent.status != "active":
        raise _safe_install_error()
    api_key = generate_api_key()
    create_bot_credential(session, agent.bot_id, api_key, settings.signing_secret, role="bot")
    payload = _credential_payload(agent, api_key, request, settings, link.scopes)
    payload["install_link"] = {"id": link.id, "consumed_at": now.isoformat(), "expires_at": link.expires_at.isoformat(), "single_use": True}
    return payload


@app.get("/api/v1/agent-capabilities")
def agent_capabilities(request: Request, session: Session = Depends(get_session), settings: Settings = Depends(get_settings), credential: BotCredential = Depends(require_bot_credential)):
    agent = session.exec(select(AgentProfile).where(AgentProfile.bot_id == credential.bot_id)).first()
    if not agent or agent.status != "active":
        raise HTTPException(status_code=404, detail="agent not found")
    base_url = _public_base_url(request, settings)
    rails = session.exec(select(RailConnection).where(RailConnection.agent_id == agent.id)).all()
    enabled_rails = []
    x402_config = None
    x402_available = False
    for rail in rails:
        cfg = _safe_rail_config_summary(rail)
        enabled_rails.append({"rail": rail.rail, "status": rail.status, "mode": rail.mode, "config_summary": cfg})
        if rail.rail == "x402" and rail.status in {"connected", "enabled"}:
            x402_available = True
            x402_config = cfg
    return {
        "agent": {"id": agent.id, "bot_id": agent.bot_id, "name": agent.name, "platform": agent.platform, "status": agent.status, "default_currency": agent.default_currency},
        "base_url": base_url,
        "enabled_rails": enabled_rails,
        "settlement_rails": [manifest for manifest in list_settlement_rail_manifests()],
        "settlement_rails_url": f"{base_url}/api/v1/agents/{agent.bot_id}/settlement-rails",
        "tools": _tool_descriptors(x402_available=x402_available),
        "premium_action_presets_url": f"{base_url}/api/v1/premium-action-presets",
        "premium_action_preset_count": len(list_presets()),
        "premium_tool_discovery": _premium_tool_discovery(base_url, x402_available=x402_available),
        "active_payment": {
            "provider": _checkout_provider(settings),
            "ready": _payment_readiness(settings)["active_payment_ready"],
            "instructions": "Send the returned payment_prompt/payment_url to the user. When configured for production, Decal is the active checkout rail; wait for paid status before resuming.",
        },
        "limits": {
            "default_currency": agent.default_currency,
            "x402": {
                "max_per_request_minor": x402_config.get("max_per_request_minor") if x402_config else None,
                "max_per_call_minor": x402_config.get("max_per_call_minor") if x402_config else None,
                "currency": x402_config.get("currency", agent.default_currency) if x402_config else agent.default_currency,
            },
        },
        "docs_url": f"{base_url}/docs/agent-payjent-self-setup.md",
        "dashboard_url": f"{base_url}/dashboard/agents/{agent.id}",
        "security_invariants": _discovery_manifest(base_url)["security_invariants"],
    }


@app.get("/dashboard/agents/{agent_id}", response_class=HTMLResponse)
def dashboard_agent(agent_id: str, request: Request, session: Session = Depends(get_session), settings: Settings = Depends(get_settings)):
    account = _require_dashboard_account(request, session, settings)
    if isinstance(account, RedirectResponse):
        return account
    agent = _require_owned_agent(agent_id, account, session)
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
    lifecycle = _lifecycle_summary_html(session, quotes, "Agent action status")
    stripe_cmd = f"curl -X POST /api/v1/agents/{agent.id}/stripe-connect/start -H 'X-Payjent-Bot-Key: &lt;operator-key&gt;'"
    x402_cmd = f"curl -X POST /api/v1/agents/{agent.id}/x402/configure -H 'X-Payjent-Bot-Key: &lt;operator-key&gt;' -H 'Content-Type: application/json' -d '{{\"network\":\"base-sepolia\",\"pay_to\":\"0xTEST_PAY_TO\",\"max_per_request_minor\":900,\"max_per_call_minor\":250,\"enabled\":true}}'"
    controls_disabled = agent.status != "active"
    credential_form = f"""<form method='post' action='/dashboard/agents/{_html_escape(agent.id)}/credentials'><p class='warnbox'><b>Unsafe manual/admin recovery fallback.</b> Prefer Agent Install Link. Create a raw copy-once credential only if the install-link flow is unavailable and you can place it directly into a private secret store; never paste it in chat.</p><button type='submit' {'disabled' if controls_disabled else ''}>Create manual recovery credential</button></form>"""
    revoke_form = f"""<form method='post' action='/dashboard/agents/{_html_escape(agent.id)}/credentials/revoke'><p class='warnbox'><b>Revoke credentials.</b> Deletes hashed bot credentials for this agent. Existing payment history remains readable; old agent API keys immediately stop authenticating.</p><button type='submit' {'disabled' if controls_disabled else ''}>Revoke credentials</button></form>"""
    delete_form = f"""<form method='post' action='/dashboard/agents/{_html_escape(agent.id)}/delete'><p class='warnbox'><b>Delete agent.</b> This deactivates the agent instead of hard-deleting it: credentials are revoked, outstanding install links are burned, new install links are blocked, and audit/payment history is preserved.</p><button type='submit' {'disabled' if controls_disabled else ''}>Delete agent</button></form>"""
    install_link_form = f"""<form method='post' action='/dashboard/agents/install-links'><input type='hidden' name='agent_id' value='{_html_escape(agent.id)}'><input type='hidden' name='ttl_seconds' value='900'><p class='muted'>Safest easy setup: generate a short-lived, single-use Agent Install Link for this agent, give that link to the agent, and let it redeem an agent-scoped credential once. Do not paste raw credentials or env lines in chat.</p><button type='submit' {'disabled' if controls_disabled else ''}>Generate one-time install link</button></form>"""
    base_url = _public_base_url(request, settings)
    discovery_card = f"""<div class='card'><h3>Paid tool discovery</h3><p class='muted'>After install, have the agent fetch the public manifest, then call authenticated capabilities with its private <code>X-Payjent-Bot-Key</code> to decide which paid actions are possible.</p><p class='fine'>Public manifest: <a href='/.well-known/payjent-tools.json'>{_html_escape(base_url)}/.well-known/payjent-tools.json</a></p><p class='fine'>Authenticated capabilities: <code>{_html_escape(base_url)}/api/v1/agent-capabilities</code></p><p><a class='btn' href='/docs/agent-payjent-self-setup.md'>Read discovery docs</a></p></div>"""
    return f"<!doctype html><html><head><title>{_html_escape(agent.name)} · Payjent</title>{_DASHBOARD_CSS}</head><body><main><div class='topbar'><a href='/dashboard'>← Dashboard</a><form method='post' action='/auth/logout'><button class='logout' type='submit'>Log out</button></form></div><section class='hero'><span class='pill'>{_html_escape(agent.status)}</span><h1>{_html_escape(agent.name)}</h1><p class='muted'>{_html_escape(agent.platform)} · <code>{_html_escape(agent.bot_id)}</code></p></section><div class='grid'><div class='card'><h3>Current policy defaults</h3>{_policy_defaults_html(x402_caps)}</div><div class='card'><h3>Smoke-test setup</h3><ol class='checklist'><li>Keep the Payjent agent credential in the agent secret store.</li><li>Give the agent <a href='/docs/agent-payjent-self-setup.md'>the setup guide</a>.</li><li>Run the demo smoke test and confirm payment creates a one-time resumable action.</li></ol></div></div>{discovery_card}<div class='grid'>{rail_cards}</div><div class='grid'><div class='card'><h3>Agent Install Link</h3>{install_link_form}</div><div class='card'><h3>Agent credential</h3>{credential_form}</div><div class='card'><h3>Danger zone</h3>{revoke_form}{delete_form}</div><div class='card'><h3>Stripe Connect</h3><p class='muted'>Local/test starts return a simulated account link; production fails closed until live OAuth is configured.</p><pre><code>{_html_escape(stripe_cmd)}</code></pre></div><div class='card'><h3>x402 rail configuration</h3><p class='muted'>Stores only non-secret network, pay_to, facilitator URL, and caps.</p><pre><code>{_html_escape(x402_cmd)}</code></pre></div></div><div class='card'><h3>Integration snippet</h3><pre><code>{_html_escape(_integration_snippet(agent))}</code></pre></div><div class='card'><h3>Recent payments / spend ledger</h3><h3>Spend ledger entries</h3><p>{len(quotes)} recent quotes</p><ul>{ledger}</ul></div>{lifecycle}</main></body></html>"


@app.post("/dashboard/agents/{agent_id}/credentials", response_class=HTMLResponse)
def dashboard_create_agent_credential(agent_id: str, request: Request, session: Session = Depends(get_session), settings: Settings = Depends(get_settings)):
    account = _require_dashboard_account(request, session, settings)
    if isinstance(account, RedirectResponse):
        return account
    agent = _require_owned_agent(agent_id, account, session)
    if agent.status != "active":
        raise HTTPException(status_code=409, detail="deleted agents cannot create credentials")
    generated_key = generate_api_key()
    create_bot_credential(session, agent.bot_id, generated_key, settings.signing_secret, role="bot")
    return HTMLResponse(_credential_display_html(account, agent, generated_key, True))


@app.post("/dashboard/agents/{agent_id}/credentials/revoke")
def dashboard_revoke_agent_credentials(agent_id: str, request: Request, session: Session = Depends(get_session), settings: Settings = Depends(get_settings)):
    account = _require_dashboard_account(request, session, settings)
    if isinstance(account, RedirectResponse):
        return account
    agent = _require_owned_agent(agent_id, account, session)
    revoked = _revoke_agent_credentials(session, agent)
    session.commit()
    return JSONResponse({"agent_id": agent.id, "bot_id": agent.bot_id, "credentials_revoked": revoked})


@app.post("/dashboard/agents/{agent_id}/delete")
def dashboard_delete_agent(agent_id: str, request: Request, session: Session = Depends(get_session), settings: Settings = Depends(get_settings)):
    account = _require_dashboard_account(request, session, settings)
    if isinstance(account, RedirectResponse):
        return account
    agent = _require_owned_agent(agent_id, account, session)
    agent.status = "deleted"
    session.add(agent)
    revoked = _revoke_agent_credentials(session, agent)
    invalidated = _invalidate_agent_install_links(session, agent)
    session.commit()
    return JSONResponse({"agent_id": agent.id, "bot_id": agent.bot_id, "status": agent.status, "credentials_revoked": revoked, "install_links_invalidated": invalidated})


def _require_owned_payment_session(payment_session_id: str, account: Account, session: Session) -> tuple[PaymentSession, Quote, AgentProfile]:
    ps = session.get(PaymentSession, payment_session_id)
    if not ps:
        raise HTTPException(404, "payment session not found")
    q = session.get(Quote, ps.quote_id)
    if not q:
        raise HTTPException(404, "quote not found")
    agent = session.exec(select(AgentProfile).where(AgentProfile.bot_id == q.bot_id)).first()
    if not agent or agent.owner_id != account.id:
        raise HTTPException(404, "payment session not found")
    return ps, q, agent


@app.post("/dashboard/payment-sessions/{payment_session_id}/refund")
async def dashboard_request_refund(payment_session_id: str, request: Request, session: Session = Depends(get_session), settings: Settings = Depends(get_settings)):
    account = _require_dashboard_account(request, session, settings)
    if isinstance(account, RedirectResponse):
        return account
    ps, q, agent = _require_owned_payment_session(payment_session_id, account, session)
    existing = session.exec(select(FulfillmentEvent).where(FulfillmentEvent.quote_id == q.id, FulfillmentEvent.status.in_(["refunded", "refund_requested"]))).first()
    if ps.status in {"refunded", "refund_pending"} or q.status in {"refunded", "refund_requested"} or existing:
        return RedirectResponse(f"/dashboard/agents/{agent.id}", status_code=303)
    ev_status = "refund_requested"
    metadata = {"provider": ps.provider, "payment_session_id": ps.id, "source": "dashboard", "support_required": True}
    if ps.provider == "stripe" and ps.status == "paid" and q.status == "failed" and settings.stripe_secret_key:
        try:
            refund_id, refund_status = create_stripe_refund(ps, q, settings, reason="dashboard_failed_action")
            ev_status = "refunded"
            ps.status = "refunded"
            q.status = "refunded"
            metadata = {"provider": "stripe", "refund_status": refund_status, "payment_session_id": ps.id, "source": "dashboard"}
            if refund_id:
                metadata["refund_id"] = refund_id
        except Exception:
            session.rollback()
            ps, q, agent = _require_owned_payment_session(payment_session_id, account, session)
            ps.status = "refund_pending"
            q.status = "refund_requested"
    else:
        ps.status = "refund_pending"
        q.status = "refund_requested"
    session.add(ps); session.add(q)
    session.add(FulfillmentEvent(id=f"ful_{uuid4().hex}", quote_id=q.id, status=ev_status, metadata_json=metadata))
    session.commit()
    return RedirectResponse(f"/dashboard/agents/{agent.id}", status_code=303)


def _enforce_stripe_checkout_guardrails(settings: Settings) -> None:
    if not settings.stripe_secret_key:
        raise HTTPException(status_code=503, detail="PAYJENT_STRIPE_SECRET_KEY is required for Stripe checkout")
    if settings.is_production and not settings.stripe_webhook_secret:
        raise HTTPException(
            status_code=503,
            detail="PAYJENT_STRIPE_WEBHOOK_SECRET is required for Stripe checkout in production",
        )


def _reject_legacy_minimum_or_topup_pricing(cost_breakdown: list[Any]) -> None:
    for item in cost_breakdown:
        label = ""
        if isinstance(item, dict):
            label = str(item.get("label", ""))
        else:
            label = str(getattr(item, "label", ""))
        normalized = label.lower()
        if any(term in normalized for term in ("stripe minimum", "minimum/top-up", "minimum top-up", "top-up", "top up")):
            raise HTTPException(
                status_code=422,
                detail="cost_breakdown must use the exact provider quote; minimum/top-up pricing is not allowed",
            )


@app.post("/api/v1/quotes", response_model=QuoteRead)
def create_quote(payload: QuoteCreate, session: Session = Depends(get_session), settings: Settings = Depends(get_settings), credential: BotCredential = Depends(require_bot_credential)):
    _enforce_bot_scope(credential, payload.bot_id)
    try:
        validate_breakdown(payload.amount_minor, payload.cost_breakdown)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    _reject_legacy_minimum_or_topup_pricing(payload.cost_breakdown)
    request_hash = payload.request_hash or quote_hash({
        "bot_id": payload.bot_id,
        "external_user_id": payload.external_user_id,
        "request_summary": payload.request_summary,
        "execution_envelope": payload.execution_envelope,
    })
    cost_breakdown = [i.model_dump() for i in payload.cost_breakdown]
    canonical = {
        "bot_id": payload.bot_id,
        "external_user_id": payload.external_user_id,
        "request_summary": payload.request_summary,
        "request_hash": request_hash,
        "amount_minor": payload.amount_minor,
        "currency": payload.currency.upper(),
        "cost_breakdown": cost_breakdown,
        "execution_envelope": attach_pricing_allocation(payload.execution_envelope, cost_breakdown),
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


STRIPE_MINIMUM_CHARGE_MINOR_BY_CURRENCY = {
    "USD": 50,
}


def _enforce_checkout_amount_supported(q: Quote, requested_provider: str) -> None:
    if requested_provider != "stripe":
        return
    minimum = STRIPE_MINIMUM_CHARGE_MINOR_BY_CURRENCY.get(q.currency.upper())
    if minimum is not None and q.amount_minor < minimum:
        raise HTTPException(
            status_code=422,
            detail=(
                f"Stripe checkout minimum for {q.currency.upper()} is {minimum} minor units; "
                "obtain an exact provider quote at or above the card checkout minimum, or batch/top up the paid action before creating checkout"
            ),
        )


def _create_checkout_for_quote(
    q: Quote,
    *,
    idempotency_key: str | None,
    provider: str | None,
    session: Session,
    settings: Settings,
) -> PaymentSession:
    _validate_managed_execution_envelope(q.execution_envelope, settings)
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
    if requested_provider not in {"mock", "local", "stripe", "decal", "link"}:
        raise HTTPException(status_code=422, detail="unsupported checkout provider")
    _enforce_checkout_amount_supported(q, requested_provider)
    if settings.is_production and requested_provider in {"mock", "local"}:
        safe_internal_hosted_smoke = (
            settings.hosted_smoke_test_rail_enabled
            and q.external_user_id == "hosted-smoke-user"
            and q.request_summary.startswith("Payjent hosted smoke:")
        )
        if not safe_internal_hosted_smoke:
            raise HTTPException(status_code=503, detail="active checkout provider not configured")
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
    if requested_provider == "decal":
        provider_session_id, hosted_url = create_decal_checkout_session(q, ps, settings)
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
    _validate_managed_execution_envelope(payload.execution_envelope, settings)
    q = create_quote(payload, session=session, settings=settings, credential=credential)
    stored_quote = session.get(Quote, q.id)
    if not stored_quote:
        raise HTTPException(500, "agent action quote was not persisted")
    ps = _create_checkout_for_quote(stored_quote, idempotency_key=idempotency_key, provider=provider, session=session, settings=settings)
    return create_paid_action_response(quote=stored_quote, payment_session=ps)


def _reject_secret_headers(headers: dict[str, str] | None) -> None:
    for name in (headers or {}):
        normalized = str(name).lower().replace("-", "_")
        if any(marker in normalized for marker in _SECRET_HEADER_MARKERS):
            raise HTTPException(status_code=422, detail=f"x402 action envelope may not store secret-like outbound header: {name}")


def _validate_generic_x402_envelope(envelope: dict, settings: Settings) -> None:
    # Payjent is not executing the target, so do not require the managed-execution allowlist.
    # Still reject unsafe/private URLs and secret-like headers in the stored request envelope.
    if envelope.get("service_url"):
        ok, reason = _safe_public_https_url(envelope.get("service_url"))
        if not ok:
            raise HTTPException(status_code=422, detail=reason)
    _reject_secret_headers(envelope.get("headers") or {})


_FAL_EXTERNAL_RUNTIME_OPT_IN_DETAIL = {
    "code": "external_runtime_opt_in_required",
    "recommended_tool_id": "fal.image.generate",
    "required_argument": "external_runtime",
    "guidance": "Use /api/v1/toolbox/fal.image.generate/checkout for normal Payjent-managed FAL image generation. Set provider_metadata.external_runtime=true or execution_readiness.external_runtime=true only when intentionally using the external pay.sh/x402 FAL runtime.",
}


def _truthy_external_runtime_opt_in(payload: Any) -> bool:
    for source in (getattr(payload, "provider_metadata", None), getattr(payload, "execution_readiness", None)):
        if isinstance(source, dict) and source.get("external_runtime") is True:
            return True
    return False


def _is_fal_external_runtime_target(*, service_url: str | None = None, target_url: str | None = None, service_fqn: str | None = None, provider: str | None = None) -> bool:
    if service_fqn and service_fqn.strip().lower() == FAL_LEGACY_SERVICE_FQN:
        return True
    for url in (service_url, target_url):
        if not url:
            continue
        host = (urlparse(url).netloc or "").lower()
        if host == "fal.mpp.tempo.xyz" or url.rstrip("/").lower().startswith(FAL_MPP_TEMPO_BASE_URL):
            return True
        if (provider and "fal" in provider.strip().lower().replace("-", "_")) and (host.endswith(".paysponge.com") or host == "paysponge.com"):
            return True
    return False


def _require_external_runtime_opt_in_for_fal_target(payload: Any, *, service_url: str | None = None, target_url: str | None = None, service_fqn: str | None = None, provider: str | None = None) -> None:
    if _is_fal_external_runtime_target(service_url=service_url, target_url=target_url, service_fqn=service_fqn, provider=provider) and not _truthy_external_runtime_opt_in(payload):
        raise HTTPException(status_code=422, detail=dict(_FAL_EXTERNAL_RUNTIME_OPT_IN_DETAIL))


def _create_x402_action_from_payload(
    payload: X402PaidActionCreate,
    *,
    idempotency_key: str | None,
    provider: str | None,
    session: Session,
    settings: Settings,
    credential: BotCredential,
    strict_generic: bool,
) -> dict:
    if strict_generic and (payload.payjent_fulfillment_callback or payload.payjent_managed_execution):
        raise HTTPException(status_code=422, detail="generic x402 actions are authorization-only; Payjent does not execute target_url/service_url")
    service_url = payload.service_url or payload.target_url
    _require_external_runtime_opt_in_for_fal_target(
        payload,
        service_url=service_url,
        target_url=payload.target_url,
        service_fqn=payload.service_fqn,
        provider=payload.provider,
    )
    try:
        envelope = build_paysh_execution_envelope(
            service_url=service_url,
            service_fqn=payload.service_fqn,
            resource=payload.resource,
            method=payload.method,
            body=payload.body,
            headers=payload.headers,
            description=payload.description or payload.request_summary,
            payjent_fulfillment_callback=False,
            payjent_managed_execution=False,
        )
        envelope["provider_metadata"] = dict(payload.provider_metadata or {})
        envelope["rail"] = payload.rail or "x402"
        envelope["task_budget_id"] = payload.task_budget_id
        if strict_generic:
            _validate_generic_x402_envelope(envelope, settings)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    readiness = dict(payload.provider_metadata or {})
    readiness.update(payload.execution_readiness or {})
    requested_provider = (provider or settings.checkout_provider or "mock").lower()
    enforce_readiness(session, bot_id=payload.bot_id, provider="pay_sh", readiness_mode=payload.readiness_mode, metadata=readiness)
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
    if payload.task_budget_id or (payload.currency.upper() == "USD" and payload.amount_minor < 50):
        qread = create_quote(action_payload, session=session, settings=settings, credential=credential)
        q = session.get(Quote, qread.id)
        if not q:
            raise HTTPException(500, "x402 action quote was not persisted")
        _reserve_task_budget_for_action(payload, q, session)
        ps = PaymentSession(id=f"ps_{uuid4().hex}", quote_id=q.id, provider="task_budget", status="checkout_created", checkout_url=None, idempotency_key=idempotency_key)
        session.add(ps); session.commit(); session.refresh(ps)
        _issue_paid_session(session, ps, settings, provider="task_budget")
        q.status = "paid"
        session.add(q); session.commit(); session.refresh(q); session.refresh(ps)
        resp = create_paid_action_response(quote=q, payment_session=ps)
        data = resp if isinstance(resp, dict) else resp.model_dump()
    else:
        action = create_agent_action(
            action_payload,
            idempotency_key=idempotency_key,
            provider=provider,
            session=session,
            settings=settings,
            credential=credential,
        )
        data = action if isinstance(action, dict) else action.model_dump()
    return {
        **data,
        "provider": "pay_sh",
        "premium_provider": "pay_sh",
        "command_preview": envelope["command_preview"],
        "request_fingerprint": data["request_hash"],
        "execution_boundary": envelope["payjent_execution_boundary"],
        "provider_metadata": envelope.get("provider_metadata") or {},
    }


def _validate_premium_action_envelope(payload: PremiumActionCreate, envelope: dict) -> None:
    if payload.payjent_fulfillment_callback or payload.payjent_managed_execution:
        raise HTTPException(status_code=422, detail="generic premium actions are authorization-only; Payjent does not execute target_url/service_url")
    for url in (payload.service_url, payload.target_url):
        if url:
            ok, reason = _safe_public_https_url(url)
            if not ok:
                raise HTTPException(status_code=422, detail=reason)
    _reject_secret_headers(payload.headers)
    if not (payload.service_url or payload.target_url or payload.body or payload.provider_metadata or payload.provider != "generic"):
        raise HTTPException(status_code=422, detail="premium action must include a safe target_url/service_url or provider/body/provider_metadata describing the provider-backed action")


def _premium_command_preview(payload: PremiumActionCreate, service_url: str | None) -> str:
    method = (payload.method or "POST").upper()
    target = service_url or f"provider:{payload.provider}"
    return f"{method} {target} (agent executes externally after Payjent authorization)"


def _budget_to_read(b: TaskBudget) -> dict:
    return {
        "id": b.id, "bot_id": b.bot_id, "external_user_id": b.external_user_id, "task_id": b.task_id,
        "max_amount_minor": b.max_amount_minor, "currency": b.currency, "available_minor": b.available_minor,
        "reserved_minor": b.reserved_minor, "captured_minor": b.captured_minor, "refunded_minor": b.refunded_minor,
        "released_minor": b.released_minor, "status": b.status, "provider": b.provider, "checkout_url": b.checkout_url,
    }


@app.post("/api/v1/task-budgets", response_model=TaskBudgetRead)
def create_task_budget(payload: TaskBudgetCreate, session: Session = Depends(get_session), credential: BotCredential = Depends(require_bot_credential)):
    _enforce_bot_scope(credential, payload.bot_id)
    b = TaskBudget(id=f"tb_{uuid4().hex}", bot_id=payload.bot_id, external_user_id=payload.external_user_id, task_id=payload.task_id, max_amount_minor=payload.max_amount_minor, currency=payload.currency.upper(), status="created")
    session.add(b); session.commit(); session.refresh(b)
    return _budget_to_read(b)


@app.get("/api/v1/task-budgets/{budget_id}", response_model=TaskBudgetRead)
def get_task_budget(budget_id: str, session: Session = Depends(get_session), credential: BotCredential = Depends(require_bot_credential)):
    b = session.get(TaskBudget, budget_id)
    if not b: raise HTTPException(404, "task budget not found")
    _enforce_bot_scope(credential, b.bot_id)
    return _budget_to_read(b)


@app.post("/api/v1/task-budgets/{budget_id}/checkout", response_model=TaskBudgetFundResponse)
def checkout_task_budget(budget_id: str, provider: str | None = Header(default=None, alias="X-Payjent-Provider"), session: Session = Depends(get_session), credential: BotCredential = Depends(require_bot_credential)):
    b = session.get(TaskBudget, budget_id)
    if not b: raise HTTPException(404, "task budget not found")
    _enforce_bot_scope(credential, b.bot_id)
    b.provider = (provider or "stripe").lower(); b.checkout_url = f"https://payjent.com/api/v1/task-budgets/{b.id}/mock-fund"; b.status = "checkout_created"
    session.add(b); session.commit(); session.refresh(b)
    return {"budget": _budget_to_read(b), "checkout_url": b.checkout_url, "message": "Fund this task budget once, then reference task_budget_id for micro premium actions."}


@app.post("/api/v1/task-budgets/{budget_id}/mock-fund", response_model=TaskBudgetRead)
def mock_fund_task_budget(budget_id: str, session: Session = Depends(get_session), credential: BotCredential = Depends(require_operator_credential)):
    b = session.get(TaskBudget, budget_id)
    if not b: raise HTTPException(404, "task budget not found")
    if b.status not in {"active", "closed"}:
        b.available_minor = b.max_amount_minor - b.captured_minor - b.reserved_minor
        b.status = "active" if b.available_minor > 0 else "closed"
        b.provider = b.provider or "mock"; b.funded_at = b.funded_at or datetime.now(timezone.utc)
        session.add(b); session.commit(); session.refresh(b)
    return _budget_to_read(b)


@app.post("/api/v1/task-budgets/{budget_id}/release-unused", response_model=TaskBudgetRead)
def release_unused_task_budget(budget_id: str, session: Session = Depends(get_session), credential: BotCredential = Depends(require_bot_credential)):
    b = session.get(TaskBudget, budget_id)
    if not b: raise HTTPException(404, "task budget not found")
    _enforce_bot_scope(credential, b.bot_id)
    if b.available_minor > 0:
        amount = b.available_minor; b.available_minor = 0; b.released_minor += amount; b.status = "closed"
        session.add(TaskBudgetLedgerEntry(id=f"tbl_{uuid4().hex}", budget_id=b.id, operation_id=f"release_unused:{b.id}", amount_minor=amount, currency=b.currency, status="released", reason="release_unused"))
    session.add(b); session.commit(); session.refresh(b)
    return _budget_to_read(b)


def _reserve_task_budget_for_action(payload: PremiumActionCreate, q: Quote, session: Session) -> TaskBudget | None:
    if not payload.task_budget_id:
        return None
    b = session.get(TaskBudget, payload.task_budget_id)
    if not b or b.status != "active" or b.available_minor < payload.amount_minor:
        raise HTTPException(status_code=422, detail="task_budget_id must reference an active funded budget with sufficient available balance")
    if b.bot_id != payload.bot_id or b.external_user_id != payload.external_user_id or b.currency.upper() != payload.currency.upper():
        raise HTTPException(status_code=422, detail="task budget must match bot_id, external_user_id, and currency")
    b.available_minor -= payload.amount_minor; b.reserved_minor += payload.amount_minor
    session.add(TaskBudgetLedgerEntry(id=f"tbl_{uuid4().hex}", budget_id=b.id, quote_id=q.id, operation_id=f"reserve:{q.id}", amount_minor=payload.amount_minor, currency=b.currency, status="reserved", reason="premium_action_create"))
    session.add(b); session.commit(); session.refresh(b)
    return b


def _create_premium_action_from_payload(
    payload: PremiumActionCreate,
    *,
    idempotency_key: str | None,
    provider: str | None,
    session: Session,
    settings: Settings,
    credential: BotCredential,
) -> dict:
    service_url = payload.service_url or payload.target_url
    _require_external_runtime_opt_in_for_fal_target(
        payload,
        service_url=payload.service_url,
        target_url=payload.target_url,
        provider=payload.provider,
    )
    kind = payload.kind or payload.action_type or "premium_action"
    envelope = {
        "provider": payload.provider,
        "kind": kind,
        "target_url": payload.target_url,
        "service_url": payload.service_url,
        "method": (payload.method or "POST").upper(),
        "body": payload.body or {},
        "headers": payload.headers or {},
        "description": payload.description or payload.request_summary,
        "command_preview": _premium_command_preview(payload, service_url),
        "setup_hint": "Use the provider's external runtime/SDK/API after Payjent payment authorization; do not expose provider secrets to Payjent.",
        "settlement": "provider_external_runtime",
        "provider_metadata": dict(payload.provider_metadata or {}),
        "rail": payload.rail,
        "payjent_fulfillment_callback": False,
        "payjent_managed_execution": False,
        "payjent_execution_boundary": "agent_executes_after_payjent_authorization",
        "boundary": "agent_executes_after_payjent_authorization",
        "task_budget_id": payload.task_budget_id,
    }
    _validate_premium_action_envelope(payload, envelope)
    readiness = dict(payload.provider_metadata or {})
    readiness.update(payload.execution_readiness or {})
    enforce_readiness(session, bot_id=payload.bot_id, provider=payload.provider, readiness_mode=payload.readiness_mode, metadata=readiness)
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
    if payload.task_budget_id or (payload.currency.upper() == "USD" and payload.amount_minor < 50):
        qread = create_quote(action_payload, session=session, settings=settings, credential=credential)
        q = session.get(Quote, qread.id)
        if not q:
            raise HTTPException(500, "premium action quote was not persisted")
        _reserve_task_budget_for_action(payload, q, session)
        ps = PaymentSession(id=f"ps_{uuid4().hex}", quote_id=q.id, provider="task_budget", status="checkout_created", checkout_url=None, idempotency_key=idempotency_key)
        session.add(ps); session.commit(); session.refresh(ps)
        _issue_paid_session(session, ps, settings, provider="task_budget")
        q.status = "paid"
        session.add(q); session.commit(); session.refresh(q); session.refresh(ps)
        resp = create_paid_action_response(quote=q, payment_session=ps)
        data = resp if isinstance(resp, dict) else resp.model_dump()
    else:
        action = create_agent_action(
            action_payload,
            idempotency_key=idempotency_key,
            provider=provider,
            session=session,
            settings=settings,
            credential=credential,
        )
        data = action if isinstance(action, dict) else action.model_dump()
    return {
        **data,
        "provider": payload.provider,
        "premium_provider": payload.provider,
        "command_preview": envelope["command_preview"],
        "request_fingerprint": data["request_hash"],
        "execution_boundary": envelope["payjent_execution_boundary"],
        "provider_metadata": envelope["provider_metadata"],
    }


@app.post("/api/v1/premium-actions", response_model=PremiumActionCreateResponse)
def create_premium_action(
    payload: PremiumActionCreate,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    provider: str | None = Header(default=None, alias="X-Payjent-Provider"),
    session: Session = Depends(get_session),
    settings: Settings = Depends(get_settings),
    credential: BotCredential = Depends(require_bot_credential),
):
    return _create_premium_action_from_payload(payload, idempotency_key=idempotency_key, provider=provider, session=session, settings=settings, credential=credential)


@app.get("/api/v1/premium-action-presets")
def premium_action_presets(
    request: Request,
    settings: Settings = Depends(get_settings),
    _credential: BotCredential = Depends(require_bot_credential),
):
    base_url = _public_base_url(request, settings)
    premium_discovery = _premium_tool_discovery(base_url)
    return {
        "presets": list_presets(),
        "premium_tool_discovery": premium_discovery,
        "creation_template": premium_discovery["creation_template"],
    }


@app.post("/api/v1/premium-action-presets/{preset_id}/actions", response_model=PremiumActionCreateResponse)
def create_premium_action_from_preset(
    preset_id: str,
    payload: PremiumActionPresetActionCreate,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    provider: str | None = Header(default=None, alias="X-Payjent-Provider"),
    session: Session = Depends(get_session),
    settings: Settings = Depends(get_settings),
    credential: BotCredential = Depends(require_bot_credential),
):
    preset = get_preset(preset_id)
    envelope = preset.builder(dict(payload.input or {}))
    enforce_readiness(session, bot_id=payload.bot_id, provider=preset.provider, readiness_mode=payload.readiness_mode, metadata=payload.execution_readiness or payload.input)
    action_payload = AgentActionCreate(
        bot_id=payload.bot_id,
        external_user_id=payload.external_user_id,
        request_summary=payload.request_summary or envelope.get("description") or preset.name,
        request_hash=payload.request_hash,
        amount_minor=payload.amount_minor,
        currency=payload.currency,
        cost_breakdown=payload.cost_breakdown,
        execution_envelope=envelope,
        callback_url=payload.callback_url,
    )
    action = create_agent_action(action_payload, idempotency_key=idempotency_key, provider=provider, session=session, settings=settings, credential=credential)
    data = action if isinstance(action, dict) else action.model_dump()
    return {
        **data,
        "provider": preset.provider,
        "premium_provider": preset.provider,
        "command_preview": envelope["command_preview"],
        "request_fingerprint": data["request_hash"],
        "execution_boundary": PREMIUM_PRESET_EXECUTION_BOUNDARY,
        "provider_metadata": envelope.get("provider_metadata") or {},
    }


@app.post("/api/v1/premium-actions/x402", response_model=X402PaidActionCreateResponse)
def create_x402_paid_action(
    payload: X402PaidActionCreate,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    provider: str | None = Header(default=None, alias="X-Payjent-Provider"),
    session: Session = Depends(get_session),
    settings: Settings = Depends(get_settings),
    credential: BotCredential = Depends(require_bot_credential),
):
    return _create_x402_action_from_payload(
        payload,
        idempotency_key=idempotency_key,
        provider=provider,
        session=session,
        settings=settings,
        credential=credential,
        strict_generic=True,
    )


@app.post("/api/v1/premium-actions/pay-sh", response_model=PayShPremiumActionCreateResponse)
def create_pay_sh_premium_action(
    payload: PayShPremiumActionCreate,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    provider: str | None = Header(default=None, alias="X-Payjent-Provider"),
    session: Session = Depends(get_session),
    settings: Settings = Depends(get_settings),
    credential: BotCredential = Depends(require_bot_credential),
):
    return _create_x402_action_from_payload(payload, idempotency_key=idempotency_key, provider=provider, session=session, settings=settings, credential=credential, strict_generic=False)


@app.post("/api/v1/premium-actions/pay-sh/bigquery-query", response_model=PayShPremiumActionCreateResponse)
def create_bigquery_paid_query(
    payload: BigQueryPaidQueryCreate,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    provider: str | None = Header(default=None, alias="X-Payjent-Provider"),
    session: Session = Depends(get_session),
    settings: Settings = Depends(get_settings),
    credential: BotCredential = Depends(require_bot_credential),
):
    project_id = payload.project_id.strip()
    if any(ch in project_id for ch in ("/", "?", "#")):
        raise HTTPException(status_code=422, detail="project_id must be a BigQuery project id, not a path or URL")
    service_url = f"https://bigquery.google.gateway-402.com/bigquery/v2/projects/{project_id}/queries"
    summary = payload.request_summary or f"Run paid BigQuery query through pay.sh/x402 for project {project_id}"
    premium_payload = PayShPremiumActionCreate(
        bot_id=payload.bot_id,
        external_user_id=payload.external_user_id,
        request_summary=summary,
        request_hash=payload.request_hash,
        amount_minor=payload.amount_minor,
        currency=payload.currency,
        cost_breakdown=payload.cost_breakdown,
        service_url=service_url,
        service_fqn="solana-foundation/google/bigquery",
        resource="jobs",
        method="POST",
        body={"query": payload.query, "useLegacySql": payload.use_legacy_sql},
        headers={"Content-Type": "application/json"},
        description=(
            "Real pay.sh public catalog BigQuery jobs endpoint. Payjent gates Stripe payment "
            "and x402 spend authorization only; the agent executes externally with pay curl/pay.sh."
        ),
        payjent_fulfillment_callback=False,
        payjent_managed_execution=False,
        callback_url=payload.callback_url,
    )
    return create_pay_sh_premium_action(
        premium_payload,
        idempotency_key=idempotency_key,
        provider=provider,
        session=session,
        settings=settings,
        credential=credential,
    )


@app.post("/api/v1/purchase-actions", response_model=AgentActionCreateResponse)
def create_purchase_fulfillment_action(
    payload: PurchaseFulfillmentCreate,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    provider: str | None = Header(default=None, alias="X-Payjent-Provider"),
    session: Session = Depends(get_session),
    settings: Settings = Depends(get_settings),
    credential: BotCredential = Depends(require_bot_credential),
):
    if not payload.payjent_fulfillment_callback:
        raise HTTPException(status_code=422, detail="purchase actions require payjent_fulfillment_callback=true")
    secret_path = _purchase_secret_body_key_path(payload.body)
    if secret_path:
        raise HTTPException(status_code=422, detail=f"purchase fulfillment body may not include credential/card/password/shipping secret fields: {secret_path}")
    envelope = {
        "provider": "merchant_purchase",
        "category": "physical_or_digital_purchase_procurement",
        "service_url": payload.service_url,
        "method": "POST",
        "headers": payload.headers,
        "body": payload.body,
        "merchant": payload.merchant.model_dump(),
        "item": payload.item.model_dump(),
        "order_summary": payload.order_summary,
        "amount_minor": payload.amount_minor,
        "currency": payload.currency.upper(),
        "cost_breakdown": [item.model_dump() for item in payload.cost_breakdown],
        "payjent_fulfillment_callback": True,
        "money_flow": "User pays through Payjent; Payjent verifies payment and sends a verified callback to an allowlisted procurement executor. The executor buys from the merchant using its configured procurement/payment method; Payjent does not send funds to the agent or directly pay Amazon.",
    }
    _validate_purchase_executor_allowlisted(envelope, settings)
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
    return create_agent_action(
        action_payload,
        idempotency_key=idempotency_key,
        provider=provider,
        session=session,
        settings=settings,
        credential=credential,
    )


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
    fulfillment_events = session.exec(select(FulfillmentEvent).where(FulfillmentEvent.quote_id == q.id).order_by(FulfillmentEvent.created_at.desc())).all()
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
        "fulfillment_events": [{"id": ev.id, "status": ev.status, "metadata": ev.metadata_json, "created_at": ev.created_at.isoformat()} for ev in fulfillment_events],
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


def _budget_for_quote(q: Quote, session: Session) -> TaskBudget | None:
    budget_id = (q.execution_envelope or {}).get("task_budget_id")
    return session.get(TaskBudget, budget_id) if budget_id else None


def _transition_budget_reservation(q: Quote, session: Session, status: str, reason: str) -> bool:
    b = _budget_for_quote(q, session)
    if not b:
        return False
    existing = session.exec(select(TaskBudgetLedgerEntry).where(TaskBudgetLedgerEntry.budget_id == b.id, TaskBudgetLedgerEntry.operation_id == f"{status}:{q.id}")).first()
    if existing:
        return False
    reserve = session.exec(select(TaskBudgetLedgerEntry).where(TaskBudgetLedgerEntry.budget_id == b.id, TaskBudgetLedgerEntry.operation_id == f"reserve:{q.id}")).first()
    amount = reserve.amount_minor if reserve else q.amount_minor
    if status == "captured":
        b.reserved_minor = max(0, b.reserved_minor - amount); b.captured_minor += amount
    elif status == "released":
        b.reserved_minor = max(0, b.reserved_minor - amount); b.available_minor += amount; b.released_minor += amount
    elif status == "refunded":
        b.reserved_minor = max(0, b.reserved_minor - amount); b.available_minor += amount; b.refunded_minor += amount
    if b.status == "closed" and b.available_minor > 0:
        b.status = "active"
    session.add(TaskBudgetLedgerEntry(id=f"tbl_{uuid4().hex}", budget_id=b.id, quote_id=q.id, operation_id=f"{status}:{q.id}", amount_minor=amount, currency=b.currency, status=status, reason=reason))
    session.add(b); session.commit()
    return True


@app.post("/api/v1/agent-actions/{action_id}/complete", response_model=AgentActionCompleteResponse)
def complete_agent_action(action_id: str, payload: FulfillmentCreate, session: Session = Depends(get_session), credential: BotCredential = Depends(require_bot_credential)):
    event = record_fulfillment(action_id, payload, session=session, credential=credential)
    q = session.get(Quote, action_id)
    if q and payload.status == "fulfilled":
        _transition_budget_reservation(q, session, "captured", "agent_action_complete")
    elif q and payload.status in {"failed", "refunded"}:
        _transition_budget_reservation(q, session, "released", "agent_action_not_fulfilled")
    stored = session.get(FulfillmentEvent, event.id)
    if not stored:
        raise HTTPException(500, "agent action fulfillment was not persisted")
    return action_result_response(stored)


def _safe_failure_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
    blocked = ("secret", "token", "key", "authorization", "cookie", "password", "credential")
    def scrub(value: Any) -> Any:
        if isinstance(value, dict):
            safe: dict[str, Any] = {}
            for key, nested in value.items():
                if any(marker in str(key).lower() for marker in blocked):
                    continue
                safe[str(key)] = scrub(nested)
            return safe
        if isinstance(value, list):
            return [scrub(item) for item in value]
        if isinstance(value, (str, int, float, bool, type(None))):
            return value
        return str(value)
    return scrub(metadata or {})


@app.post("/api/v1/agent-actions/{action_id}/fail", response_model=AgentActionFailResponse)
def fail_agent_action(action_id: str, payload: AgentActionFailRequest, session: Session = Depends(get_session), settings: Settings = Depends(get_settings), credential: BotCredential = Depends(require_bot_credential)):
    q = session.get(Quote, action_id)
    if not q:
        raise HTTPException(404, "quote not found")
    _enforce_bot_scope(credential, q.bot_id)
    budget_release_attempted = _transition_budget_reservation(q, session, "released", "agent_action_failed")
    ps = session.exec(select(PaymentSession).where(PaymentSession.quote_id == q.id).order_by(PaymentSession.created_at.desc())).first()
    existing_refund = session.exec(select(FulfillmentEvent).where(FulfillmentEvent.quote_id == q.id, FulfillmentEvent.status == "refunded")).first()
    if q.status == "refunded" and payload.refund and (existing_refund or (ps and ps.status == "refunded")):
        return AgentActionFailResponse(action_id=q.id, quote_id=q.id, fulfillment_id=(existing_refund.id if existing_refund else q.id), status="failed", metadata=(existing_refund.metadata_json if existing_refund else {}), refund_status="already_refunded", refund_id=(existing_refund.metadata_json.get("refund_id") if existing_refund and isinstance(existing_refund.metadata_json, dict) else None), payment_status=(ps.status if ps else "refunded"), quote_status=q.status, message="Action was already refunded.")
    if q.status in {"fulfilled", "refunded"}:
        raise HTTPException(status_code=409, detail="fulfilled or refunded actions cannot be failed/refunded")
    if not ps or ps.status not in {"paid", "refunded"}:
        raise HTTPException(status_code=409, detail="action must have a paid payment session before failure/refund")
    existing_failed = session.exec(select(FulfillmentEvent).where(FulfillmentEvent.quote_id == q.id, FulfillmentEvent.status == "failed")).first()
    if not existing_failed:
        ev = FulfillmentEvent(id=f"ful_{uuid4().hex}", quote_id=q.id, status="failed", metadata_json={"reason": payload.reason, **_safe_failure_metadata(payload.metadata)})
        q.status = "failed"
        session.add(q); session.add(ev); session.commit(); session.refresh(ev)
    else:
        ev = existing_failed
    refund_status = "not_requested"
    refund_id = None
    message = "Action marked failed."
    if payload.refund:
        if existing_refund or ps.status == "refunded" or q.status == "refunded":
            refund_status = "already_refunded"
            message = "Action was already refunded."
        elif ps.status != "paid":
            raise HTTPException(status_code=409, detail="payment session must be paid before refund")
        elif ps.provider in {"mock", "local"}:
            refund_id = f"mock_ref_{uuid4().hex}"
            refund_status = "succeeded"
            rev = FulfillmentEvent(id=f"ful_{uuid4().hex}", quote_id=q.id, status="refunded", metadata_json={"provider": ps.provider, "refund_id": refund_id, "refund_status": refund_status, "payment_session_id": ps.id, "reason": payload.reason})
            ps.status = "refunded"; q.status = "refunded"
            session.add(ps); session.add(q); session.add(rev); session.commit()
            message = "Mock/local payment marked refunded."
        elif ps.provider == "stripe":
            try:
                refund_id, refund_status = create_stripe_refund(ps, q, settings, reason=payload.reason)
                rev = FulfillmentEvent(id=f"ful_{uuid4().hex}", quote_id=q.id, status="refunded", metadata_json={"provider": "stripe", "refund_id": refund_id, "refund_status": refund_status, "payment_session_id": ps.id, "reason": payload.reason})
                ps.status = "refunded"; q.status = "refunded"
                session.add(ps); session.add(q); session.add(rev); session.commit()
                message = "Stripe refund created and payment marked refunded."
            except HTTPException:
                refund_status = "manual_review_required"
                message = "Automatic Stripe refund unavailable; manual review required."
        else:
            refund_status = "manual_review_required"
            message = f"Automatic refund unsupported for payment provider '{ps.provider}'."
    session.refresh(q); session.refresh(ps)
    return AgentActionFailResponse(action_id=q.id, quote_id=q.id, fulfillment_id=ev.id, status="failed", metadata=ev.metadata_json, refund_status=refund_status, refund_id=refund_id, payment_status=ps.status, quote_status=q.status, message=message)


@app.get("/api/v1/payment-sessions/{session_id}", response_model=PaymentSessionRead)
def get_payment_session(session_id: str, session: Session = Depends(get_session)):
    ps = session.get(PaymentSession, session_id)
    if not ps: raise HTTPException(404, "payment session not found")
    return session_to_read(ps)


def _resume_event_payload(q: Quote, ps: PaymentSession, provider: str) -> dict[str, Any]:
    return {
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
        "resume_hint": {
            "poll_url": f"/api/v1/agents/{q.bot_id}/resume-events",
            "consume_url": f"/api/v1/agent-actions/{q.id}/start",
            "instruction": "Poll/consume the Payjent action grant for this action_id before executing; this event is a readiness signal only.",
        },
    }


def _event_to_read(event: ResumeEvent) -> ResumeEventRead:
    return ResumeEventRead(
        id=event.id,
        event_type=event.event_type,
        action_id=event.action_id,
        quote_id=event.quote_id,
        payment_session_id=event.payment_session_id,
        bot_id=event.bot_id,
        status=event.status,
        payload=event.payload,
        callback_status=event.callback_status,
        created_at=event.created_at.isoformat(),
    )


def _enqueue_resume_event(session: Session, q: Quote, ps: PaymentSession, settings: Settings, provider: str) -> ResumeEvent:
    existing = session.exec(select(ResumeEvent).where(ResumeEvent.payment_session_id == ps.id, ResumeEvent.event_type == "agent_action.ready")).first()
    if existing:
        return existing
    payload = _resume_event_payload(q, ps, provider)
    timestamp, signature = sign_webhook_payload(payload, settings.signing_secret)
    event = ResumeEvent(
        id=f"re_{uuid4().hex}", bot_id=q.bot_id, quote_id=q.id, action_id=q.id,
        payment_session_id=ps.id, event_type="agent_action.ready", status="ready",
        payload=payload, callback_url=q.callback_url, signature=signature, signature_timestamp=timestamp,
    )
    session.add(event)
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        existing = session.exec(select(ResumeEvent).where(ResumeEvent.payment_session_id == ps.id, ResumeEvent.event_type == "agent_action.ready")).first()
        if existing:
            return existing
        raise
    session.refresh(event)
    return event


def _deliver_resume_event_callback(session: Session, event: ResumeEvent, settings: Settings) -> WebhookDeliveryAttempt | None:
    if not event.callback_url:
        return None
    if event.callback_attempt_id and event.callback_status == "success":
        return session.get(WebhookDeliveryAttempt, event.callback_attempt_id)
    timestamp = event.signature_timestamp
    signature = event.signature
    if not timestamp or not signature:
        timestamp, signature = sign_webhook_payload(event.payload, settings.signing_secret)
        event.signature_timestamp = timestamp
        event.signature = signature
    attempt = WebhookDeliveryAttempt(
        id=f"wh_{uuid4().hex}", quote_id=event.quote_id, action_id=event.action_id,
        payment_session_id=event.payment_session_id, callback_url=event.callback_url,
        status="pending", payload=event.payload,
    )
    try:
        with httpx.Client(timeout=5.0) as client:
            response = client.post(event.callback_url, json=event.payload, headers={PAYJENT_TIMESTAMP_HEADER: timestamp, PAYJENT_SIGNATURE_HEADER: signature})
        attempt.http_status = response.status_code
        attempt.status = "success" if 200 <= response.status_code < 300 else "failed"
        if attempt.status == "failed":
            attempt.error = response.text[:500]
    except Exception as exc:
        attempt.status = "failed"
        attempt.error = str(exc)[:500]
    event.callback_attempt_id = attempt.id
    event.callback_status = attempt.status
    event.updated_at = datetime.now(timezone.utc)
    session.add(attempt)
    session.add(event)
    session.commit()
    session.refresh(attempt)
    return attempt


def _deliver_agent_action_callback(session: Session, q: Quote, ps: PaymentSession, settings: Settings, provider: str) -> WebhookDeliveryAttempt | None:
    event = _enqueue_resume_event(session, q, ps, settings, provider)
    return _deliver_resume_event_callback(session, event, settings)


@app.get("/api/v1/agents/{bot_id}/resume-events", response_model=ResumeEventListResponse)
def list_resume_events(bot_id: str, session: Session = Depends(get_session), credential: BotCredential = Depends(require_bot_credential)):
    _enforce_bot_scope(credential, bot_id)
    events = session.exec(
        select(ResumeEvent).where(
            ResumeEvent.bot_id == bot_id,
            ResumeEvent.event_type == "agent_action.ready",
            ResumeEvent.status == "ready",
            ResumeEvent.acked_at.is_(None),
        ).order_by(ResumeEvent.created_at)
    ).all()
    return ResumeEventListResponse(events=[_event_to_read(event) for event in events])


@app.post("/api/v1/resume-events/{event_id}/ack", response_model=ResumeEventAckResponse)
def ack_resume_event(event_id: str, session: Session = Depends(get_session), credential: BotCredential = Depends(require_bot_credential)):
    event = session.get(ResumeEvent, event_id)
    if not event:
        raise HTTPException(404, "resume event not found")
    _enforce_bot_scope(credential, event.bot_id)
    if event.acked_at is None:
        event.acked_at = datetime.now(timezone.utc)
        event.status = "acked"
        event.updated_at = event.acked_at
        session.add(event)
        session.commit()
        session.refresh(event)
    return ResumeEventAckResponse(id=event.id, status=event.status, acked=True)


@app.post("/api/v1/resume-events/{event_id}/retry", response_model=ResumeEventRead)
def retry_resume_event(event_id: str, session: Session = Depends(get_session), settings: Settings = Depends(get_settings), credential: BotCredential = Depends(require_bot_credential)):
    event = session.get(ResumeEvent, event_id)
    if not event:
        raise HTTPException(404, "resume event not found")
    _enforce_bot_scope(credential, event.bot_id)
    if event.acked_at is not None:
        raise HTTPException(status_code=409, detail="resume event already acked")
    _deliver_resume_event_callback(session, event, settings)
    session.refresh(event)
    return _event_to_read(event)


_SECRET_HEADER_MARKERS = ("authorization", "cookie", "token", "secret", "key", "credential")
_INTERNAL_HOSTS = {"localhost", "localhost.localdomain", "metadata.google.internal"}
_RESERVED_DOWNSTREAM_BODY_KEYS = {"grant", "grant_id", "payment", "payment_token", "receipt", "token"}
_PURCHASE_SECRET_BODY_KEY_MARKERS = ("card", "cvv", "cvc", "pan", "password", "passcode", "credential", "amazon_login", "amazon_password", "shipping_address", "address", "ssn", "secret", "auth_token", "access_token")


def _safe_public_https_url(url: str | None) -> tuple[bool, str | None]:
    parsed = urlparse(url or "")
    if parsed.scheme.lower() != "https":
        return False, "service_url must use https"
    if not parsed.hostname:
        return False, "service_url missing hostname"
    host = parsed.hostname.strip().lower().rstrip(".")
    if host in _INTERNAL_HOSTS or host.endswith(".localhost") or host.endswith(".local") or host.endswith(".internal"):
        return False, "service_url hostname is internal"
    try:
        ips = set()
        try:
            ips.add(ipaddress.ip_address(host))
        except ValueError:
            for info in socket.getaddrinfo(host, parsed.port or 443, type=socket.SOCK_STREAM):
                ips.add(ipaddress.ip_address(info[4][0]))
        if any(ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_reserved or ip.is_unspecified for ip in ips):
            return False, "service_url resolves to a non-public address"
    except socket.gaierror:
        return False, "service_url hostname did not resolve"
    except ValueError:
        return False, "service_url hostname resolution failed"
    return True, None


def _managed_execution_host_allowed(url: str | None, settings: Settings) -> tuple[bool, str | None]:
    parsed = urlparse(url or "")
    if parsed.scheme.lower() != "https":
        return False, "service_url must use https"
    if not parsed.hostname:
        return False, "service_url missing hostname"
    host = parsed.hostname.strip().lower().rstrip(".")
    allowed_hosts = settings.managed_execution_allowed_host_set
    if allowed_hosts:
        return (True, None) if host in allowed_hosts else (False, "service_url hostname is not allowed for fulfillment callback")
    if settings.is_production:
        return False, "PAYJENT_MANAGED_EXECUTION_ALLOWED_HOSTS must include fulfillment callback/executor service_url hostname in production"
    return _safe_public_https_url(url)


def _reserved_downstream_body_key_path(value, path: str = "body") -> str | None:
    if isinstance(value, dict):
        for key, child in value.items():
            child_path = f"{path}.{key}"
            if str(key).lower() in _RESERVED_DOWNSTREAM_BODY_KEYS:
                return child_path
            found = _reserved_downstream_body_key_path(child, child_path)
            if found:
                return found
    elif isinstance(value, list):
        for index, child in enumerate(value):
            found = _reserved_downstream_body_key_path(child, f"{path}[{index}]")
            if found:
                return found
    return None


def _purchase_secret_body_key_path(value, path: str = "body") -> str | None:
    if isinstance(value, dict):
        for key, child in value.items():
            child_path = f"{path}.{key}"
            normalized = str(key).lower().replace("-", "_")
            if any(marker in normalized for marker in _PURCHASE_SECRET_BODY_KEY_MARKERS):
                return child_path
            found = _purchase_secret_body_key_path(child, child_path)
            if found:
                return found
    elif isinstance(value, list):
        for index, child in enumerate(value):
            found = _purchase_secret_body_key_path(child, f"{path}[{index}]")
            if found:
                return found
    return None


def _fulfillment_callback_requested(envelope: dict | None) -> bool:
    if not envelope:
        return False
    return bool(envelope.get("payjent_fulfillment_callback") or envelope.get("payjent_managed_execution"))


def _validate_managed_execution_envelope(envelope: dict | None, settings: Settings) -> None:
    if not _fulfillment_callback_requested(envelope):
        return
    if (envelope.get("method") or "POST").upper() != "POST":
        raise HTTPException(status_code=422, detail="only POST downstream execution is supported")
    ok, reason = _managed_execution_host_allowed(envelope.get("service_url"), settings)
    if not ok:
        raise HTTPException(status_code=422, detail=reason)
    reserved_path = _reserved_downstream_body_key_path(envelope.get("body") or {})
    if reserved_path:
        raise HTTPException(status_code=422, detail=f"fulfillment callback body may not include reserved Payjent field: {reserved_path}")


def _validate_purchase_executor_allowlisted(envelope: dict, settings: Settings) -> None:
    if not settings.managed_execution_allowed_host_set:
        raise HTTPException(status_code=422, detail="purchase fulfillment requires PAYJENT_MANAGED_EXECUTION_ALLOWED_HOSTS to include the procurement executor hostname")
    _validate_managed_execution_envelope(envelope, settings)


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


def _stripe_event_amount_currency(event_type: str | None, data_object: dict) -> tuple[int | None, str | None]:
    if event_type == "checkout.session.completed":
        amount = data_object.get("amount_total")
    else:
        amount = data_object.get("amount_received", data_object.get("amount"))
    currency = data_object.get("currency")
    return amount, currency.upper() if isinstance(currency, str) else None


def _validate_stripe_paid_event(session: Session, ps: PaymentSession, data_object: dict, event_type: str | None) -> None:
    if ps.provider != "stripe":
        raise HTTPException(status_code=409, detail="payment session provider is not stripe")
    provider_event_id = data_object.get("id")
    if provider_event_id and ps.provider_session_id and provider_event_id != ps.provider_session_id:
        raise HTTPException(status_code=409, detail="Stripe provider_session_id mismatch")
    q = session.get(Quote, ps.quote_id)
    if not q:
        raise HTTPException(404, "quote not found")
    amount_minor, currency = _stripe_event_amount_currency(event_type, data_object)
    if amount_minor is None:
        raise HTTPException(status_code=409, detail="Stripe amount missing")
    if currency is None:
        raise HTTPException(status_code=409, detail="Stripe currency missing")
    if int(amount_minor) != q.amount_minor:
        raise HTTPException(status_code=409, detail="Stripe amount mismatch")
    if currency != q.currency.upper():
        raise HTTPException(status_code=409, detail="Stripe currency mismatch")


def _decal_session_object(payload: dict) -> dict:
    return payload.get("session", payload) if isinstance(payload, dict) else {}


def _decal_amount_paid_minor(session_object: dict) -> int | None:
    amounts = session_object.get("order", {}).get("amounts", {}) if isinstance(session_object.get("order"), dict) else {}
    paid = amounts.get("paid")
    return int(paid) if paid is not None else None


def _validate_decal_paid_session(db_session: Session, ps: PaymentSession, decal_session: dict) -> None:
    if ps.provider != "decal":
        raise HTTPException(status_code=409, detail="payment session provider is not decal")
    session_object = _decal_session_object(decal_session)
    provider_session_id = session_object.get("id")
    if provider_session_id and ps.provider_session_id and provider_session_id != ps.provider_session_id:
        raise HTTPException(status_code=409, detail="Decal provider_session_id mismatch")
    order = session_object.get("order", {}) if isinstance(session_object.get("order"), dict) else {}
    if str(order.get("paymentStatus", "")).lower() != "paid":
        raise HTTPException(status_code=409, detail="Decal session is not paid")
    q = db_session.get(Quote, ps.quote_id)
    if not q:
        raise HTTPException(404, "quote not found")
    paid = _decal_amount_paid_minor(session_object)
    if paid is None or paid < q.amount_minor:
        raise HTTPException(status_code=409, detail="Decal amount paid mismatch")
    currency = order.get("currency")
    if not isinstance(currency, str) or currency.upper() != q.currency.upper():
        raise HTTPException(status_code=409, detail="Decal currency mismatch")


@app.post("/api/v1/webhooks/decal")
async def decal_webhook(request: Request, payment_session_id: str | None = None, session: Session = Depends(get_session), settings: Settings = Depends(get_settings)):
    try:
        event = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail="invalid webhook payload") from exc
    if not isinstance(event, dict):
        raise HTTPException(status_code=400, detail="invalid webhook payload")
    if event.get("event") != "checkout.session.completed":
        return {"received": True, "processed": False, "reason": "event ignored"}
    session_object = event.get("session", {}) if isinstance(event.get("session"), dict) else {}
    provider_session_id = session_object.get("id")
    body_payment_session_id = event.get("payment_session_id") or session_object.get("payment_session_id")
    lookup_id = payment_session_id or body_payment_session_id
    ps = session.get(PaymentSession, lookup_id) if lookup_id else None
    if not ps and provider_session_id:
        ps = session.exec(select(PaymentSession).where(PaymentSession.provider_session_id == provider_session_id)).first()
    if not ps:
        raise HTTPException(404 if (lookup_id or provider_session_id) else 400, "payment session not found" if (lookup_id or provider_session_id) else "missing payment_session_id")
    if ps.status == "paid":
        return {"received": True, "processed": False, "reason": "payment session already paid", "payment_session": session_to_read(ps)}
    if not ps.provider_session_id:
        raise HTTPException(status_code=409, detail="Decal provider_session_id missing")
    verified = retrieve_decal_checkout_session(ps.provider_session_id, settings)
    _validate_decal_paid_session(session, ps, verified)
    _issue_paid_session(session, ps, settings, provider="decal")
    return {"received": True, "processed": True}


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
    _validate_stripe_paid_event(session, ps, data_object, event_type)
    _issue_paid_session(session, ps, settings, provider="stripe")
    return {"received": True, "processed": True}


@app.post("/api/v1/payment-sessions/{session_id}/refund", response_model=PaymentSessionRefundResponse)
def refund_payment_session(
    session_id: str,
    payload: PaymentSessionRefundCreate,
    session: Session = Depends(get_session),
    settings: Settings = Depends(get_settings),
    _credential: BotCredential = Depends(require_operator_credential),
):
    ps = session.get(PaymentSession, session_id)
    if not ps:
        raise HTTPException(404, "payment session not found")
    q = session.get(Quote, ps.quote_id)
    if not q:
        raise HTTPException(404, "quote not found")
    if ps.provider == "decal":
        raise HTTPException(status_code=501, detail="Decal automatic refunds are not implemented yet; feature gap tracked")
    if ps.provider != "stripe":
        raise HTTPException(status_code=409, detail="only Stripe payment sessions can be refunded automatically")
    existing_refund = session.exec(select(FulfillmentEvent).where(FulfillmentEvent.quote_id == q.id, FulfillmentEvent.status == "refunded")).first()
    if ps.status == "refunded" or q.status == "refunded" or existing_refund:
        metadata = existing_refund.metadata_json if existing_refund and isinstance(existing_refund.metadata_json, dict) else {}
        return PaymentSessionRefundResponse(
            payment_session_id=ps.id,
            quote_id=q.id,
            payment_status=ps.status,
            quote_status=q.status,
            refund_id=str(metadata.get("refund_id") or "already_refunded"),
            refund_status="already_refunded",
            amount_minor=q.amount_minor,
            currency=q.currency,
            fulfillment_id=existing_refund.id if existing_refund else q.id,
            message="Payment session was already refunded.",
        )
    if ps.status != "paid":
        raise HTTPException(status_code=409, detail="payment session must be paid before refund")
    if q.status != "failed" and not payload.force:
        raise HTTPException(status_code=409, detail="quote must be failed before refund unless force=true")

    refund_id, refund_status = create_stripe_refund(ps, q, settings, reason=payload.reason)
    ev = FulfillmentEvent(
        id=f"ful_{uuid4().hex}",
        quote_id=q.id,
        status="refunded",
        metadata_json={
            "provider": "stripe",
            "refund_id": refund_id,
            "refund_status": refund_status,
            "payment_session_id": ps.id,
            "reason": payload.reason,
            "forced": payload.force,
        },
    )
    ps.status = "refunded"
    q.status = "refunded"
    session.add(ps)
    session.add(q)
    session.add(ev)
    session.commit()
    session.refresh(ev)
    return PaymentSessionRefundResponse(
        payment_session_id=ps.id,
        quote_id=q.id,
        payment_status=ps.status,
        quote_status=q.status,
        refund_id=refund_id,
        refund_status=refund_status,
        amount_minor=q.amount_minor,
        currency=q.currency,
        fulfillment_id=ev.id,
        message="Stripe refund created and Payjent session marked refunded.",
    )


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
