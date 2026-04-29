import pytest
from fastapi.testclient import TestClient
from sqlmodel import SQLModel, Session, create_engine
from sqlmodel.pool import StaticPool
from payjent.auth import create_bot_credential
from payjent.config import get_settings
from payjent.db import get_session
from payjent.main import app


@pytest.fixture
def engine():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    return engine


@pytest.fixture
def client(engine):
    settings = get_settings()
    with Session(engine) as session:
        create_bot_credential(session, "bot-1", "test-bot-key", settings.signing_secret)
        create_bot_credential(session, "operator-1", "test-operator-key", settings.signing_secret, role="operator")

    def override_session():
        with Session(engine) as session:
            yield session
    app.dependency_overrides[get_session] = override_session
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


@pytest.fixture
def bot_headers():
    return {"Authorization": "Bearer test-bot-key"}


@pytest.fixture
def operator_headers():
    return {"X-Payjent-Bot-Key": "test-operator-key"}


@pytest.fixture
def quote_payload():
    return {
        "bot_id":"bot-1",
        "external_user_id":"user-1",
        "request_summary":"do a thing",
        "request_hash":"hash-1",
        "amount_minor":250,
        "currency":"USD",
        "cost_breakdown":[{"label":"work","amount_minor":200},{"label":"fee","amount_minor":50}],
        "execution_envelope":{"action":"test"},
    }


@pytest.fixture
def paid_grant(client, quote_payload, bot_headers, operator_headers):
    q = client.post("/api/v1/quotes", json=quote_payload, headers=bot_headers).json()
    ps = client.post(f"/api/v1/quotes/{q['id']}/checkout", headers=bot_headers).json()
    paid = client.post(f"/api/v1/payment-sessions/{ps['id']}/mock-pay", headers=operator_headers).json()
    return q, ps, paid["grant"]
