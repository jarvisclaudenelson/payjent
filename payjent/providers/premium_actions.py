from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass
from typing import Any, Callable
from urllib.parse import urlparse

from fastapi import HTTPException


EXECUTION_BOUNDARY = "agent_executes_after_payjent_authorization"
SECRET_POLICY = {
    "payjent_stores_provider_secrets": False,
    "provider_api_keys": "agent_side_secret_only",
    "forbidden_envelope_headers": ["authorization", "cookie", "x-api-key", "api-key"],
}


@dataclass(frozen=True)
class PremiumActionPreset:
    id: str
    name: str
    provider: str
    task_type: str
    quote_basis: str
    endpoint: str
    method: str
    required_input_fields: list[str]
    optional_input_fields: list[str]
    output_instructions: str
    failure_instructions: str
    refund_instructions: str
    auth_instructions: dict[str, Any]
    builder: Callable[[dict[str, Any]], dict[str, Any]]

    def catalog(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "provider": self.provider,
            "task_type": self.task_type,
            "quote_basis": self.quote_basis,
            "endpoint": self.endpoint,
            "method": self.method,
            "required_input_fields": self.required_input_fields,
            "optional_input_fields": self.optional_input_fields,
            "output_instructions": self.output_instructions,
            "failure_instructions": self.failure_instructions,
            "refund_instructions": self.refund_instructions,
            "secret_policy": SECRET_POLICY,
            "auth_instructions": self.auth_instructions,
            "execution_boundary": EXECUTION_BOUNDARY,
        }


def _require(inputs: dict[str, Any], name: str) -> Any:
    value = inputs.get(name)
    if value in (None, ""):
        raise HTTPException(status_code=422, detail=f"provider input '{name}' is required")
    return value


def _reject(fields: dict[str, Any], names: set[str], message: str) -> None:
    for name in names:
        if name in fields:
            raise HTTPException(status_code=422, detail=message)


def _public_https(url: str) -> None:
    parsed = urlparse(url or "")
    if parsed.scheme.lower() != "https" or not parsed.hostname:
        raise HTTPException(status_code=422, detail="Firecrawl target url must be public HTTPS")
    host = parsed.hostname.lower()
    blocked_hosts = {"localhost", "localhost.localdomain", "metadata.google.internal"}
    if host in blocked_hosts or host.endswith((".localhost", ".local", ".internal")):
        raise HTTPException(status_code=422, detail="Firecrawl target url must be public HTTPS")
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return
    if (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    ):
        raise HTTPException(status_code=422, detail="Firecrawl target url must be public HTTPS")


_ELEVENLABS_VOICE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")


def _safe_elevenlabs_voice_id(voice_id: Any) -> str:
    value = str(voice_id)
    if not _ELEVENLABS_VOICE_ID_RE.fullmatch(value):
        raise HTTPException(status_code=422, detail="ElevenLabs voice_id must contain only letters, numbers, underscores, or hyphens")
    return value


def _envelope(preset: PremiumActionPreset, body: dict[str, Any], description: str) -> dict[str, Any]:
    return {
        "provider": preset.provider,
        "kind": preset.task_type,
        "target_url": preset.endpoint,
        "service_url": preset.endpoint,
        "method": preset.method,
        "body": body,
        "headers": {},
        "auth_instructions": preset.auth_instructions,
        "description": description,
        "command_preview": f"{preset.method} {preset.endpoint} ({EXECUTION_BOUNDARY})",
        "setup_hint": "Agent supplies provider API key at execution time; Payjent stores no provider secrets and does not execute the provider call.",
        "settlement": "provider_external_runtime",
        "provider_metadata": {"preset_id": preset.id, "quote_basis": preset.quote_basis},
        "payjent_fulfillment_callback": False,
        "payjent_managed_execution": False,
        "payjent_execution_boundary": EXECUTION_BOUNDARY,
        "boundary": EXECUTION_BOUNDARY,
    }


