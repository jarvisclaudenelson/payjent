# Decal checkout rail

Decal is Payjent's primary active hosted checkout provider. In production set `PAYJENT_CHECKOUT_PROVIDER=decal`, `PAYJENT_DECAL_API_KEY`, and a canonical HTTPS `PAYJENT_PUBLIC_BASE_URL`.

Payjent creates Decal checkout sessions with `POST /v0/checkout/sessions`, stores the Decal session id and hosted checkout URL, and receives completion callbacks at `/api/v1/webhooks/decal`. Because Decal webhook delivery is currently best-effort and does not document signature/retry semantics, Payjent verifies completed callbacks server-side by retrieving the Decal checkout session before issuing a paid grant.

Stripe remains a legacy fallback only when explicitly configured with `PAYJENT_CHECKOUT_PROVIDER=stripe` and the Stripe secrets/webhook secret.

## Decal feature gaps to track

- Automatic paid refunds API and Payjent refund integration.
- Disputes/chargebacks lifecycle and operator notifications.
- Webhook retry and signature semantics.
- Reconciliation/polling for missed best-effort webhooks.
