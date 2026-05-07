"""pay.sh premium action provider envelope helpers.

This module is intentionally transport-neutral: it normalizes metadata and a
safe command preview for a downstream pay.sh/paycurl runtime, but never shells
out to paycurl, resolves pay.sh gateway endpoints, or handles pay.sh settlement.
"""

from __future__ import annotations

import json
import shlex
from typing import Any
from urllib.parse import urlparse

PROVIDER = "pay_sh"
KIND = "premium_api_call"
SETUP_HINT = "Install/setup pay.sh CLI at runtime: brew install pay; pay setup; pay skills update; inspect gateway URLs with pay skills endpoints."
SETTLEMENT = "external_pay_sh_runtime"


def _clean_optional(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = value.strip()
    return cleaned or None


def validate_target(*, service_url: str | None = None, service_fqn: str | None = None, resource: str | None = None) -> tuple[str | None, str | None, str | None]:
    """Validate the supported pay.sh target forms.

    Accept either a direct service_url, or service_fqn + resource for gateway
    resolution by the external pay.sh runtime.
    """

    service_url = _clean_optional(service_url)
    service_fqn = _clean_optional(service_fqn)
    resource = _clean_optional(resource)
    if not service_url and not (service_fqn and resource):
        raise ValueError("provide either service_url or both service_fqn and resource")
    if service_url:
        parsed = urlparse(service_url)
        host = (parsed.netloc or "").lower()
        path = (parsed.path or "").strip("/")
        if host in {"fal.ai", "www.fal.ai"} and not path:
            raise ValueError(
                "https://fal.ai is a catalog/site root, not an executable pay.sh/x402 gateway endpoint; "
                "use the public pay.sh catalog target service_fqn='paysponge/fal' with a concrete resource "
                "such as 'fal-ai/flux/schnell', or provide a resolved gateway URL like "
                "https://fal.x402.paysponge.com/<resource> plus the required request body"
            )
    return service_url, service_fqn, resource


def build_command_preview(*, service_url: str | None, service_fqn: str | None, resource: str | None, method: str, body: dict[str, Any] | None = None, headers: dict[str, str] | None = None) -> str:
    """Return a non-executed paycurl preview string with no secret assumptions."""

    target = service_url or f"{service_fqn}:{resource}"
    parts = ["paycurl", "-X", method.upper(), target]
    for name, value in (headers or {}).items():
        parts.extend(["-H", f"{name}: {value}"])
    if body:
        parts.extend(["--json", json.dumps(body, sort_keys=True, separators=(",", ":"))])
    return " ".join(shlex.quote(str(part)) for part in parts)


def build_execution_envelope(
    *,
    service_url: str | None = None,
    service_fqn: str | None = None,
    resource: str | None = None,
    method: str = "POST",
    body: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    description: str | None = None,
    payjent_fulfillment_callback: bool = False,
    payjent_managed_execution: bool = False,
) -> dict[str, Any]:
    """Normalize a pay.sh premium API call envelope for a paid agent action."""

    service_url, service_fqn, resource = validate_target(service_url=service_url, service_fqn=service_fqn, resource=resource)
    method = (method or "POST").strip().upper()
    if not method:
        method = "POST"
    normalized_headers = dict(headers or {})
    normalized_body = dict(body or {})
    # Historical flags requested Payjent-side callbacks/execution.  Payjent's
    # product boundary for pay.sh/x402 is now strictly authorization only:
    # Payjent gates payment and issues spend authorization, while the agent
    # executes the downstream pay.sh/x402 task in its own runtime.
    envelope = {
        "provider": PROVIDER,
        "kind": KIND,
        "service_url": service_url,
        "service_fqn": service_fqn,
        "resource": resource,
        "method": method,
        "body": normalized_body,
        "headers": normalized_headers,
        "description": _clean_optional(description),
        "command_preview": build_command_preview(
            service_url=service_url,
            service_fqn=service_fqn,
            resource=resource,
            method=method,
            body=normalized_body,
            headers=normalized_headers,
        ),
        "setup_hint": SETUP_HINT,
        "settlement": SETTLEMENT,
        "payjent_fulfillment_callback": False,
        "payjent_managed_execution": False,
        "payjent_execution_boundary": "agent_executes_after_spend_authorization",
    }
    return envelope