def build_exa(inputs: dict[str, Any], preset: PremiumActionPreset) -> dict[str, Any]:
    query = _require(inputs, "query")
    body = {"query": query}
    for k in ("num_results", "contents", "type"):
        if k in inputs:
            body[k] = inputs[k]
    return _envelope(preset, body, f"Exa deep search for: {query}")


def build_firecrawl(inputs: dict[str, Any], preset: PremiumActionPreset) -> dict[str, Any]:
    url = _require(inputs, "url")
    _public_https(url)
    body = {"url": url}
    if "formats" in inputs:
        body["formats"] = inputs["formats"]
    if "only_main_content" in inputs:
        body["onlyMainContent"] = inputs["only_main_content"]
    return _envelope(preset, body, f"Firecrawl scrape: {url}")


def build_elevenlabs(inputs: dict[str, Any], preset: PremiumActionPreset) -> dict[str, Any]:
    _reject(inputs, {"voice_clone", "voice_cloning", "clone_voice", "samples", "voice_samples"}, "ElevenLabs voice cloning fields are not allowed for this preset")
    text = _require(inputs, "text")
    voice_id = _safe_elevenlabs_voice_id(_require(inputs, "voice_id"))
    body = {"text": text}
    for k in ("model_id", "voice_settings"):
        if k in inputs:
            body[k] = inputs[k]
    env = _envelope(preset, body, f"ElevenLabs text-to-speech for voice {voice_id}")
    env["target_url"] = env["service_url"] = preset.endpoint.format(voice_id=voice_id)
    env["command_preview"] = f"POST {env['service_url']} ({EXECUTION_BOUNDARY})"
    return env


PRESETS: dict[str, PremiumActionPreset] = {}

def _add(p: PremiumActionPreset) -> None:
    PRESETS[p.id] = p

_add(PremiumActionPreset("exa.deep_search", "Exa Deep Search", "exa", "deep_search", "exact Exa quoted search cost", "https://api.exa.ai/search", "POST", ["query"], ["num_results", "contents", "type"], "Return Exa search results from the agent-side provider response.", "On provider error, call /api/v1/agent-actions/{action_id}/fail with safe error metadata.", "Set refund=true on fail when paid provider execution cannot be fulfilled.", {"header_template": {"x-api-key": "${EXA_API_KEY}"}, "secret_location": "agent_runtime_only"}, lambda inputs: build_exa(inputs, PRESETS["exa.deep_search"])))
_add(PremiumActionPreset("firecrawl.scrape", "Firecrawl Scrape", "firecrawl", "scrape", "exact Firecrawl quoted scrape cost", "https://api.firecrawl.dev/v2/scrape", "POST", ["url"], ["formats", "only_main_content"], "Return scraped content/formats from the agent-side provider response.", "On scrape failure, record failed fulfillment and optionally request refund.", "Set refund=true on fail if the paid scrape cannot be delivered.", {"header_template": {"Authorization": "Bearer ${FIRECRAWL_API_KEY}"}, "secret_location": "agent_runtime_only"}, lambda inputs: build_firecrawl(inputs, PRESETS["firecrawl.scrape"])))
_add(PremiumActionPreset("elevenlabs.text_to_speech", "ElevenLabs Text to Speech", "elevenlabs", "text_to_speech", "exact ElevenLabs quoted synthesis cost", "https://api.elevenlabs.io/v1/text-to-speech/{voice_id}", "POST", ["text", "voice_id"], ["model_id", "voice_settings"], "Return generated audio/reference from the agent-side provider response.", "On synthesis failure, record failed fulfillment and optionally request refund. Voice cloning is out of scope.", "Set refund=true on fail if TTS cannot be delivered.", {"header_template": {"xi-api-key": "${ELEVENLABS_API_KEY}"}, "secret_location": "agent_runtime_only"}, lambda inputs: build_elevenlabs(inputs, PRESETS["elevenlabs.text_to_speech"])))


def list_presets() -> list[dict[str, Any]]:
    return [PRESETS[k].catalog() for k in sorted(PRESETS)]


def get_preset(preset_id: str) -> PremiumActionPreset:
    try:
        return PRESETS[preset_id]
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="premium action preset not found") from exc
