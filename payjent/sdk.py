from __future__ import annotations

from typing import Any

import httpx


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
