import json

from sqlmodel import Session

from payjent.auth import create_bot_credential
from payjent.config import Settings, get_settings
from payjent.main import app
from payjent.models import ToolExecution


def _ready_execution(engine, *, tool_id="exa.deep_search", status="ready_to_execute", arguments=None):
    execution_id = f"texec-test-{tool_id.replace('.', '-')}-{status}"
    execution = ToolExecution(
        id=execution_id,
        tool_id=tool_id,
        bot_id="bot-1",
        external_user_id="user-1",
        amount_minor=35,
        currency="USD",
        request_hash="hash-test",
        arguments_json=arguments or {"query": "agent payments", "num_results": 2},
        status=status,
    )
    with Session(engine) as session:
        session.add(execution)
        session.commit()
    return execution_id


def _assert_no_secret_or_executable_marker(value):
    serialized = json.dumps(value).lower()
    assert "test-exa-secret" not in serialized
    assert "authorization" not in serialized
    assert "api_key" not in serialized
    assert "headers" not in serialized
    assert "curl " not in serialized


def test_managed_exa_run_rejects_unpaid(client, bot_headers, engine):
    execution_id = _ready_execution(engine, status="payment_required")
    response = client.post(f"/api/v1/toolbox/executions/{execution_id}/run", headers=bot_headers)
    assert response.status_code == 409


def test_managed_exa_run_rejects_executing_without_provider_call(client, bot_headers, engine, monkeypatch):
    app.dependency_overrides[get_settings] = lambda: Settings(exa_api_key="test-exa-secret")

    def fail_if_called(arguments, *, api_key):  # pragma: no cover - assertion guard
        raise AssertionError("provider must not be called for executing replay")

    monkeypatch.setattr("payjent.main.run_exa_deep_search", fail_if_called)
    execution_id = _ready_execution(engine, status="executing")
    response = client.post(f"/api/v1/toolbox/executions/{execution_id}/run", headers=bot_headers)
    assert response.status_code == 409
    assert response.json()["detail"] == "tool execution is already executing"


def test_managed_exa_run_requires_auth(client, engine):
    execution_id = _ready_execution(engine)
    response = client.post(f"/api/v1/toolbox/executions/{execution_id}/run")
    assert response.status_code in {401, 403}


def test_managed_exa_run_rejects_wrong_bot(client, engine):
    with Session(engine) as session:
        create_bot_credential(session, "bot-2", "test-bot-2-key", get_settings().signing_secret)
    execution_id = _ready_execution(engine)
    response = client.post(
        f"/api/v1/toolbox/executions/{execution_id}/run",
        headers={"Authorization": "Bearer test-bot-2-key"},
    )
    assert response.status_code == 403


def test_managed_exa_run_succeeds_with_mocked_provider(client, bot_headers, engine, monkeypatch):
    app.dependency_overrides[get_settings] = lambda: Settings(exa_api_key="test-exa-secret")

    def fake_run(arguments, *, api_key):
        assert api_key == "test-exa-secret"
        assert arguments == {"query": "agent payments", "num_results": 2}
        return {
            "provider": "exa",
            "tool_id": "exa.deep_search",
            "result_count": 1,
            "results": [{"title": "Safe", "url": "https://example.com/research", "text": "summary", "score": 0.9}],
        }

    monkeypatch.setattr("payjent.main.run_exa_deep_search", fake_run)
    execution_id = _ready_execution(engine)
    response = client.post(f"/api/v1/toolbox/executions/{execution_id}/run", headers=bot_headers)
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "succeeded"
    assert body["result_metadata_json"]["results"][0]["url"] == "https://example.com/research"
    _assert_no_secret_or_executable_marker(body)


def test_managed_exa_run_missing_provider_config_fails_closed(client, bot_headers, engine):
    app.dependency_overrides[get_settings] = lambda: Settings(exa_api_key=None)
    execution_id = _ready_execution(engine)
    response = client.post(f"/api/v1/toolbox/executions/{execution_id}/run", headers=bot_headers)
    assert response.status_code == 503
    with Session(engine) as session:
        execution = session.get(ToolExecution, execution_id)
        assert execution.status == "failed"
        assert execution.error_metadata_json["code"] == "provider_not_configured"
        _assert_no_secret_or_executable_marker(execution.error_metadata_json)


