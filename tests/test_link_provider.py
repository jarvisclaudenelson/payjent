import pytest
from sqlmodel import Session, select

from payjent.models import Grant, PaymentSession
from payjent.providers.link import (
    LINK_AUTH_STATUS_COMMAND,
    LinkCredentialRequest,
    build_link_cli_command_sequence,
    build_link_retrieve_command,
    build_link_spend_request_command,
    create_link_spend_request,
    parse_link_spend_request_response,
    parse_link_status_response,
    retrieve_link_status,
    run_link_cli_retrieve,
    run_link_cli_spend_request,
    validate_credential_type,
)


def test_link_checkout_provider_creates_unpaid_session(client, quote_payload, bot_headers):
    q = client.post("/api/v1/quotes", json=quote_payload, headers=bot_headers).json()
    r = client.post(f"/api/v1/quotes/{q['id']}/checkout", headers={**bot_headers, "X-Payjent-Provider": "link"})
    assert r.status_code == 200
    ps = r.json()
    assert ps["provider"] == "link"
    assert ps["status"] == "checkout_created"
    assert ps["checkout_url"] == f"/pay/{ps['id']}"
    assert ps["receipt_id"] is None


def test_credential_type_is_explicit_and_not_defaulted():
    for bad in (None, "", "   "):
        with pytest.raises(ValueError, match="does not infer or default to card"):
            validate_credential_type(bad)
    req = LinkCredentialRequest(
        merchant_url="https://merchant.example/checkout",
        credential_type="card",
        amount_minor=1234,
        currency="usd",
        purpose="Buy bounded item",
        external_user_id="user-1",
    )
    argv = build_link_spend_request_command(req)
    assert "--credential-type" in argv
    assert argv[argv.index("--credential-type") + 1] == "card"
    assert "--format" in argv and "json" in argv


def test_parse_link_response_requires_approval_url_and_extracts_next_command():
    approval = parse_link_spend_request_response({
        "id": "sr_123",
        "approval_url": "https://link.com/approve/sr_123",
        "_next": {"command": ["link-cli", "spend-request", "retrieve", "sr_123", "--format", "json"]},
    })
    assert approval.provider_session_id == "sr_123"
    assert approval.approval_url.endswith("sr_123")
    assert approval.polling_command == ["link-cli", "spend-request", "retrieve", "sr_123", "--format", "json"]
    with pytest.raises(ValueError, match="missing approval_url"):
        parse_link_spend_request_response({"id": "sr_missing"})


def test_parse_link_status_response_is_conservative():
    cases = [
        ({"id": "sr_missing"}, "unknown", None, False),
        ({"id": "sr_approved", "status": "approved"}, "approved_not_settled", "approved", False),
        ({"id": "sr_cred", "credential": {"status": "credential_created"}}, "credential_created_not_settled", "credential_created", False),
        ({"id": "sr_failed", "state": "failed"}, "failed", "failed", False),
        ({"id": "sr_paid", "status": "merchant_charge_succeeded"}, "settled", "merchant_charge_succeeded", True),
    ]
    for raw, expected, raw_status, settled in cases:
        result = parse_link_status_response(raw)
        assert result.normalized_status == expected
        assert result.raw_status == raw_status
        assert result.is_settled is settled


def test_link_retrieve_orchestrator_prefers_mcp_and_cli_auth_preflight():
    assert build_link_retrieve_command("sr_123") == ["link-cli", "spend-request", "retrieve", "sr_123", "--format", "json"]
    assert retrieve_link_status("sr_mcp", mcp_client=lambda provider_session_id: {"id": provider_session_id, "status": "approved"}).normalized_status == "approved_not_settled"

    commands = []

    def runner(command):
        commands.append(command)
        if command == LINK_AUTH_STATUS_COMMAND:
            return {"authenticated": True}
        return {"id": "sr_cli", "status": "credential_created"}

    result = run_link_cli_retrieve("sr_cli", runner=runner)
    assert result.normalized_status == "credential_created_not_settled"
    assert commands == [LINK_AUTH_STATUS_COMMAND, build_link_retrieve_command("sr_cli")]


