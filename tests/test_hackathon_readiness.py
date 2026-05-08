import httpx

from payjent.sdk import PayjentClient


def test_product_docs_route_and_swagger_do_not_conflict(monkeypatch):
    from fastapi.testclient import TestClient

    from payjent.config import get_settings
    from payjent.main import app

    monkeypatch.setenv("PAYJENT_ENV", "local")
    monkeypatch.setenv("PAYJENT_DATABASE_URL", "sqlite://")
    get_settings.cache_clear()
    with TestClient(app) as client:
        docs = client.get("/docs")
        assert docs.status_code == 200
        assert "Payjent agent setup" in docs.text
        assert "Swagger UI" not in docs.text

        swagger = client.get("/api-docs")
        assert swagger.status_code == 200
        assert "Swagger UI" in swagger.text
    get_settings.cache_clear()


def test_sdk_toolbox_runtime_pricing_methods_use_expected_endpoints_and_headers():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path, request.headers.get("authorization"), request.headers.get("idempotency-key"), request.content))
        responses = {
            ("GET", "/api/v1/toolbox"): {"tools": [], "count": 0},
            ("GET", "/api/v1/toolbox/fal.image"): {"tool_id": "fal.image"},
            ("POST", "/api/v1/toolbox/fal.image/quote"): {"amount_minor": 123, "currency": "USD", "cost_breakdown": [{"label": "fal runtime", "amount_minor": 123}]},
            ("POST", "/api/v1/toolbox/fal.image/checkout"): {"status": "checkout_created", "payment_url": "https://www.payjent.com/pay/ps_1"},
            ("POST", "/api/v1/toolbox/fal.image/executions"): {"id": "texec_1", "status": "ready"},
            ("GET", "/api/v1/toolbox/executions/texec_1"): {"id": "texec_1", "status": "ready"},
            ("POST", "/api/v1/toolbox/executions/texec_1/run"): {"id": "texec_1", "status": "running"},
            ("POST", "/api/v1/toolbox/executions/texec_1/complete"): {"id": "texec_1", "status": "completed"},
            ("POST", "/api/v1/toolbox/executions/texec_1/fail"): {"id": "texec_1", "status": "failed"},
        }
        body = responses.get((request.method, request.url.path))
        if body is None:
            return httpx.Response(404, json={"detail": "not found"})
        return httpx.Response(200, json=body)

    http_client = httpx.Client(transport=httpx.MockTransport(handler), base_url="http://testserver")
    sdk = PayjentClient(base_url="http://testserver", api_key="bot-key", client=http_client)
    payload = {
        "bot_id": "bot",
        "external_user_id": "user",
        "arguments": {"prompt": "generate a product mockup"},
        "amount_minor": 123,
        "currency": "USD",
        "cost_breakdown": [{"label": "fal runtime", "amount_minor": 123}],
    }

    assert sdk.list_toolbox_tools()["count"] == 0
    assert sdk.get_toolbox_tool("fal.image")["tool_id"] == "fal.image"
    quote = sdk.quote_toolbox_action("fal.image", **payload)
    assert quote["amount_minor"] == 123
    assert quote["currency"] == "USD"
    assert quote["cost_breakdown"][0]["amount_minor"] == 123
    assert sdk.create_toolbox_checkout("fal.image", idempotency_key="idem-1", **payload)["payment_url"].startswith("https://www.payjent.com/")
    assert sdk.create_toolbox_execution("fal.image", **payload)["id"] == "texec_1"
    assert sdk.get_toolbox_execution("texec_1")["status"] == "ready"
    assert sdk.run_toolbox_execution("texec_1")["status"] == "running"
    assert sdk.complete_toolbox_execution("texec_1", result={"ok": True})["status"] == "completed"
    assert sdk.fail_toolbox_execution("texec_1", error="provider failed") ["status"] == "failed"

    assert all(call[2] == "Bearer bot-key" for call in calls)
    assert ("POST", "/api/v1/toolbox/fal.image/checkout", "Bearer bot-key", "idem-1", calls[3][4]) in calls
