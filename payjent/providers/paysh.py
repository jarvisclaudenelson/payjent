"""pay.sh premium action provider envelope helpers.

This module is intentionally transport-neutral: it normalizes metadata and a
safe command preview for a downstream pay.sh/paycurl runtime, but never shells
out to paycurl, resolves pay.sh gateway endpoints, or handles pay.sh settlement.
"""

from __future__ import annotations

import json
import shlex
from typing import Any

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
) -> dict[str, Any]:
    """Normalize a pay.sh premium API call envelope for a paid agent action."""

    service_url, service_fqn, resource = validate_target(service_url=service_url, service_fqn=service_fqn, resource=resource)
    method = (method or "POST").strip().upper()
    if not method:
        method = "POST"
    normalized_headers = dict(headers or {})
    normalized_body = dict(body or {})
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
    }
    return envelope
