import json

from sqlmodel import Session

from payjent.auth import create_bot_credential
from payjent.config import Settings, get_settings
from payjent.main import app
from payjent.models import ToolExecution


def _ready_execution(engine, *, tool_id="firecrawl.scrape", status="ready_to_execute", arguments=None):
    execution_id = f"texec-test-{tool_id.replace('.', '-')}-{status}"
    execution = ToolExecution(
        id=execution_id,
        tool_id=tool_id,
        bot_id="bot-1",
        external_user_id="user-1",
        amount_minor=25,
        currency="USD",
        request_hash="hash-test",
        arguments_json=arguments or {"url": {"scheme": "https", "host": "example.com", "canonical_url": "https://example.com/page?x=1"}, "formats": ["markdown", "links"], "only_main_content": True},
        status=status,
    )
    with Session(engine) as session:
        session.add(execution)
        session.commit()
    return execution_id


def _assert_no_secret_or_executable_marker(value):
    serialized = json.dumps(value).lower()
    assert "test-firecrawl-secret" not in serialized
    assert "authorization" not in serialized
    assert "api_key" not in serialized
    assert "headers" not in serialized
    assert "cookie" not in serialized
    assert "https://example.com/page" not in serialized


def test_managed_firecrawl_run_rejects_unpaid(client, bot_headers, engine):
    execution_id = _ready_execution(engine, status="payment_required")
    response = client.post(f"/api/v1/toolbox/executions/{execution_id}/run", headers=bot_headers)
    assert response.status_code == 409


def test_managed_firecrawl_run_rejects_executing_without_provider_call(client, bot_headers, engine, monkeypatch):
    app.dependency_overrides[get_settings] = lambda: Settings(firecrawl_api_key="test-firecrawl-secret")

    def fail_if_called(arguments, *, api_key):  # pragma: no cover
        raise AssertionError("provider must not be called for executing replay")

    monkeypatch.setattr("payjent.main.run_firecrawl_scrape", fail_if_called)
    execution_id = _ready_execution(engine, status="executing")
    response = client.post(f"/api/v1/toolbox/executions/{execution_id}/run", headers=bot_headers)
    assert response.status_code == 409
    assert response.json()["detail"] == "tool execution is already executing"


def test_managed_firecrawl_run_requires_auth(client, engine):
    execution_id = _ready_execution(engine)
    response = client.post(f"/api/v1/toolbox/executions/{execution_id}/run")
    assert response.status_code in {401, 403}


def test_managed_firecrawl_run_rejects_wrong_bot(client, engine):
    with Session(engine) as session:
        create_bot_credential(session, "bot-2", "test-bot-2-key", get_settings().signing_secret)
    execution_id = _ready_execution(engine)
    response = client.post(f"/api/v1/toolbox/executions/{execution_id}/run", headers={"Authorization": "Bearer test-bot-2-key"})
    assert response.status_code == 403


def test_managed_firecrawl_run_succeeds_with_mocked_provider(client, bot_headers, engine, monkeypatch):
    app.dependency_overrides[get_settings] = lambda: Settings(firecrawl_api_key="test-firecrawl-secret")

    def fake_run(arguments, *, api_key):
        assert api_key == "test-firecrawl-secret"
        assert arguments["url"]["canonical_url"] == "https://example.com/page?x=1"
        return {"provider": "firecrawl", "tool_id": "firecrawl.scrape", "url_summary": {"scheme": "https", "host": "example.com"}, "markdown": "safe"}

    monkeypatch.setattr("payjent.main.run_firecrawl_scrape", fake_run)
    execution_id = _ready_execution(engine)
    response = client.post(f"/api/v1/toolbox/executions/{execution_id}/run", headers=bot_headers)
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "succeeded"
    assert body["arguments_json"]["url"] == {"scheme": "https", "host": "example.com"}
    assert body["result_metadata_json"]["markdown"] == "safe"
    _assert_no_secret_or_executable_marker(body)


