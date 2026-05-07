import json

import pytest
from sqlmodel import Session

from payjent.config import Settings, get_settings
from payjent.main import app
from payjent.models import ToolExecution


def _ready_execution(engine, *, status="ready_to_execute", arguments=None):
    execution_id = f"texec-test-elevenlabs-{status}"
    execution = ToolExecution(
        id=execution_id,
        tool_id="elevenlabs.text_to_speech",
        bot_id="bot-1",
        external_user_id="user-1",
        amount_minor=35,
        currency="USD",
        request_hash="hash-test",
        arguments_json=arguments or {"text": "Hello from Payjent", "voice_id": "voice_123", "model_id": "eleven_multilingual_v2"},
        status=status,
    )
    with Session(engine) as session:
        session.add(execution)
        session.commit()
    return execution_id


def _assert_no_secret(value):
    serialized = json.dumps(value).lower()
    assert "test-elevenlabs-secret" not in serialized
    assert "xi-api-key" not in serialized
    assert "authorization" not in serialized
    assert "api_key" not in serialized
    assert "raw-audio" not in serialized


def test_managed_elevenlabs_run_succeeds_with_mocked_provider(client, bot_headers, engine, monkeypatch):
    app.dependency_overrides[get_settings] = lambda: Settings(elevenlabs_api_key="test-elevenlabs-secret")

    def fake_run(arguments, *, api_key):
        assert api_key == "test-elevenlabs-secret"
        assert arguments["text"] == "Hello from Payjent"
        return {
            "provider": "elevenlabs",
            "tool_id": "elevenlabs.text_to_speech",
            "voice_id": "voice_123",
            "model_id": "eleven_multilingual_v2",
            "content_type": "audio/mpeg",
            "audio_size_bytes": 1234,
            "delivery_mode": "metadata_only",
            "result_caveat": "metadata_only_audio_not_stored",
            "provider_request_id": "req_abc",
        }

    monkeypatch.setattr("payjent.main.run_elevenlabs_text_to_speech", fake_run)
    execution_id = _ready_execution(engine)
    response = client.post(f"/api/v1/toolbox/executions/{execution_id}/run", headers=bot_headers)
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "succeeded"
    assert body["result_metadata_json"]["audio_size_bytes"] == 1234
    assert body["result_metadata_json"]["delivery_mode"] == "metadata_only"
    assert body["result_metadata_json"]["result_caveat"] == "metadata_only_audio_not_stored"
    _assert_no_secret(body)


def test_managed_elevenlabs_run_missing_provider_config_fails_closed(client, bot_headers, engine):
    app.dependency_overrides[get_settings] = lambda: Settings(elevenlabs_api_key=None)
    execution_id = _ready_execution(engine)
    response = client.post(f"/api/v1/toolbox/executions/{execution_id}/run", headers=bot_headers)
    assert response.status_code == 503
    with Session(engine) as session:
        execution = session.get(ToolExecution, execution_id)
        assert execution.status == "failed"
        assert execution.error_metadata_json["code"] == "provider_not_configured"
        _assert_no_secret(execution.error_metadata_json)


def test_managed_elevenlabs_run_invalid_args_records_failed_422(client, bot_headers, engine):
    app.dependency_overrides[get_settings] = lambda: Settings(elevenlabs_api_key="test-elevenlabs-secret")
    execution_id = _ready_execution(engine, arguments={"text": "", "voice_id": "voice_123"})
    response = client.post(f"/api/v1/toolbox/executions/{execution_id}/run", headers=bot_headers)
    assert response.status_code == 422
    with Session(engine) as session:
        execution = session.get(ToolExecution, execution_id)
        assert execution.status == "failed"
        assert execution.error_metadata_json["code"] == "invalid_arguments"
        _assert_no_secret(execution.error_metadata_json)


def test_managed_elevenlabs_provider_failure_records_sanitized_failed_state(client, bot_headers, engine, monkeypatch):
    from payjent.providers.elevenlabs import ElevenLabsProviderError

    app.dependency_overrides[get_settings] = lambda: Settings(elevenlabs_api_key="test-elevenlabs-secret")

    def fake_run(arguments, *, api_key):
        raise ElevenLabsProviderError("test-elevenlabs-secret raw provider traceback")

    monkeypatch.setattr("payjent.main.run_elevenlabs_text_to_speech", fake_run)
    execution_id = _ready_execution(engine)
    response = client.post(f"/api/v1/toolbox/executions/{execution_id}/run", headers=bot_headers)
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "failed"
    assert body["error_metadata_json"] == {"code": "provider_execution_failed", "message": "managed provider execution failed"}
    _assert_no_secret(body)


