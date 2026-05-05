"""Minimal C3PO/community-agent Payjent + pay.sh flow.

Run a complete local dry demo without a running server:

    python examples/c3po_payjent_pay_sh_flow.py

In production, replace the TestClient setup with:

    PayjentClient(os.environ["PAYJENT_BASE_URL"], api_key=os.environ["PAYJENT_BOT_KEY"])

and execute the resumed envelope in C3PO's own pay.sh runtime. This script never
calls paycurl or performs pay.sh settlement.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient
from sqlmodel import SQLModel, Session, create_engine
from sqlmodel.pool import StaticPool

from payjent.auth import create_bot_credential
from payjent.c3po_adapter import C3POPayjentBridge, MemoryPendingPremiumRequestStore
from payjent.config import get_settings
from payjent.db import get_session
from payjent.main import app
from payjent.sdk import PayjentClient


def main() -> None:
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    settings = get_settings()
    with Session(engine) as session:
        create_bot_credential(session, "c3po-community", "local-c3po-key", settings.signing_secret)
        create_bot_credential(session, "operator", "local-operator-key", settings.signing_secret, role="operator")

    def override_session():
        with Session(engine) as session:
            yield session

    app.dependency_overrides[get_session] = override_session
    try:
        with TestClient(app) as test_client:
            payjent = PayjentClient("http://testserver", api_key="local-c3po-key", client=test_client)
            bridge = C3POPayjentBridge(
                payjent,
                bot_id="c3po-community",
                store=MemoryPendingPremiumRequestStore(),
                public_base_url="http://testserver",
            )

            print("community ask: C3PO, fetch premium Lisbon weather data from pay.sh")
            pending, payment_message = bridge.request_pay_sh_data(
                community_user_id="community-user-1",
                summary="Premium pay.sh forecast for Lisbon",
                amount_minor=800,
                service_url="https://api.weather.ai/forecast",
                method="POST",
                body={"city": "Lisbon", "units": "metric"},
                description="premium weather data",
            )
            print("payment prompt to send:")
            print(payment_message)

            paid = test_client.post(
                f"/api/v1/payment-sessions/{pending.payment_session_id}/mock-pay",
                headers={"X-Payjent-Bot-Key": "local-operator-key"},
            ).json()
            payment_token = paid["grant"]["id"]
            print(f"mocked payment token: {payment_token}")

            resumed = bridge.resume_after_payment(
                action_id=pending.action_id,
                community_user_id="community-user-1",
                payment_token=payment_token,
            )
            print("resumed pay.sh command_preview:")
            print(resumed["execution_envelope"]["command_preview"])
            fulfilled = bridge.mark_fulfilled(pending.action_id, "fulfilled", {"example": True})
            print(f"fulfilled status: {fulfilled.status}")
    finally:
        app.dependency_overrides.clear()


if __name__ == "__main__":
    main()
