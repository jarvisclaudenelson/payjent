from __future__ import annotations

import base64
import hashlib
import json
from typing import Any
from uuid import uuid4

from sqlmodel import Session

from .models import ExecutionArtifact

MAX_ARTIFACT_BYTES = 5 * 1024 * 1024
MAX_TEXT_CHARS = 200_000
SECRET_MARKERS = ("secret", "token", "api_key", "apikey", "authorization", "cookie", "password", "private_key", "credential", "grant")


def scrub_artifact_value(value: Any) -> Any:
    if isinstance(value, dict):
        safe = {}
        for k, v in value.items():
            key = str(k)
            if any(m in key.lower().replace("-", "_") for m in SECRET_MARKERS):
                safe["redacted"] = "redacted"
            else:
                safe[key] = scrub_artifact_value(v)
        return safe
    if isinstance(value, list):
        return [scrub_artifact_value(v) for v in value]
    if isinstance(value, str):
        lowered = value.lower()
        if any(marker in lowered for marker in ("bearer ", "api_key=", "token=", "authorization:")):
            return "redacted"
    return value


def artifact_pointer(artifact: ExecutionArtifact) -> dict[str, Any]:
    return {
        "artifact_id": artifact.artifact_id,
        "kind": artifact.kind,
        "mime_type": artifact.mime_type,
        "size_bytes": artifact.size_bytes,
        "checksum_sha256": artifact.checksum_sha256,
        "url": f"/api/v1/toolbox/executions/{artifact.execution_id}/artifacts/{artifact.artifact_id}",
    }


def create_artifact(
    session: Session,
    *,
    execution_id: str,
    kind: str,
    mime_type: str,
    content_bytes: bytes | None = None,
    text_payload: str | None = None,
    json_payload: Any | None = None,
    metadata: dict[str, Any] | None = None,
) -> ExecutionArtifact:
    if kind not in {"audio", "image", "text", "json", "html", "file"}:
        raise ValueError("invalid artifact kind")
    payload_text = None
    content_base64 = None
    if json_payload is not None:
        safe_json = scrub_artifact_value(json_payload)
        payload_text = json.dumps(safe_json, separators=(",", ":"), sort_keys=True)
        raw = payload_text.encode("utf-8")
    elif text_payload is not None:
        payload_text = str(scrub_artifact_value(text_payload))
        if len(payload_text) > MAX_TEXT_CHARS:
            raise ValueError("artifact text payload too large")
        raw = payload_text.encode("utf-8")
    else:
        raw = content_bytes or b""
        content_base64 = base64.b64encode(raw).decode("ascii")
    if len(raw) > MAX_ARTIFACT_BYTES:
        raise ValueError("artifact payload too large")
    artifact = ExecutionArtifact(
        artifact_id=f"art_{uuid4().hex}",
        execution_id=execution_id,
        kind=kind,
        mime_type=mime_type[:200],
        size_bytes=len(raw),
        storage_backend="db_inline",
        content_base64=content_base64,
        payload_json=safe_json if json_payload is not None else None,
        text_payload=payload_text if json_payload is None else None,
        checksum_sha256=hashlib.sha256(raw).hexdigest(),
        metadata_json=scrub_artifact_value(metadata or {}),
    )
    session.add(artifact)
    session.commit()
    session.refresh(artifact)
    return artifact