def test_managed_exa_provider_failure_records_sanitized_failed_state(client, bot_headers, engine, monkeypatch):
    from payjent.providers.exa import ExaProviderError

    app.dependency_overrides[get_settings] = lambda: Settings(exa_api_key="test-exa-secret")

    def fake_run(arguments, *, api_key):
        raise ExaProviderError("test-exa-secret raw provider traceback")

    monkeypatch.setattr("payjent.main.run_exa_deep_search", fake_run)
    execution_id = _ready_execution(engine)
    response = client.post(f"/api/v1/toolbox/executions/{execution_id}/run", headers=bot_headers)
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "failed"
    assert body["error_metadata_json"] == {"code": "provider_execution_failed", "message": "managed provider execution failed"}
    _assert_no_secret_or_executable_marker(body)


def test_managed_exa_run_rejects_non_exa_tool(client, bot_headers, engine):
    execution_id = _ready_execution(engine, tool_id="fal.image.generate", arguments={"prompt": "robot"})
    response = client.post(f"/api/v1/toolbox/executions/{execution_id}/run", headers=bot_headers)
    assert response.status_code == 501


def test_managed_exa_adapter_validates_and_sanitizes_response():
    from payjent.providers.exa import run_deep_search

    def fake_transport(payload, api_key):
        assert payload == {"query": "bounded", "num_results": 1}
        return {"results": [{"title": "T", "url": "https://example.com", "text": "x" * 2000, "headers": {"Authorization": "secret"}}]}

    result = run_deep_search({"query": " bounded ", "num_results": 1}, api_key="test-exa-secret", transport=fake_transport)
    assert result["result_count"] == 1
    assert len(result["results"][0]["text"]) == 1000
    _assert_no_secret_or_executable_marker(result)


def test_managed_exa_adapter_rejects_invalid_input_before_transport():
    from payjent.providers.exa import run_deep_search

    calls = 0

    def fake_transport(payload, api_key):  # pragma: no cover - assertion guard
        nonlocal calls
        calls += 1
        return {"results": []}

    invalid_arguments = [
        ({"query": ""}, "query is required"),
        ({"query": "x" * 501}, "query must be at most 500 characters"),
        ({"query": "ok", "num_results": 0}, "num_results must be between 1 and 10"),
        ({"query": "ok", "num_results": 11}, "num_results must be between 1 and 10"),
        ({"query": "ok", "num_results": "many"}, "num_results must be an integer"),
        ({"query": "ok", "api_key": "test-exa-secret"}, "provider credentials are not accepted"),
        ({"query": "ok", "filters": {"headers": {"Authorization": "Bearer test-exa-secret"}}}, "provider credentials are not accepted"),
        ({"query": "ok", "items": [{"token": "test-exa-secret"}]}, "provider credentials are not accepted"),
    ]
    for arguments, expected in invalid_arguments:
        try:
            run_deep_search(arguments, api_key="test-exa-secret", transport=fake_transport)
        except ValueError as exc:
            assert expected in str(exc)
        else:  # pragma: no cover - assertion guard
            raise AssertionError(f"expected ValueError for {arguments}")
    assert calls == 0


def test_managed_exa_adapter_sanitizes_transport_value_error():
    from payjent.providers.exa import ExaProviderError, run_deep_search

    def fake_transport(payload, api_key):
        raise ValueError("raw provider body includes test-exa-secret")

    try:
        run_deep_search({"query": "valid", "num_results": 1}, api_key="test-exa-secret", transport=fake_transport)
    except ExaProviderError as exc:
        assert str(exc) == "provider_execution_failed"
    else:  # pragma: no cover - assertion guard
        raise AssertionError("expected ExaProviderError")