def test_managed_firecrawl_run_missing_provider_config_fails_closed(client, bot_headers, engine):
    app.dependency_overrides[get_settings] = lambda: Settings(firecrawl_api_key=None)
    execution_id = _ready_execution(engine)
    response = client.post(f"/api/v1/toolbox/executions/{execution_id}/run", headers=bot_headers)
    assert response.status_code == 503
    with Session(engine) as session:
        execution = session.get(ToolExecution, execution_id)
        assert execution.status == "failed"
        assert execution.error_metadata_json["code"] == "provider_not_configured"


def test_managed_firecrawl_provider_failure_records_sanitized_failed_state(client, bot_headers, engine, monkeypatch):
    from payjent.providers.firecrawl import FirecrawlProviderError

    app.dependency_overrides[get_settings] = lambda: Settings(firecrawl_api_key="test-firecrawl-secret")

    def fake_run(arguments, *, api_key):
        raise FirecrawlProviderError("test-firecrawl-secret raw provider traceback")

    monkeypatch.setattr("payjent.main.run_firecrawl_scrape", fake_run)
    execution_id = _ready_execution(engine)
    response = client.post(f"/api/v1/toolbox/executions/{execution_id}/run", headers=bot_headers)
    assert response.status_code == 200
    assert response.json()["error_metadata_json"] == {"code": "provider_execution_failed", "message": "managed provider execution failed"}
    _assert_no_secret_or_executable_marker(response.json())


def test_managed_firecrawl_adapter_validates_and_sanitizes_response():
    from payjent.providers.firecrawl import run_scrape

    def fake_transport(payload, api_key):
        assert payload["url"] == "https://example.com/page"
        assert api_key == "test-firecrawl-secret"
        return {"data": {"markdown": "x" * 5000, "html": "<p>ok</p>", "links": ["https://a.example"] * 30, "metadata": {"title": "T", "headers": {"Authorization": "secret"}, "sourceURL": "https://example.com/page"}}}

    result = run_scrape({"url": "https://example.com/page", "formats": ["markdown", "links"]}, api_key="test-firecrawl-secret", transport=fake_transport)
    assert len(result["markdown"]) == 4000
    assert len(result["links"]) == 25
    assert result["metadata"] == {"title": "T"}
    _assert_no_secret_or_executable_marker(result)


def test_managed_firecrawl_adapter_rejects_invalid_input_before_transport():
    from payjent.providers.firecrawl import run_scrape

    calls = 0

    def fake_transport(payload, api_key):  # pragma: no cover
        nonlocal calls
        calls += 1
        return {}

    invalid_arguments = [
        ({"url": "http://example.com"}, "public HTTPS"),
        ({"url": "https://localhost/a"}, "public HTTPS"),
        ({"url": "https://127.0.0.1/a"}, "public HTTPS"),
        ({"url": "https://169.254.169.254/a"}, "public HTTPS"),
        ({"url": "https://metadata.google.internal/a"}, "public HTTPS"),
        ({"url": "https://service.local/a"}, "public HTTPS"),
        ({"url": "https://example.com", "api_key": "***"}, "provider credentials are not accepted"),
        ({"url": "https://example.com", "formats": ["screenshot"]}, "unsupported"),
        ({"url": "https://example.com", "only_main_content": "yes"}, "boolean"),
    ]
    for arguments, expected in invalid_arguments:
        try:
            run_scrape(arguments, api_key="test-firecrawl-secret", transport=fake_transport)
        except ValueError as exc:
            assert expected in str(exc)
        else:  # pragma: no cover
            raise AssertionError(f"expected ValueError for {arguments}")
    assert calls == 0


def test_managed_firecrawl_adapter_sanitizes_transport_value_error():
    from payjent.providers.firecrawl import FirecrawlProviderError, run_scrape

    def fake_transport(payload, api_key):
        raise ValueError("raw provider body includes test-firecrawl-secret")

    try:
        run_scrape({"url": "https://example.com", "formats": ["markdown"]}, api_key="test-firecrawl-secret", transport=fake_transport)
    except FirecrawlProviderError as exc:
        assert str(exc) == "provider_execution_failed"
    else:  # pragma: no cover
        raise AssertionError("expected FirecrawlProviderError")