def test_managed_elevenlabs_adapter_happy_path_via_fake_transport():
    from payjent.providers.elevenlabs import run_text_to_speech

    def fake_transport(payload, api_key):
        assert api_key == "test-elevenlabs-secret"
        assert payload == {"text": "Hello", "voice_id": "voice_123", "model_id": "eleven_multilingual_v2"}
        return {"content_type": "audio/mpeg", "audio_size_bytes": 42, "request_id": "request-id-0123456789" * 5, "audio": b"raw-audio"}

    result = run_text_to_speech({"text": " Hello ", "voice": "voice_123"}, api_key="test-elevenlabs-secret", transport=fake_transport)
    assert result == {
        "provider": "elevenlabs",
        "tool_id": "elevenlabs.text_to_speech",
        "voice_id": "voice_123",
        "model_id": "eleven_multilingual_v2",
        "content_type": "audio/mpeg",
        "audio_size_bytes": 42,
        "delivery_mode": "metadata_only",
        "result_caveat": "metadata_only_audio_not_stored",
        "provider_request_id": ("request-id-0123456789" * 5)[:64],
    }
    _assert_no_secret(result)


def test_managed_elevenlabs_adapter_rejects_invalid_input_before_transport():
    from payjent.providers.elevenlabs import run_text_to_speech

    calls = 0

    def fake_transport(payload, api_key):  # pragma: no cover - assertion guard
        nonlocal calls
        calls += 1
        return {"audio_size_bytes": 1}

    invalid_arguments = [
        ({"text": ""}, "text is required"),
        ({"text": "x" * 5001}, "text must be at most 5000 characters"),
        ({"text": "ok", "voice_id": "../bad"}, "voice_id contains unsupported characters"),
        ({"text": "ok", "model_id": "bad/model"}, "model_id contains unsupported characters"),
        ({"text": "ok", "api_key": "***"}, "provider credentials are not accepted"),
        ({"text": "bearer test-elevenlabs-secret"}, "provider credentials are not accepted"),
        ({"text": "ok", "nested": [{"token": "***"}]}, "provider credentials are not accepted"),
    ]
    for arguments, expected in invalid_arguments:
        try:
            run_text_to_speech(arguments, api_key="test-elevenlabs-secret", transport=fake_transport)
        except ValueError as exc:
            assert expected in str(exc)
        else:  # pragma: no cover - assertion guard
            raise AssertionError(f"expected ValueError for {arguments}")
    assert calls == 0


def test_managed_elevenlabs_adapter_sanitizes_transport_failure():
    from payjent.providers.elevenlabs import ElevenLabsProviderError, run_text_to_speech

    def fake_transport(payload, api_key):
        raise RuntimeError("raw body includes test-elevenlabs-secret")

    try:
        run_text_to_speech({"text": "valid"}, api_key="test-elevenlabs-secret", transport=fake_transport)
    except ElevenLabsProviderError as exc:
        assert str(exc) == "provider_execution_failed"
    else:  # pragma: no cover - assertion guard
        raise AssertionError("expected ElevenLabsProviderError")


def test_managed_elevenlabs_adapter_rejects_empty_audio_body():
    from payjent.providers.elevenlabs import ElevenLabsProviderError, run_text_to_speech

    def fake_transport(payload, api_key):
        return {"content_type": "audio/mpeg", "audio_size_bytes": 0}

    with pytest.raises(ElevenLabsProviderError, match="provider_execution_failed"):
        run_text_to_speech({"text": "valid"}, api_key="test-elevenlabs-secret", transport=fake_transport)


def test_managed_elevenlabs_adapter_rejects_non_audio_2xx_body():
    from payjent.providers.elevenlabs import ElevenLabsProviderError, run_text_to_speech

    def fake_transport(payload, api_key):
        return {"content_type": "application/json; charset=utf-8", "audio_size_bytes": 2, "content": b"{}"}

    with pytest.raises(ElevenLabsProviderError, match="provider_execution_failed"):
        run_text_to_speech({"text": "valid"}, api_key="test-elevenlabs-secret", transport=fake_transport)


def test_managed_elevenlabs_adapter_rejects_oversized_fake_transport_response():
    from payjent.providers.elevenlabs import ElevenLabsProviderError, MAX_AUDIO_SIZE_BYTES, run_text_to_speech

    def fake_transport(payload, api_key):
        return {"content_type": "audio/mpeg", "audio_size_bytes": MAX_AUDIO_SIZE_BYTES + 1}

    with pytest.raises(ElevenLabsProviderError, match="provider_execution_failed"):
        run_text_to_speech({"text": "valid"}, api_key="test-elevenlabs-secret", transport=fake_transport)


def test_managed_elevenlabs_default_transport_streams_and_rejects_json_before_reading(monkeypatch):
    from payjent.providers import elevenlabs
    from payjent.providers.elevenlabs import ElevenLabsProviderError, run_text_to_speech

    class FakeResponse:
        headers = {"content-type": "application/json"}

        def raise_for_status(self):
            return None

        def iter_bytes(self):  # pragma: no cover - should reject on content-type first
            raise AssertionError("default transport buffered/read a non-audio response")

    class FakeStream:
        def __enter__(self):
            return FakeResponse()

        def __exit__(self, exc_type, exc, tb):
            return False

    def fake_stream(*args, **kwargs):
        return FakeStream()

    monkeypatch.setattr(elevenlabs.httpx, "stream", fake_stream)
    with pytest.raises(ElevenLabsProviderError, match="provider_execution_failed"):
        run_text_to_speech({"text": "valid"}, api_key="test-elevenlabs-secret")
