from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

from sqlmodel import Session, select

from payjent.auth import hash_api_key
from payjent.config import get_settings
from payjent.models import AgentInstallLink, AgentProfile, BotCredential


def _register_owner_and_agent(client):
    client.post("/auth/register", data={"email": "owner@example.com", "password": "correct-horse-battery"}, follow_redirects=False)
    response = client.post(
        "/dashboard/agents/register",
        data={"name": "Research Agent", "platform": "cli", "bot_id": "agent-install-bot", "default_currency": "USD"},
    )
    assert response.status_code == 200
    dashboard = client.get("/dashboard")
    marker = "data-agent-id='"
    agent_id = dashboard.text.split(marker, 1)[1].split("'", 1)[0]
    return agent_id


def test_dashboard_creates_one_time_agent_install_link(client, engine):
    agent_id = _register_owner_and_agent(client)
    response = client.post("/dashboard/agents/install-links", json={"agent_id": agent_id})
    assert response.status_code == 200
    body = response.json()
    assert body["agent_id"] == agent_id
    assert body["bot_id"] == "agent-install-bot"
    assert body["install_url"].startswith("http://testserver/agent-install/")
    assert "one-time install link" in body["instructions"]
    assert "raw credentials" in body["instructions"]
    token = urlparse(body["install_url"]).path.rsplit("/", 1)[-1]
    assert len(token) > 32

    with Session(engine) as session:
        link = session.exec(select(AgentInstallLink).where(AgentInstallLink.id == body["install_link_id"])).one()
        assert link.token_hash == hash_api_key(f"agent-install:{token}", get_settings().signing_secret)
        assert token not in link.token_hash
        assert link.owner_id.startswith("acct_")
        assert link.agent_id == agent_id
        assert link.bot_id == "agent-install-bot"
        assert link.consumed_at is None


def test_dashboard_register_primary_flow_does_not_show_or_create_raw_credential(client, engine):
    client.post("/auth/register", data={"email": "owner@example.com", "password": "correc...tery"}, follow_redirects=False)
    response = client.post(
        "/dashboard/agents/register",
        data={"name": "Research Agent", "platform": "cli", "bot_id": "agent-install-bot", "default_currency": "USD"},
    )
    assert response.status_code == 200
    assert "One-time Agent Install Link" in response.text
    assert "payjent_" not in response.text
    assert "Copy this Payjent agent credential now" not in response.text
    assert "Primary safe setup" in response.text
    with Session(engine) as session:
        assert session.exec(select(AgentInstallLink)).one().consumed_at is None
        assert session.exec(select(BotCredential).where(BotCredential.bot_id == "agent-install-bot")).all() == []


def test_agent_install_link_redeems_credential_once_with_agent_scope(client):
    agent_id = _register_owner_and_agent(client)
    install_url = client.post("/dashboard/agents/install-links", json={"agent_id": agent_id}).json()["install_url"]

    landing = client.get(install_url)
    assert landing.status_code == 200
    assert "One-time Agent Install Link" in landing.text
    assert "raw credentials" in landing.text
    assert "payjent_" not in landing.text

    redeemed = client.post(install_url)
    assert redeemed.status_code == 200
    body = redeemed.json()
    assert body["agent_id"] == agent_id
    assert body["bot_id"] == "agent-install-bot"
    assert body["payjent_base_url"] == "http://testserver"
    assert body["credential"]["type"] == "payjent_agent_api_key"
    assert body["credential"]["value"].startswith("payjent_")
    assert body["credential"]["header"] == "X-Payjent-Bot-Key"
    assert body["policy"]["credential_scope"] == "agent"
    assert body["policy"]["single_agent_only"] is True
    assert "operator" not in body["scopes"]
    assert "grant_id" not in str(body).lower()
    assert "payment_token" not in str(body).lower()

    second = client.post(install_url)
    assert second.status_code == 404
    assert "invalid, expired, or already used" in second.text


def test_expired_agent_install_link_fails_safely(client, engine):
    agent_id = _register_owner_and_agent(client)
    install_url = client.post("/dashboard/agents/install-links", json={"agent_id": agent_id}).json()["install_url"]
    with Session(engine) as session:
        token = urlparse(install_url).path.rsplit("/", 1)[-1]
        link = session.exec(select(AgentInstallLink).where(AgentInstallLink.token_hash == hash_api_key(f"agent-install:{token}", get_settings().signing_secret))).one()
        link.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        session.add(link)
        session.commit()

    response = client.post(install_url)
    assert response.status_code == 404
    assert "payjent_" not in response.text
    assert "agent-install-bot" not in response.text


def test_redeemed_credential_is_not_owner_or_operator_token(client, engine):
    agent_id = _register_owner_and_agent(client)
    install_url = client.post("/dashboard/agents/install-links", json={"agent_id": agent_id}).json()["install_url"]
    api_key = client.post(install_url).json()["credential"]["value"]
    with Session(engine) as session:
        key_hash = hash_api_key(api_key, get_settings().signing_secret)
        credential = session.exec(select(BotCredential).where(BotCredential.key_hash == key_hash)).one()
        assert credential.bot_id == "agent-install-bot"
        assert credential.role == "bot"


