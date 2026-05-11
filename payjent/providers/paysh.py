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
SETUP_HINT = "Use a funded downstream x402/pay.sh runtime after Payjent authorization. For Sponge/PaySponge gateways, configure SPONGE_API_KEY in the agent runtime and execute with SpongeWallet.paidFetch/x402Fetch or `npx spongewallet pay fetch`; plain paycurl may return the expected HTTP 402 challenge without settling it."
SETTLEMENT = "external_x402_runtime"
FAL_MPP_TEMPO_BASE_URL = "https://fal.mpp.tempo.xyz"
FAL_LEGACY_SERVICE_FQN = "paysponge/fal"


def _clean_optional(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = value.strip()
    return cleaned or None


def _fal_mpp_tempo_url(resource: str) -> str:
    return f"{FAL_MPP_TEMPO_BASE_URL}/{resource.strip('/')}"


def validate_target(*, service_url: str | None = None, service_fqn: str | None = None, resource: str | None = None) -> tuple[str | None, str | None, str | None]:
    """Validate and normalize the supported pay.sh target forms."""

    service_url = _clean_optional(service_url)
    service_fqn = _clean_optional(service_fqn)
    resource = _clean_optional(resource)
    if not service_url and not (service_fqn and resource):
        raise ValueError("provide either service_url or both service_fqn and resource")
    if service_fqn and service_fqn.lower() == FAL_LEGACY_SERVICE_FQN:
        if not resource:
            raise ValueError(
                "paysponge/fal is an obsolete SpongeWallet catalog id and cannot be used without a resource; "
                "discover the current fal.ai service with `npx spongewallet pay discover fal` and use "
                "service_url='https://fal.mpp.tempo.xyz/<resource>' (for example "
                "https://fal.mpp.tempo.xyz/fal-ai/fast-sdxl)"
            )
        current_url = _fal_mpp_tempo_url(resource)
        if service_url and service_url != current_url:
            raise ValueError(
                "paysponge/fal is obsolete and must not be emitted as a downstream x402 target; use the "
                f"current fal.ai MPP executable URL {current_url} or discover it with "
                "`npx spongewallet pay discover fal`"
            )
        # Remediate the legacy catalog target to the current executable MPP URL
        # so downstream agents never receive paysponge/fal:<resource>.
        service_url = current_url
    if service_url:
        parsed = urlparse(service_url)
        host = (parsed.netloc or "").lower()
        path = (parsed.path or "").strip("/")
        if host in {"fal.ai", "www.fal.ai"} and not path:
            raise ValueError(
                "https://fal.ai is a catalog/site root, not an executable pay.sh/x402 gateway endpoint; "
                "use the current SpongeWallet fal.ai MPP executable URL like "
                "https://fal.mpp.tempo.xyz/fal-ai/fast-sdxl plus the required request body"
            )
    return service_url, service_fqn, resource


def _is_paysponge_gateway(service_url: str | None, service_fqn: str | None) -> bool:
    if service_fqn and service_fqn.strip().lower().startswith("paysponge/"):
        return True
    if not service_url:
        return False
    host = (urlparse(service_url).netloc or "").lower()
    return host.endswith(".paysponge.com") or host == "paysponge.com" or host == "fal.mpp.tempo.xyz"


def build_command_preview(*, service_url: str | None, service_fqn: str | None, resource: str | None, method: str, body: dict[str, Any] | None = None, headers: dict[str, str] | None = None) -> str:
    """Return a non-executed x402 runtime preview string with no secret assumptions."""

    target = service_url or f"{service_fqn}:{resource}"
    if _is_paysponge_gateway(service_url, service_fqn):
        parts = ["npx", "spongewallet", "pay", "fetch", "--url", target, "--method", method.upper()]
        if body:
            parts.extend(["--body", json.dumps(body, sort_keys=True, separators=(",", ":"))])
        return " ".join(shlex.quote(str(part)) for part in parts)

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
        "x402_runtime": "sponge" if _is_paysponge_gateway(service_url, service_fqn) else "pay_sh",
        "agent_runtime_requirements": (
            {
                "tool": "@paysponge/sdk or spongewallet CLI",
                "credential": "SPONGE_API_KEY in the agent runtime only",
                "execution_note": "Payjent's Stripe checkpoint does not satisfy the downstream x402 HTTP 402 challenge; the agent must settle the gateway payment with its funded Sponge wallet.",
            }
            if _is_paysponge_gateway(service_url, service_fqn)
            else {
                "tool": "pay.sh/paycurl or compatible x402 runtime",
                "credential": "configured funded wallet in the agent runtime only",
                "execution_note": "Payjent authorizes the action budget; the agent runtime settles the downstream x402 payment externally.",
            }
        ),
        "payjent_fulfillment_callback": False,
        "payjent_managed_execution": False,
        "payjent_execution_boundary": "agent_executes_after_spend_authorization",
    }
    return envelope