def test_link_spend_request_endpoint_does_not_mark_paid_or_issue_grant(monkeypatch, engine, client, quote_payload, bot_headers, operator_headers):
    from payjent.providers.link import LinkApproval
    import payjent.main as main

    captured = {}

    def fake_run(payload):
        captured["payload"] = payload
        return LinkApproval(
            approval_url="https://link.com/approve/sr_test",
            provider_session_id="sr_test",
            polling_command=["link-cli", "spend-request", "retrieve", "sr_test", "--format", "json"],
            raw={"id": "sr_test"},
        )

    monkeypatch.setattr(main, "create_link_provider_spend_request", fake_run)
    q = client.post("/api/v1/quotes", json=quote_payload, headers=bot_headers).json()
    ps = client.post(f"/api/v1/quotes/{q['id']}/checkout", headers={**bot_headers, "X-Payjent-Provider": "link"}).json()
    r = client.post(
        f"/api/v1/payment-sessions/{ps['id']}/link/spend-request",
        headers=operator_headers,
        json={"merchant_url": "https://merchant.example/checkout", "credential_type": "card", "metadata": {"merchant": "example"}},
    )
    assert r.status_code == 200
    data = r.json()
    assert data["approval_url"] == "https://link.com/approve/sr_test"
    assert data["provider_session_id"] == "sr_test"
    assert data["payment_session"]["status"] == "checkout_created"
    assert captured["payload"].amount_minor == quote_payload["amount_minor"]
    assert captured["payload"].metadata["payjent_payment_session_id"] == ps["id"]

    with Session(engine) as session:
        stored = session.get(PaymentSession, ps["id"])
        assert stored.status == "checkout_created"
        assert stored.receipt_id is None
        assert session.exec(select(Grant).where(Grant.payment_session_id == ps["id"])).first() is None


def test_link_spend_request_rejects_missing_credential_type(client, quote_payload, bot_headers, operator_headers):
    q = client.post("/api/v1/quotes", json=quote_payload, headers=bot_headers).json()
    ps = client.post(f"/api/v1/quotes/{q['id']}/checkout", headers={**bot_headers, "X-Payjent-Provider": "link"}).json()
    r = client.post(
        f"/api/v1/payment-sessions/{ps['id']}/link/spend-request",
        headers=operator_headers,
        json={"merchant_url": "https://merchant.example/checkout", "credential_type": ""},
    )
    assert r.status_code == 422


def _create_link_session_with_provider_id(client, engine, quote_payload, bot_headers, provider_session_id="sr_poll"):
    q = client.post("/api/v1/quotes", json=quote_payload, headers=bot_headers).json()
    ps = client.post(f"/api/v1/quotes/{q['id']}/checkout", headers={**bot_headers, "X-Payjent-Provider": "link"}).json()
    with Session(engine) as session:
        stored = session.get(PaymentSession, ps["id"])
        stored.provider_session_id = provider_session_id
        session.add(stored)
        session.commit()
    return ps


@pytest.mark.parametrize("raw_status,expected", [
    ("approved", "approved_not_settled"),
    ("credential_created", "credential_created_not_settled"),
    (None, "unknown"),
    ("merchant_charge_succeeded", "settled"),
])
def test_link_poll_endpoint_fail_closed_for_all_current_statuses(monkeypatch, engine, client, quote_payload, bot_headers, operator_headers, raw_status, expected):
    import payjent.main as main

    ps = _create_link_session_with_provider_id(client, engine, quote_payload, bot_headers)

    def fake_retrieve(provider_session_id):
        raw = {"id": provider_session_id}
        if raw_status is not None:
            raw["status"] = raw_status
        return parse_link_status_response(raw, provider_session_id=provider_session_id)

    monkeypatch.setattr(main, "retrieve_link_provider_status", fake_retrieve)
    r = client.post(f"/api/v1/payment-sessions/{ps['id']}/link/poll", headers=operator_headers)
    assert r.status_code == 200
    data = r.json()
    assert data["normalized_status"] == expected
    assert data["payment_session"]["status"] == "checkout_created"
    assert data["settlement_mapping_required"] is True

    with Session(engine) as session:
        stored = session.get(PaymentSession, ps["id"])
        assert stored.status == "checkout_created"
        assert stored.receipt_id is None
        assert session.exec(select(Grant).where(Grant.payment_session_id == ps["id"])).first() is None


def test_link_poll_rejects_non_link_and_missing_provider_session_id(client, quote_payload, bot_headers, operator_headers):
    q = client.post("/api/v1/quotes", json=quote_payload, headers=bot_headers).json()
    non_link = client.post(f"/api/v1/quotes/{q['id']}/checkout", headers=bot_headers).json()
    r = client.post(f"/api/v1/payment-sessions/{non_link['id']}/link/poll", headers=operator_headers)
    assert r.status_code == 409
    assert "provider is not link" in r.json()["detail"]

    link_ps = client.post(f"/api/v1/quotes/{q['id']}/checkout", headers={**bot_headers, "X-Payjent-Provider": "link"}).json()
    r = client.post(f"/api/v1/payment-sessions/{link_ps['id']}/link/poll", headers=operator_headers)
    assert r.status_code == 409
    assert "provider_session_id" in r.json()["detail"]


