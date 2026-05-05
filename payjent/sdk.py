from __future__ import annotations

from typing import Any

import httpx

from .signing import verify_webhook_signature


class PayjentClient:
    """Small synchronous helper for bot integrations."""

    def __init__(self, base_url: str = "http://127.0.0.1:8000", api_key: str | None = None, client: httpx.Client | None = None):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.client = client or httpx.Client(base_url=self.base_url)

    def _headers(self, idempotency_key: str | None = None) -> dict[str, str]:
        headers: dict[str, str] = {}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        return headers

    def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        response = self.client.request(method, path, **kwargs)
        response.raise_for_status()
        return response.json()

    def create_quote(
        self,
        *,
        bot_id: str,
        external_user_id: str,
        request_summary: str,
        amount_minor: int,
        currency: str,
        cost_breakdown: list[dict[str, Any]],
        request_hash: str | None = None,
        execution_envelope: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            "/api/v1/quotes",
            headers=self._headers(),
            json={
                "bot_id": bot_id,
                "external_user_id": external_user_id,
                "request_summary": request_summary,
                "request_hash": request_hash,
                "amount_minor": amount_minor,
                "currency": currency,
                "cost_breakdown": cost_breakdown,
                "execution_envelope": execution_envelope or {},
            },
        )

    def create_agent_action(
        self,
        *,
        bot_id: str,
        external_user_id: str,
        request_summary: str,
        request_hash: str,
        amount_minor: int,
        currency: str,
        cost_breakdown: list[dict[str, Any]],
        execution_envelope: dict[str, Any],
        callback_url: str | None = None,
    ) -> dict[str, Any]:
        """Create a Payjent-gated agent action and checkout prompt."""
        return self._request(
            "POST",
            "/api/v1/agent-actions",
            headers=self._headers(),
            json={
                "bot_id": bot_id,
                "external_user_id": external_user_id,
                "request_summary": request_summary,
                "request_hash": request_hash,
                "amount_minor": amount_minor,
                "currency": currency,
                "cost_breakdown": cost_breakdown,
                "execution_envelope": execution_envelope,
                "callback_url": callback_url,
            },
        )

    def create_pay_sh_premium_action(self, **payload: Any) -> dict[str, Any]:
        """Create a Payjent-gated pay.sh action; Payjent does not execute paycurl."""
        return self._request("POST", "/api/v1/premium-actions/pay-sh", headers=self._headers(), json=payload)

    def get_agent_action_status(self, action_id: str) -> dict[str, Any]:
        """Fetch bot-scoped action/payment readiness, including an unconsumed token when paid."""
        return self._request("GET", f"/api/v1/agent-actions/{action_id}", headers=self._headers())

    def get_agent_action(self, action_id: str) -> dict[str, Any]:
        return self.get_agent_action_status(action_id)

    def consume_agent_action(
        self,
        action_id: str,
        payment_token: str,
        presentation: dict[str, Any] | None = None,
        **presentation_fields: Any,
    ) -> dict[str, Any]:
        """Consume/start a paid action and return its stored execution envelope."""
        payload = {"payment_token": payment_token, "presentation": presentation or presentation_fields}
        return self._request("POST", f"/api/v1/agent-actions/{action_id}/consume", headers=self._headers(), json=payload)

    def complete_agent_action(self, action_id: str, status: str, metadata: dict[str, Any] | None = None) -> dict[str, Any]:
        return self._request(
            "POST",
            f"/api/v1/agent-actions/{action_id}/complete",
            headers=self._headers(),
            json={"status": status, "metadata": metadata or {}},
        )

    def get_quote(self, quote_id: str) -> dict[str, Any]:
        return self._request("GET", f"/api/v1/quotes/{quote_id}")

    def create_checkout(self, quote_id: str, idempotency_key: str | None = None) -> dict[str, Any]:
        return self._request("POST", f"/api/v1/quotes/{quote_id}/checkout", headers=self._headers(idempotency_key))

    def get_payment_session(self, session_id: str) -> dict[str, Any]:
        return self._request("GET", f"/api/v1/payment-sessions/{session_id}")

    def verify_grant(self, grant_id: str, **presentation: Any) -> dict[str, Any]:
        return self._request("POST", f"/api/v1/grants/{grant_id}/verify", headers=self._headers(), json=presentation)

    def consume_grant(self, grant_id: str, **presentation: Any) -> dict[str, Any]:
        return self._request("POST", f"/api/v1/grants/{grant_id}/consume", headers=self._headers(), json=presentation)

    def authorize_spend(self, grant_id: str, **payload: Any) -> dict[str, Any]:
        return self._request(
            "POST",
            f"/api/v1/grants/{grant_id}/spend-authorizations",
            headers=self._headers(),
            json=payload,
        )

    def capture_spend(self, spend_id: str, **payload: Any) -> dict[str, Any]:
        return self._request(
            "POST",
            f"/api/v1/spend-authorizations/{spend_id}/capture",
            headers=self._headers(),
            json=payload,
        )

    def record_fulfillment(self, quote_id: str, status: str, metadata: dict[str, Any] | None = None) -> dict[str, Any]:
        return self._request(
            "POST",
            f"/api/v1/quotes/{quote_id}/fulfillment",
            headers=self._headers(),
            json={"status": status, "metadata": metadata or {}},
        )


