from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_agent_owner_quickstart_doc_has_required_generic_flow_and_safe_env_names():
    text = (ROOT / "docs" / "agent-owner-quickstart.md").read_text()

    assert "any agent owner" in text
    assert "not C3PO-specific" in text
    assert "AgentPayjentBridge" in text
    assert "PayjentClient" in text
    assert "request_pay_sh_data" in text
    assert "pending.payment_message" in text
    assert "check_payment" in text
    assert "resume_when_paid" in text
    assert "mark_fulfilled" in text
    assert "Public users never paste grant ids or payment tokens" in text
    assert "bot auth" in text or "bot-auth" in text
    assert "execute" in text and "pay.sh externally" in text

    assert "PAYJENT_BASE_URL" in text
    assert "PAYJENT_BOT_KEY" in text
    assert "PAYJENT_BOT_ID" in text
    assert "PAYJENT_OPERATOR_KEY" in text
    assert "payjent_<redacted_bot_key>" in text
    assert "payjent_<redacted_operator_key>" in text
    assert "test-bot-key" not in text
    assert "test-operator-key" not in text


def test_agent_owner_env_example_contains_only_redacted_placeholders():
    text = (ROOT / ".env.agent.example").read_text()

    assert 'PAYJENT_BASE_URL="https://payjent.example.com"' in text
    assert 'PAYJENT_BOT_ID="agent_<your_agent_id>"' in text
    assert 'PAYJENT_BOT_KEY="payjent_<redacted_bot_key>"' in text
    assert '# PAYJENT_OPERATOR_KEY="payjent_<redacted_operator_key>"' in text
    assert "local/demo only" in text
    assert "Never expose" in text
    assert "payjent_" in text
    assert "payjent_" + "sk_" not in text
