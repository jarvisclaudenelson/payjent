# Payjent

Payjent v0 is a small FastAPI gateway skeleton for paid, bounded bot requests: quote -> checkout -> mock payment -> signed receipt/grant -> consume -> fulfillment.

**v0 uses mock/scaffold payment rails only.** Stripe webhook handling verifies deterministic test signatures only when `PAYJENT_STRIPE_WEBHOOK_SECRET` is configured; unconfigured Stripe webhooks are rejected and do not mark sessions paid. Payjent does not create Stripe Checkout sessions or call live Stripe APIs. Crypto support is a dev/operator manual `mark-paid` placeholder only; there is no wallet monitoring, on-chain confirmation, custody, or live crypto settlement.

## Local setup

```bash
python -m venv .venv
. .venv/bin/activate
pip install -e '.[test]'
cp .env.example .env
python -m pytest -q
python -m payjent.demo seed
uvicorn payjent.main:app --reload
```

By default the service uses SQLite at `sqlite:///./payjent.db`, dev mode enabled, and a development signing secret from `.env.example`. Do not use the example secret in production.

For a one-command local API exercise after seeding, export the keys printed by `python -m payjent.demo seed` and run:

```bash
python -m payjent.demo run-flow
```

If you already have a server running, target it explicitly:

```bash
PAYJENT_BASE_URL=http://127.0.0.1:8000 python -m payjent.demo run-flow
```

## Browser demo

Run the server, seed demo credentials, then create a quote and checkout via the API (or let `run-flow` do that for you). The checkout response includes `checkout_url` like `/pay/{payment_session_id}`. Open it in a browser:

```text
http://127.0.0.1:8000/pay/{payment_session_id}
http://127.0.0.1:8000/status/{payment_session_id}
```

The `/pay/...` page shows quote amount, breakdown, request summary, and current payment state. In dev mode only, it also displays an authenticated `curl` example for the operator-only mock payment API. The public browser page is read-only and does not issue grants to unauthenticated users.

## Mock flow

Set an API key header for protected bot/operator API calls. Bot credentials are scoped to their `bot_id`; only operator/admin credentials can act across bots.

Create local credentials with the demo seeder (prints plaintext keys once; store them in your shell):

```bash
python -m payjent.demo seed
```

Then export the printed values:

```bash
export PAYJENT_BOT_KEY='test-bot-key'
export PAYJENT_OPERATOR_KEY='test-operator-key'
```

To run the full local flow without copying each curl command below:

```bash
python -m payjent.demo run-flow
```

Create a quote:

```bash
curl -s -X POST http://127.0.0.1:8000/api/v1/quotes \
  -H 'content-type: application/json' \
  -H "X-Payjent-Bot-Key: <bot-key>" \
  -d '{
    "bot_id":"discord-bot-1",
    "external_user_id":"user-123",
    "request_summary":"Summarize a PDF",
    "request_hash":"reqhash123",
    "amount_minor":500,
    "currency":"USD",
    "cost_breakdown":[{"label":"analysis","amount_minor":500}],
    "execution_envelope":{"tool":"summarizer","max_pages":10}
  }'
```

Create checkout:

```bash
curl -s -X POST http://127.0.0.1:8000/api/v1/quotes/{quote_id}/checkout \
  -H "X-Payjent-Bot-Key: <bot-key>" \
  -H 'Idempotency-Key: demo-request-1'
```

Mock-pay the session (operator credential required):

```bash
curl -s -X POST http://127.0.0.1:8000/api/v1/payment-sessions/{session_id}/mock-pay \
  -H "X-Payjent-Bot-Key: <operator-key>"
```

Scaffold rails for local/dev testing only:

- `POST /api/v1/webhooks/stripe` accepts Stripe-shaped webhook events only when `PAYJENT_STRIPE_WEBHOOK_SECRET` is configured. Requests must include a valid `Stripe-Signature` HMAC header; if the secret is unset, Payjent returns `503` and does not mark anything paid. No live Stripe API calls are made.
- `POST /api/v1/payment-sessions/{session_id}/crypto/mark-paid` is an operator-only dev placeholder that marks a session paid through the same receipt/grant issuance path; it is not crypto settlement.

Verify and consume grant:

```bash
curl -s -X POST http://127.0.0.1:8000/api/v1/grants/{grant_id}/verify \
  -H 'content-type: application/json' \
  -H "X-Payjent-Bot-Key: <bot-key>" \
  -d '{"bot_id":"discord-bot-1","external_user_id":"user-123","request_hash":"reqhash123"}'

curl -s -X POST http://127.0.0.1:8000/api/v1/grants/{grant_id}/consume \
  -H 'content-type: application/json' \
  -H "X-Payjent-Bot-Key: <bot-key>" \
  -d '{"bot_id":"discord-bot-1","external_user_id":"user-123","request_hash":"reqhash123"}'
```

Record fulfillment:

```bash
curl -s -X POST http://127.0.0.1:8000/api/v1/quotes/{quote_id}/fulfillment \
  -H 'content-type: application/json' \
  -H "X-Payjent-Bot-Key: <bot-key>" \
  -d '{"status":"fulfilled","metadata":{"message_id":"abc"}}'
```

## Python SDK helper

The lightweight helper in `payjent.sdk` wraps the bot-facing API calls:

```python
from payjent.sdk import PayjentClient

client = PayjentClient("http://127.0.0.1:8000", api_key="test-bot-key")
quote = client.create_quote(
    bot_id="discord-bot-1",
    external_user_id="user-123",
    request_summary="Summarize a PDF",
    request_hash="reqhash123",
    amount_minor=500,
    currency="USD",
    cost_breakdown=[{"label": "analysis", "amount_minor": 500}],
    execution_envelope={"tool": "summarizer", "max_pages": 10},
)
checkout = client.create_checkout(quote["id"], idempotency_key="demo-1")
print(f"Open /pay/{checkout['id']} in a browser")

presentation = {"bot_id": quote["bot_id"], "external_user_id": quote["external_user_id"], "request_hash": quote["request_hash"]}
# Once a payment rail issues a grant_id:
# client.verify_grant(grant_id, **presentation)
# client.consume_grant(grant_id, **presentation)
# client.record_fulfillment(quote["id"], "fulfilled", {"message_id": "abc"})
```

See `examples/discord_bot_flow.py` for a Discord-style dry flow that requires no Discord credentials.
