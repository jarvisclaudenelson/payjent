"""Link one-time payment credential helpers.

Payjent treats Link as a bounded downstream credential rail for agent-mediated
merchant purchases. Payjent still owns quote/payment/grant state; a Link spend
request only produces an approval URL and provider id. It is not settlement.

Integration order is MCP first, CLI fallback second. The public
``create_link_spend_request`` orchestrator prefers an injected MCP callable/client
when available and otherwise falls back to the CLI helper. CLI fallback performs a
non-interactive auth preflight before creating a spend request and never runs
interactive login inside the API process.

Credential type is intentionally explicit. Payjent does not infer or default to
`card`; callers must evaluate the merchant site and choose the credential type.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from typing import Any, Callable
from urllib.parse import urlparse

from pydantic import BaseModel, Field, field_validator

SUPPORTED_CREDENTIAL_TYPES = {"card", "bank_account"}

LINK_MCP_TOOLS = {
    "auth_status": "auth_status",
    "auth_login": "auth_login",
    "spend_request_create": "spend-request_create",
    "spend_request_retrieve": "spend-request_retrieve",
    "payment_methods_list": "payment-methods_list",
}

LINK_AUTH_STATUS_COMMAND = ["link-cli", "auth", "status", "--format", "json"]
LINK_AUTH_LOGIN_COMMAND = ["link-cli", "auth", "login", "--client-name", "Payjent", "--format", "json"]


class LinkCredentialRequest(BaseModel):
    merchant_url: str = Field(min_length=1)
    credential_type: str = Field(min_length=1)
    amount_minor: int = Field(gt=0)
    currency: str = Field(min_length=3, max_length=3)
    purpose: str = Field(min_length=1)
    external_user_id: str = Field(min_length=1)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("merchant_url")
    @classmethod
    def _validate_merchant_url(cls, value: str) -> str:
        return validate_http_url(value, field_name="merchant_url")

    @field_validator("credential_type")
    @classmethod
    def _validate_credential_type(cls, value: str) -> str:
        return validate_credential_type(value)

    @field_validator("currency")
    @classmethod
    def _normalize_currency(cls, value: str) -> str:
        return value.upper()


@dataclass(frozen=True)
class LinkApproval:
    approval_url: str
    provider_session_id: str
    polling_command: list[str] | None
    raw: dict[str, Any]


@dataclass(frozen=True)
class LinkStatusResult:
    normalized_status: str
    provider_session_id: str | None
    raw_status: str | None
    raw: dict[str, Any]
    is_settled: bool


Runner = Callable[[list[str]], str | bytes | dict[str, Any]]
MCPClient = Callable[[LinkCredentialRequest], str | bytes | dict[str, Any]] | Any

LINK_STATUS_PENDING = "pending"
LINK_STATUS_APPROVED_NOT_SETTLED = "approved_not_settled"
LINK_STATUS_CREDENTIAL_CREATED_NOT_SETTLED = "credential_created_not_settled"
LINK_STATUS_SETTLED = "settled"
LINK_STATUS_FAILED = "failed"
LINK_STATUS_UNKNOWN = "unknown"

_APPROVED_NOT_SETTLED_STATUSES = {
    "approved", "approval_complete", "approval_completed", "authorized", "auth_complete", "authorization_complete",
}
_CREDENTIAL_CREATED_NOT_SETTLED_STATUSES = {
    "credential_created", "credentials_created", "credential_issued", "card_created", "payment_method_created",
}
_PENDING_STATUSES = {"pending", "created", "requested", "requires_approval", "awaiting_approval", "processing", "in_progress"}
_FAILED_STATUSES = {"failed", "failure", "declined", "canceled", "cancelled", "expired", "rejected", "error"}
_SETTLED_STATUSES = {"paid", "settled", "payment_succeeded", "merchant_charge_succeeded"}


def validate_http_url(value: str | None, *, field_name: str) -> str:
    if value is None or not str(value).strip():
        raise ValueError(f"{field_name} is required")
    normalized = str(value).strip()
    parsed = urlparse(normalized)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError(f"{field_name} must be an http or https URL")
    return normalized


def validate_credential_type(value: str | None) -> str:
    """Require a caller-selected credential type; never default to card."""
    if value is None or not str(value).strip():
        raise ValueError("credential_type is required; Payjent does not infer or default to card")
    normalized = str(value).strip()
    if normalized not in SUPPORTED_CREDENTIAL_TYPES:
        raise ValueError(f"unsupported credential_type '{normalized}'; supported values: {', '.join(sorted(SUPPORTED_CREDENTIAL_TYPES))}")
    return normalized


def build_link_spend_request_command(request: LinkCredentialRequest) -> list[str]:
    argv = [
        "link-cli",
        "spend-request",
        "create",
        "--format",
        "json",
        "--merchant-url",
        request.merchant_url,
        "--credential-type",
        validate_credential_type(request.credential_type),
        "--amount-minor",
        str(request.amount_minor),
        "--currency",
        request.currency.upper(),
        "--purpose",
        request.purpose,
        "--external-user-id",
        request.external_user_id,
    ]
    for key, value in sorted(request.metadata.items()):
        argv.extend(["--metadata", f"{key}={json.dumps(value, separators=(',', ':'))}"])
    return argv


def build_link_cli_command_sequence(request: LinkCredentialRequest) -> list[list[str]]:
    return [LINK_AUTH_STATUS_COMMAND.copy(), build_link_spend_request_command(request)]


def build_link_retrieve_command(provider_session_id: str) -> list[str]:
    if not str(provider_session_id).strip():
        raise ValueError("provider_session_id is required for Link retrieve")
    return ["link-cli", "spend-request", "retrieve", str(provider_session_id).strip(), "--format", "json"]


def _coerce_polling_command(value: Any) -> list[str] | None:
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return value
    if isinstance(value, str) and value.strip():
        return value.split()
    return None


def parse_link_spend_request_response(raw: str | bytes | dict[str, Any]) -> LinkApproval:
    if isinstance(raw, (str, bytes)):
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError("Link spend request response was not valid JSON") from exc
    elif isinstance(raw, dict):
        data = raw
    else:
        raise ValueError("Link spend request response must be JSON text or a dict")

    approval_url = data.get("approval_url") or data.get("approvalUrl")
    if not approval_url:
        raise ValueError("Link spend request response missing approval_url; approval must be shown to the user")
    approval_url = validate_http_url(str(approval_url), field_name="approval_url")
    provider_session_id = data.get("spend_request_id") or data.get("spendRequestId") or data.get("id")
    if not provider_session_id:
        raise ValueError("Link spend request response missing spend_request_id/id")
    next_hint = data.get("_next") if isinstance(data.get("_next"), dict) else {}
    return LinkApproval(
        approval_url=approval_url,
        provider_session_id=str(provider_session_id),
        polling_command=_coerce_polling_command(next_hint.get("command")),
        raw=data,
    )


def _parse_json_dict(raw: str | bytes | dict[str, Any], *, context: str) -> dict[str, Any]:
    if isinstance(raw, (str, bytes)):
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{context} response was not valid JSON") from exc
    elif isinstance(raw, dict):
        data = raw
    else:
        raise ValueError(f"{context} response must be JSON text or a dict")
    if not isinstance(data, dict):
        raise ValueError(f"{context} response must be a JSON object")
    return data


def _normalize_status_value(value: Any) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip().lower().replace("-", "_").replace(" ", "_")
    return normalized or None


def _extract_raw_status(data: dict[str, Any]) -> str | None:
    candidates: list[Any] = [data.get("status"), data.get("state")]
    for key in ("spend_request", "spendRequest", "payment", "charge", "merchant_charge", "merchantCharge", "credential"):
        nested = data.get(key)
        if isinstance(nested, dict):
            candidates.extend([nested.get("status"), nested.get("state")])
    for value in candidates:
        normalized = _normalize_status_value(value)
        if normalized:
            return normalized
    return None


def _normalize_link_status(raw_status: str | None) -> str:
    if raw_status in _SETTLED_STATUSES:
        return LINK_STATUS_SETTLED
    if raw_status in _FAILED_STATUSES:
        return LINK_STATUS_FAILED
    if raw_status in _CREDENTIAL_CREATED_NOT_SETTLED_STATUSES:
        return LINK_STATUS_CREDENTIAL_CREATED_NOT_SETTLED
    if raw_status in _APPROVED_NOT_SETTLED_STATUSES:
        return LINK_STATUS_APPROVED_NOT_SETTLED
    if raw_status in _PENDING_STATUSES:
        return LINK_STATUS_PENDING
    return LINK_STATUS_UNKNOWN


def parse_link_status_response(raw: str | bytes | dict[str, Any], *, provider_session_id: str | None = None) -> LinkStatusResult:
    data = _parse_json_dict(raw, context="Link retrieve")
    raw_status = _extract_raw_status(data)
    normalized_status = _normalize_link_status(raw_status)
    resolved_provider_session_id = provider_session_id or data.get("spend_request_id") or data.get("spendRequestId") or data.get("id")
    return LinkStatusResult(
        normalized_status=normalized_status,
        provider_session_id=str(resolved_provider_session_id) if resolved_provider_session_id else None,
        raw_status=raw_status,
        raw=data,
        is_settled=normalized_status == LINK_STATUS_SETTLED,
    )


def _default_cli_runner(command: list[str]) -> str:
    try:
        completed = subprocess.run(command, check=True, capture_output=True, text=True)
    except FileNotFoundError as exc:
        raise RuntimeError("link-cli is not installed or not on PATH") from exc
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(f"link-cli command failed: {exc.stderr or exc.stdout}") from exc
    return completed.stdout


def _is_authenticated(raw: str | bytes | dict[str, Any]) -> bool:
    if isinstance(raw, (str, bytes)):
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise RuntimeError("link-cli auth status returned invalid JSON") from exc
    elif isinstance(raw, dict):
        data = raw
    else:
        raise RuntimeError("link-cli auth status returned an unsupported response")
    return bool(data.get("authenticated") or data.get("is_authenticated") or data.get("logged_in"))


def run_link_cli_spend_request(request: LinkCredentialRequest, runner: Runner | None = None) -> LinkApproval:
    """Run CLI fallback with non-interactive auth status preflight."""
    active_runner = runner or _default_cli_runner
    auth_raw = active_runner(LINK_AUTH_STATUS_COMMAND.copy())
    if not _is_authenticated(auth_raw):
        raise RuntimeError(
            "link-cli is not authenticated; run `link-cli auth login --client-name Payjent` in a background terminal and approve it before calling this API"
        )
    raw = active_runner(build_link_spend_request_command(request))
    return parse_link_spend_request_response(raw)


def run_link_cli_retrieve(provider_session_id: str, runner: Runner | None = None) -> LinkStatusResult:
    """Run CLI retrieve fallback with non-interactive auth status preflight."""
    active_runner = runner or _default_cli_runner
    auth_raw = active_runner(LINK_AUTH_STATUS_COMMAND.copy())
    if not _is_authenticated(auth_raw):
        raise RuntimeError(
            "link-cli is not authenticated; run `link-cli auth login --client-name Payjent` in a background terminal and approve it before calling this API"
        )
    raw = active_runner(build_link_retrieve_command(provider_session_id))
    return parse_link_status_response(raw, provider_session_id=provider_session_id)


def _call_mcp_client(mcp_client: MCPClient, request: LinkCredentialRequest) -> str | bytes | dict[str, Any]:
    payload = request.model_dump()
    if callable(mcp_client):
        return mcp_client(request)
    if hasattr(mcp_client, "create_link_spend_request"):
        return mcp_client.create_link_spend_request(payload)
    if hasattr(mcp_client, "call_tool"):
        return mcp_client.call_tool(LINK_MCP_TOOLS["spend_request_create"], payload)
    raise TypeError("mcp_client must be callable or expose create_link_spend_request/call_tool")


def _call_mcp_retrieve(mcp_client: Any, provider_session_id: str) -> str | bytes | dict[str, Any]:
    payload = {"spend_request_id": provider_session_id, "id": provider_session_id}
    if callable(mcp_client):
        return mcp_client(provider_session_id)
    if hasattr(mcp_client, "retrieve_link_spend_request"):
        return mcp_client.retrieve_link_spend_request(payload)
    if hasattr(mcp_client, "call_tool"):
        return mcp_client.call_tool(LINK_MCP_TOOLS["spend_request_retrieve"], payload)
    raise TypeError("mcp_client must be callable or expose retrieve_link_spend_request/call_tool")


def create_link_spend_request(
    payload: LinkCredentialRequest,
    mcp_client: MCPClient | None = None,
    cli_runner: Runner | None = None,
) -> LinkApproval:
    """Create a Link spend request using MCP first, then CLI fallback.

    Tests can inject ``mcp_client`` and/or ``cli_runner`` so no real external calls
    are required. If an MCP client is provided it is always preferred and CLI is
    not invoked.
    """
    if mcp_client is not None:
        return parse_link_spend_request_response(_call_mcp_client(mcp_client, payload))
    return run_link_cli_spend_request(payload, runner=cli_runner)


def retrieve_link_status(
    provider_session_id: str,
    mcp_client: Any | None = None,
    cli_runner: Runner | None = None,
) -> LinkStatusResult:
    """Retrieve a Link spend request using MCP first, then CLI fallback."""
    if mcp_client is not None:
        return parse_link_status_response(_call_mcp_retrieve(mcp_client, provider_session_id), provider_session_id=provider_session_id)
    return run_link_cli_retrieve(provider_session_id, runner=cli_runner)