def verify_agent_action_webhook(payload: dict[str, Any], timestamp: str, signature: str, secret: str, tolerance_seconds: int = 300) -> bool:
    """Verify Payjent outbound webhook HMAC headers for agent runtimes."""
    return verify_webhook_signature(payload, timestamp, signature, secret, tolerance_seconds=tolerance_seconds)


def create_quote(client: PayjentClient, **kwargs: Any) -> dict[str, Any]:
    return client.create_quote(**kwargs)


def create_checkout(client: PayjentClient, quote_id: str, idempotency_key: str | None = None) -> dict[str, Any]:
    return client.create_checkout(quote_id, idempotency_key=idempotency_key)


def get_quote(client: PayjentClient, quote_id: str) -> dict[str, Any]:
    return client.get_quote(quote_id)


def get_payment_session(client: PayjentClient, session_id: str) -> dict[str, Any]:
    return client.get_payment_session(session_id)


def verify_grant(client: PayjentClient, grant_id: str, **presentation: Any) -> dict[str, Any]:
    return client.verify_grant(grant_id, **presentation)


def consume_grant(client: PayjentClient, grant_id: str, **presentation: Any) -> dict[str, Any]:
    return client.consume_grant(grant_id, **presentation)


def authorize_spend(client: PayjentClient, grant_id: str, **payload: Any) -> dict[str, Any]:
    return client.authorize_spend(grant_id, **payload)


def capture_spend(client: PayjentClient, spend_id: str, **payload: Any) -> dict[str, Any]:
    return client.capture_spend(spend_id, **payload)


def record_fulfillment(client: PayjentClient, quote_id: str, status: str, metadata: dict[str, Any] | None = None) -> dict[str, Any]:
    return client.record_fulfillment(quote_id, status, metadata)


def create_agent_action(client: PayjentClient, **kwargs: Any) -> dict[str, Any]:
    return client.create_agent_action(**kwargs)


def create_pay_sh_premium_action(client: PayjentClient, **payload: Any) -> dict[str, Any]:
    return client.create_pay_sh_premium_action(**payload)


def consume_agent_action(
    client: PayjentClient,
    action_id: str,
    payment_token: str,
    presentation: dict[str, Any] | None = None,
    **presentation_fields: Any,
) -> dict[str, Any]:
    return client.consume_agent_action(action_id, payment_token, presentation, **presentation_fields)


def complete_agent_action(client: PayjentClient, action_id: str, status: str, metadata: dict[str, Any] | None = None) -> dict[str, Any]:
    return client.complete_agent_action(action_id, status, metadata)
