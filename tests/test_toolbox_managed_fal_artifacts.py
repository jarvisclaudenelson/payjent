import base64
import json

from sqlmodel import Session

from payjent.artifacts import MAX_ARTIFACT_BYTES, create_artifact
from payjent.config import Settings, get_settings
from payjent.main import app
from payjent.models import PaymentSession, Quote, ResumeEvent, ToolExecution


def _ready_execution(engine, *, status="ready_to_execute", tool_id="fal.image.generate", quote_id=None, payment_session_id=None):
    execution_id = f"texec-test-fal-{status}-{quote_id or 'none'}"
    execution = ToolExecution(
        id=execution_id,
        tool_id=tool_id,
        bot_id="bot-1",
        external_user_id="user-1",
        quote_id=quote_id,
        payment_session_id=payment_session_id,
        amount_minor=50,
        currency="USD",
        request_hash="hash-test",
        arguments_json={"prompt": "A safe test image", "count": 1},
        status=status,
    )
    with Session(engine) as session:
        session.add(execution)
        session.commit()
    return execution_id


def _assert_no_secret(value):
    serialized = json.dumps(value).lower()
    assert "test-fal-secret" not in serialized
    assert "authorization" not in serialized
    assert "api_key" not in serialized
    assert "token" not in serialized


def test_artifact_endpoints_require_auth_and_scrub_secrets(client, bot_headers, engine):
    execution_id = _ready_execution(engine)
    with Session(engine) as session:
        artifact = create_artifact(session, execution_id=execution_id, kind="json", mime_type="application/json", json_payload={"ok": True, "api_key": "test-fal-secret"}, metadata={"token": "test-fal-secret"})
        artifact_id = artifact.artifact_id
    assert client.get(f"/api/v1/toolbox/executions/{execution_id}/artifacts").status_code in {401, 403}
    listed = client.get(f"/api/v1/toolbox/executions/{execution_id}/artifacts", headers=bot_headers)
    assert listed.status_code == 200
    assert listed.json()["artifacts"][0]["artifact_id"] == artifact_id
    got = client.get(f"/api/v1/toolbox/executions/{execution_id}/artifacts/{artifact_id}", headers=bot_headers)
    assert got.status_code == 200
    body = got.json()
    assert body["metadata_json"]["redacted"] == "redacted"
    assert body["payload_json"]["redacted"] == "redacted"
    _assert_no_secret(body)


def test_artifact_helper_enforces_size_bounds(engine):
    execution_id = _ready_execution(engine)
    with Session(engine) as session:
        try:
            create_artifact(session, execution_id=execution_id, kind="file", mime_type="application/octet-stream", content_bytes=b"x" * (MAX_ARTIFACT_BYTES + 1))
        except ValueError as exc:
            assert "too large" in str(exc)
        else:
            raise AssertionError("expected ValueError")


def test_managed_fal_missing_provider_config_fails_closed(client, bot_headers, engine):
    app.dependency_overrides[get_settings] = lambda: Settings(fal_api_key=None)
    execution_id = _ready_execution(engine)
    response = client.post(f"/api/v1/toolbox/executions/{execution_id}/run", headers=bot_headers)
    assert response.status_code == 503
    with Session(engine) as session:
        execution = session.get(ToolExecution, execution_id)
        assert execution.status == "failed"
        assert execution.error_metadata_json["code"] == "provider_not_configured"


def test_managed_fal_success_creates_artifact_and_prevents_duplicate(client, bot_headers, engine, monkeypatch):
    app.dependency_overrides[get_settings] = lambda: Settings(fal_api_key="test-fal-secret")
    png = base64.b64decode("iVBORw0KGgo=")

    def fake_run(arguments, *, api_key):
        assert api_key == "test-fal-secret"
        return {"provider": "fal", "tool_id": "fal.image.generate", "image_count": 1, "images": [{"mime_type": "image/png", "content_bytes": png, "url": "https://example.com/image.png?token=test-fal-secret"}]}

    monkeypatch.setattr("payjent.main.run_fal_image_generate", fake_run)
    execution_id = _ready_execution(engine)
    response = client.post(f"/api/v1/toolbox/executions/{execution_id}/run", headers=bot_headers)
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "succeeded"
    assert body["result_metadata_json"]["artifacts"][0]["kind"] == "image"
    _assert_no_secret(body)
    artifacts = client.get(f"/api/v1/toolbox/executions/{execution_id}/artifacts", headers=bot_headers).json()["artifacts"]
    assert len(artifacts) == 1
    duplicate = client.post(f"/api/v1/toolbox/executions/{execution_id}/run", headers=bot_headers)
    assert duplicate.status_code == 409


def test_managed_fal_success_enqueues_resume_event_with_artifact_idempotently(client, bot_headers, engine, monkeypatch):
    app.dependency_overrides[get_settings] = lambda: Settings(fal_api_key="test-fal-secret")
    with Session(engine) as session:
        q = Quote(id="quote-fal", bot_id="bot-1", external_user_id="user-1", request_summary="generate", request_hash="hash-test", amount_minor=50, currency="USD", cost_breakdown=[], execution_envelope={}, quote_hash="qh", status="paid")
        ps = PaymentSession(id="ps-fal", quote_id=q.id, provider="mock", status="paid")
        session.add(q); session.add(ps); session.commit()
    monkeypatch.setattr("payjent.main.run_fal_image_generate", lambda arguments, *, api_key: {"provider": "fal", "tool_id": "fal.image.generate", "image_count": 1, "images": [{"mime_type": "image/png", "content_bytes": b"img"}]})
    execution_id = _ready_execution(engine, quote_id="quote-fal", payment_session_id="ps-fal")
    assert client.post(f"/api/v1/toolbox/executions/{execution_id}/run", headers=bot_headers).status_code == 200
    events = client.get("/api/v1/agents/bot-1/resume-events", headers=bot_headers).json()["events"]
    managed = [e for e in events if e["payment_session_id"] == "ps-fal"][0]["payload"]["managed_execution"]
    assert managed["execution_id"] == execution_id
    assert managed["artifacts"][0]["artifact_id"].startswith("art_")
    _assert_no_secret(managed)
    with Session(engine) as session:
        assert len(session.exec(__import__("sqlmodel").select(ResumeEvent).where(ResumeEvent.payment_session_id == "ps-fal")).all()) == 1