def test_agent_install_link_second_redemption_does_not_create_duplicate_credential(client, engine):
    agent_id = _register_owner_and_agent(client)
    install_url = client.post("/dashboard/agents/install-links", json={"agent_id": agent_id}).json()["install_url"]
    assert client.post(install_url).status_code == 200
    assert client.post(install_url).status_code == 404
    with Session(engine) as session:
        credentials = session.exec(select(BotCredential).where(BotCredential.bot_id == "agent-install-bot")).all()
        assert len(credentials) == 1


def test_agent_setup_doc_mentions_install_link_and_forbids_raw_credential_chat(client):
    response = client.get("/docs/agent-payjent-self-setup.md")
    assert response.status_code == 200
    text = response.text.lower()
    assert "one-time agent install link" in text
    assert "registers the target agent" in text or "register" in text
    assert "do not ask them for code snippets" in text
    assert "raw credentials" in text
    assert "chat" in text


QUOTE_PAYLOAD = {
    "external_user_id": "user-1",
    "request_summary": "do a thing",
    "amount_minor": 100,
    "currency": "USD",
    "cost_breakdown": [{"label": "work", "amount_minor": 100}],
    "execution_envelope": {"action": "test"},
}


def _quote_payload(bot_id: str, request_hash: str) -> dict:
    return {**QUOTE_PAYLOAD, "bot_id": bot_id, "request_hash": request_hash}


def test_dashboard_revoke_credentials_removes_bot_credentials_and_old_key_fails(client, engine):
    agent_id = _register_owner_and_agent(client)
    install_url = client.post("/dashboard/agents/install-links", json={"agent_id": agent_id}).json()["install_url"]
    api_key = client.post(install_url).json()["credential"]["value"]

    response = client.post(f"/dashboard/agents/{agent_id}/credentials/revoke")
    assert response.status_code == 200
    assert response.json()["credentials_revoked"] == 1

    with Session(engine) as session:
        assert session.exec(select(BotCredential).where(BotCredential.bot_id == "agent-install-bot")).all() == []

    auth_response = client.post("/api/v1/quotes", json=_quote_payload("agent-install-bot", "hash-after-revoke"), headers={"X-Payjent-Bot-Key": api_key})
    assert auth_response.status_code == 401


def test_dashboard_delete_agent_deactivates_revokes_links_and_hides_from_active_dashboard(client, engine):
    agent_id = _register_owner_and_agent(client)
    install_url = client.post("/dashboard/agents/install-links", json={"agent_id": agent_id}).json()["install_url"]
    api_key = client.post(install_url).json()["credential"]["value"]
    outstanding_url = client.post("/dashboard/agents/install-links", json={"agent_id": agent_id}).json()["install_url"]

    response = client.post(f"/dashboard/agents/{agent_id}/delete")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "deleted"
    assert body["credentials_revoked"] == 1
    assert body["install_links_invalidated"] >= 1

    with Session(engine) as session:
        agent = session.get(AgentProfile, agent_id)
        assert agent.status == "deleted"
        assert session.exec(select(BotCredential).where(BotCredential.bot_id == "agent-install-bot")).all() == []
        links = session.exec(select(AgentInstallLink).where(AgentInstallLink.agent_id == agent_id)).all()
        assert all(link.consumed_at is not None for link in links)

    dashboard = client.get("/dashboard")
    assert dashboard.status_code == 200
    assert f"data-agent-id='{agent_id}'" not in dashboard.text
    assert f"data-deleted-agent-id='{agent_id}'" in dashboard.text
    assert "Deleted agents" in dashboard.text

    assert client.post("/dashboard/agents/install-links", json={"agent_id": agent_id}).status_code == 409
    assert client.post(outstanding_url).status_code == 404
    assert client.post("/api/v1/quotes", json=_quote_payload("agent-install-bot", "hash-after-delete"), headers={"X-Payjent-Bot-Key": api_key}).status_code == 401


def test_redeeming_install_link_after_agent_deletion_fails_without_credential(client, engine):
    agent_id = _register_owner_and_agent(client)
    install_url = client.post("/dashboard/agents/install-links", json={"agent_id": agent_id}).json()["install_url"]
    with Session(engine) as session:
        agent = session.get(AgentProfile, agent_id)
        agent.status = "deleted"
        session.add(agent)
        session.commit()

    response = client.post(install_url)
    assert response.status_code == 404
    assert "payjent_" not in response.text
    with Session(engine) as session:
        assert session.exec(select(BotCredential).where(BotCredential.bot_id == "agent-install-bot")).all() == []


def test_cross_owner_dashboard_revoke_and_delete_attempts_fail(client):
    agent_id = _register_owner_and_agent(client)
    client.post("/auth/logout")
    client.post("/auth/register", data={"email": "other@example.com", "password": "correc...tery"}, follow_redirects=False)

    assert client.post(f"/dashboard/agents/{agent_id}/credentials/revoke").status_code == 404
    assert client.post(f"/dashboard/agents/{agent_id}/delete").status_code == 404
    assert client.get(f"/dashboard/agents/{agent_id}").status_code == 404
