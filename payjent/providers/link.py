"""Link one-time payment credential helpers.

Payjent treats Link as a bounded downstream credential rail for agent-mediated
merchant purchases. Payjent still owns quote/payment/grant state; a Link spend
request only produces an approval URL and provider id. It is not settlement.

Integration order is MCP first, CLI fallback second. MCP adapter bindings can use
LINK_MCP_TOOLS below. CLI fallback must authenticate/check status before login
(`link-cli auth status --format json`, then interactive `auth login --client-name
Payjent --format json` if needed) and must not run login in tests.

Credential type is intentionally explicit. Payjent does not infer or default to
`card`; callers must evaluate the merchant site and choose the credential type.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from typing import Any, Callable

from pydantic import BaseModel, Field, field_validator

SUPPORTED_CREDENTIAL_TYPES = {"card", "bank_account", "unknown"}

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
    provider_session_id = data.get("spend_request_id") or data.get("spendRequestId") or data.get("id")
    if not provider_session_id:
        raise ValueError("Link spend request response missing spend_request_id/id")
    next_hint = data.get("_next") if isinstance(data.get("_next"), dict) else {}
    return LinkApproval(
        approval_url=str(approval_url),
        provider_session_id=str(provider_session_id),
        polling_command=_coerce_polling_command(next_hint.get("command")),
        raw=data,
    )


Runner = Callable[[list[str]], str | bytes | dict[str, Any]]


def run_link_cli_spend_request(request: LinkCredentialRequest, runner: Runner | None = None) -> LinkApproval:
    """CLI fallback execution hook. Tests should inject runner; no auth/login here."""
    command = build_link_spend_request_command(request)
    if runner is None:
        try:
            completed = subprocess.run(command, check=True, capture_output=True, text=True)
        except FileNotFoundError as exc:
            raise RuntimeError("link-cli is not installed or not on PATH") from exc
        except subprocess.CalledProcessError as exc:
            raise RuntimeError(f"link-cli spend request failed: {exc.stderr or exc.stdout}") from exc
        raw: str | bytes | dict[str, Any] = completed.stdout
    else:
        raw = runner(command)
    return parse_link_spend_request_response(raw)
