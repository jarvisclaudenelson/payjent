# Payjent

Payjent v0 is a small FastAPI gateway skeleton for paid, bounded bot requests: quote -> checkout -> mock payment -> signed receipt/grant -> consume -> fulfillment.

**v0 uses a mock payment provider only.** Stripe and crypto are future adapter rails/placeholders and are not live settlement in this version.

## Local setup

```bash
python -m venv .venv
. .venv/bin/activate
pip install -e '.[test]'
cp .env.example .env
python -m pytest -q
uvicorn payjent.main:app --reload
```

By default the service uses SQLite at `sqlite:///./payjent.db`, dev mode enabled, and a development signing secret from `.env.example`. Do not use the example secret in production.

## Mock flow

Create a quote:

```bash
curl -s -X POST http://127.0.0.1:8000/api/v1/quotes \
  -H 'content-type: application/json' \
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
curl -s -X POST http://127.0.0.1:8000/api/v1/quotes/{quote_id}/checkout
```

Mock-pay the session:

```bash
curl -s -X POST http://127.0.0.1:8000/api/v1/payment-sessions/{session_id}/mock-pay
```

Verify and consume grant:

```bash
curl -s -X POST http://127.0.0.1:8000/api/v1/grants/{grant_id}/verify \
  -H 'content-type: application/json' \
  -d '{"bot_id":"discord-bot-1","external_user_id":"user-123","request_hash":"reqhash123"}'

curl -s -X POST http://127.0.0.1:8000/api/v1/grants/{grant_id}/consume \
  -H 'content-type: application/json' \
  -d '{"bot_id":"discord-bot-1","external_user_id":"user-123","request_hash":"reqhash123"}'
```

Record fulfillment:

```bash
curl -s -X POST http://127.0.0.1:8000/api/v1/quotes/{quote_id}/fulfillment \
  -H 'content-type: application/json' \
  -d '{"status":"fulfilled","metadata":{"message_id":"abc"}}'
```