def _provider_request(**overrides):
    data = {
        "merchant_url": "https://merchant.example/checkout",
        "credential_type": "card",
        "amount_minor": 1234,
        "currency": "usd",
        "purpose": "Buy bounded item",
        "external_user_id": "user-1",
    }
    data.update(overrides)
    return LinkCredentialRequest(**data)


def test_link_orchestrator_prefers_mcp_callable_over_cli_runner():
    calls = []

    def mcp(payload):
        calls.append(("mcp", payload.merchant_url))
        return {"id": "sr_mcp", "approval_url": "https://link.example/approve/sr_mcp"}

    def cli(_command):
        calls.append(("cli", None))
        raise AssertionError("CLI should not run when MCP is injected")

    approval = create_link_spend_request(_provider_request(), mcp_client=mcp, cli_runner=cli)
    assert approval.provider_session_id == "sr_mcp"
    assert calls == [("mcp", "https://merchant.example/checkout")]


def test_link_orchestrator_falls_back_to_cli_when_mcp_absent():
    commands = []

    def runner(command):
        commands.append(command)
        if command == LINK_AUTH_STATUS_COMMAND:
            return {"authenticated": True}
        return {"id": "sr_cli", "approval_url": "https://link.example/approve/sr_cli"}

    approval = create_link_spend_request(_provider_request(), cli_runner=runner)
    assert approval.provider_session_id == "sr_cli"
    assert commands == build_link_cli_command_sequence(_provider_request())


def test_cli_auth_status_precedes_spend_request_and_unauthenticated_stops():
    commands = []

    def runner(command):
        commands.append(command)
        if command == LINK_AUTH_STATUS_COMMAND:
            return {"authenticated": True}
        return {"id": "sr_cli", "approval_url": "https://link.example/approve/sr_cli"}

    run_link_cli_spend_request(_provider_request(), runner=runner)
    assert commands[0] == LINK_AUTH_STATUS_COMMAND
    assert commands[1][0:3] == ["link-cli", "spend-request", "create"]

    unauthenticated_commands = []

    def unauthenticated_runner(command):
        unauthenticated_commands.append(command)
        return {"authenticated": False}

    with pytest.raises(RuntimeError, match="link-cli is not authenticated.*auth login"):
        run_link_cli_spend_request(_provider_request(), runner=unauthenticated_runner)
    assert unauthenticated_commands == [LINK_AUTH_STATUS_COMMAND]


def test_url_validation_rejects_non_http_urls():
    with pytest.raises(ValueError, match="merchant_url must be an http or https URL"):
        _provider_request(merchant_url="ftp://merchant.example/checkout")
    with pytest.raises(ValueError, match="approval_url must be an http or https URL"):
        parse_link_spend_request_response({"id": "sr_bad", "approval_url": "javascript:alert(1)"})


def test_unknown_credential_type_is_not_supported():
    with pytest.raises(ValueError, match="unsupported credential_type 'unknown'"):
        validate_credential_type("unknown")


def test_link_spend_request_rejects_reserved_metadata(client, quote_payload, bot_headers, operator_headers):
    q = client.post("/api/v1/quotes", json=quote_payload, headers=bot_headers).json()
    ps = client.post(f"/api/v1/quotes/{q['id']}/checkout", headers={**bot_headers, "X-Payjent-Provider": "link"}).json()
    r = client.post(
        f"/api/v1/payment-sessions/{ps['id']}/link/spend-request",
        headers=operator_headers,
        json={
            "merchant_url": "https://merchant.example/checkout",
            "credential_type": "card",
            "metadata": {"payjent_quote_id": "evil"},
        },
    )
    assert r.status_code == 422
    assert "reserved Payjent keys" in r.json()["detail"]


def test_link_spend_request_rejects_invalid_merchant_url(client, quote_payload, bot_headers, operator_headers):
    q = client.post("/api/v1/quotes", json=quote_payload, headers=bot_headers).json()
    ps = client.post(f"/api/v1/quotes/{q['id']}/checkout", headers={**bot_headers, "X-Payjent-Provider": "link"}).json()
    r = client.post(
        f"/api/v1/payment-sessions/{ps['id']}/link/spend-request",
        headers=operator_headers,
        json={"merchant_url": "file:///tmp/card", "credential_type": "card"},
    )
    assert r.status_code == 422
