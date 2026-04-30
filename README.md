# Payjent

Payjent v0 is a small FastAPI gateway skeleton for paid, bounded bot requests: quote -> checkout -> mock payment -> signed receipt/grant -> consume -> fulfillment.

**v0 defaults to mock/local payment rails.** Stripe Checkout is available only when explicitly configured (`PAYJENT_CHECKOUT_PROVIDER=stripe` or `X-Payjent-Provider: stripe`) and the optional Stripe SDK extra is installed for live calls. Link is available as an experimental one-time credential rail (`X-Payjent-Provider: link`) for downstream agent-mediated merchant purchases, not as Payjent settlement. Checkout creation never marks a session paid; Stripe receipt/grant issuance happens only after a verified webhook. Crypto support is a dev/operator manual `mark-paid` placeholder only; there is no wallet monitoring, on-chain confirmation, custody, or live crypto settlement.

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

By default the service uses SQLite at `sqlite:///./payjent.db`, `PAYJENT_ENV=local`, dev mode enabled, mock payment rails enabled, and a development signing secret from `.env.example`. Do not use the example secret in production.

Payjent is pre-live/disposable-DB today: there is intentionally no Alembic/migration layer yet. For local/dev only, reset all SQLModel tables with:

```bash
python -m payjent.demo reset-db
```

That command drops and recreates the configured database schema. It refuses to run when `PAYJENT_ENV=production` unless `PAYJENT_ALLOW_UNSAFE_DB_RESET=true` is also set for an intentional pre-live reset.

For a one-command local API exercise after seeding, export the keys printed by `python -m payjent.demo seed` and run:

```bash
python -m payjent.demo run-flow
```

To demo the first end-to-end local agent UX with no env keys, running server, Discord token, Stripe, Link, or network access, run:

```bash
python -m payjent.demo agent-prompt
```

The agent-prompt demo creates a PayjentBotGate pending request from a simulated user ask, prints a user-facing payment prompt with checkout URL, pending id, price, and paid work description, blocks unpaid execution, performs a dev/operator-only mock payment, verifies/consumes the issued grant, resumes only the stored execution envelope, records fulfillment, and prints the final fake agent result. By default it uses an isolated temporary SQLite database unless `PAYJENT_DATABASE_URL` is explicitly set, so stale local `payjent.db` files do not break the demo. This is the local UX slice; live Stripe settlement and hosted Discord/agent integration remain the next production slice.

To demo the Discord-style spend aggregation slice with no env keys, running server, Discord token, Stripe keys, x402 network, or wallet, run:

```bash
python -m payjent.demo discord-aggregator
```

This proves the control-plane thesis: **one user payment/approval -> bounded Payjent grant -> resumed paid request -> downstream x402 paid call + Stripe funding rail in one agent command**. The demo simulates `/research-with-paid-tools topic=...`, prints one Discord-style `PAYMENT_PROMPT` with a total USD 9.00 budget and checkout URL, completes the user funding step with a dev/operator mock-pay, verifies/consumes the grant by resuming the stored request, then authorizes and captures a USD 2.50 local fake x402 premium-tool spend against the same consumed grant and records fulfillment with the ledger summary. The “Stripe” line is intentionally a **mock Stripe funding placeholder** for a future Stripe test-mode hosted checkout/webhook path; the default command uses local mock-pay and does not create a live Stripe charge. The x402 call is a deterministic fake local ledger capture; no live x402 settlement, external network call, or wallet is used.

To demo a stronger, still credential-safe Stripe test-mode smoke for the same Discord-style aggregator UX, run:

```bash
python -m payjent.demo discord-aggregator-stripe-smoke
```

This opt-in smoke calls the real Payjent `/api/v1/quotes/{quote_id}/checkout` path with `X-Payjent-Provider: stripe` and test-looking local settings (`sk_test_demo`, `whsec_demo`, and a public base URL), but dependency-injects a fake Stripe checkout adapter that returns `cs_test_discord_aggregator` and `https://checkout.stripe.test/discord-aggregator`. It then posts a correctly HMAC-signed synthetic `checkout.session.completed` event to `/api/v1/webhooks/stripe`, proves receipt/grant issuance through the Stripe webhook path, consumes the grant, records a local fake x402 capture, and fulfills the request. Output includes `stripe_test_webhook_simulated=True` and `live_stripe_charge=False`; no Stripe SDK call, network call, wallet, or live charge is performed.

For a manual real Stripe test-mode check, run Payjent behind an HTTPS/tunnel URL, install `payjent[stripe]`, set `PAYJENT_STRIPE_SECRET_KEY=***`, `PAYJENT_STRIPE_WEBHOOK_SECRET=***`, and `PAYJENT_PUBLIC_BASE_URL=https://<your-tunnel>`, configure Stripe Checkout webhooks to `https://<your-tunnel>/api/v1/webhooks/stripe`, and request checkout with `X-Payjent-Provider: stripe`. Keep this outside default tests and never commit keys.

To demo the experimental Link purchase approval boundary locally without Link auth, npm, CLI, MCP, or network access, run:

```bash
python -m payjent.demo link-purchase
```

The Link demo creates a quote, creates checkout with `X-Payjent-Provider: link`, calls `/api/v1/payment-sessions/{session_id}/link/spend-request` with a deterministic fake approval, prints the approval URL and polling hint, and explicitly leaves the Payjent session `checkout_created`/unpaid with no receipt or grant. Use `--real-link` or `PAYJENT_DEMO_REAL_LINK=true` only for an intentional real Link integration check.

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

Payment rails:

- Mock/local remains the default in local/dev. `POST /api/v1/quotes/{quote_id}/checkout` returns a local `/pay/{payment_session_id}` URL unless Stripe is requested. Mock completion endpoints are disabled in production even if dev flags are accidentally left enabled.
- Stripe Checkout: install `pip install -e '.[stripe]'`, set `PAYJENT_CHECKOUT_PROVIDER=stripe` (or send `X-Payjent-Provider: stripe` per request), `PAYJENT_STRIPE_SECRET_KEY`, `PAYJENT_PUBLIC_BASE_URL`, and `PAYJENT_STRIPE_WEBHOOK_SECRET`. Checkout sessions are created with quote amount/currency/summary metadata and an idempotency key; Payjent stores Stripe's Checkout Session id and hosted URL.
- Stripe webhook URL: configure Stripe to send Checkout events to `https://<your-payjent-host>/api/v1/webhooks/stripe`.
- Test/live caveat: automated tests monkeypatch/fake the Stripe adapter and never call live Stripe. Use Stripe test-mode credentials while testing and keep all keys out of source control.
- `POST /api/v1/webhooks/stripe` verifies `Stripe-Signature` with `PAYJENT_STRIPE_WEBHOOK_SECRET`, maps Stripe Checkout Session ids or metadata back to Payjent payment sessions, and only then issues receipts/grants. If the secret is unset, Payjent returns `503` and does not mark anything paid.
- Link one-time credential rail (experimental): create checkout with `X-Payjent-Provider: link`, then an operator calls `POST /api/v1/payment-sessions/{session_id}/link/spend-request` with an http/https `merchant_url` and an explicit `credential_type` (`card` or `bank_account`) chosen after evaluating the merchant site. Payjent does **not** infer/default to `card`. Link integration is MCP-first (`payment-methods_list`, `spend-request_create`, `spend-request_retrieve`) with `link-cli` fallback. CLI fallback runs `link-cli auth status --format json` before creating a spend request or retrieving status; if not authenticated, operators must run `link-cli auth login --client-name "Payjent"` separately and approve it out-of-band. The spend-request endpoint returns `approval_url` plus a polling command/hint; show the approval URL to the user. Operators can call `POST /api/v1/payment-sessions/{session_id}/link/poll` after a `provider_session_id` is stored to retrieve a normalized status (`pending`, `approved_not_settled`, `credential_created_not_settled`, `settled`, `failed`, or `unknown`). Approval URL creation, spend requests, approval-only states, credential creation, missing/unknown status, and current poll results are not Payjent settlement by themselves and do not mark Payjent paid, issue a receipt, or issue a grant. The poll endpoint is fail-closed and returns `settlement_mapping_required` even for parser-level explicit settled-ish values until verified terminal payment evidence is production-mapped. See `docs/link-settlement-boundary.md`.
- `POST /api/v1/payment-sessions/{session_id}/crypto/mark-paid` is an operator-only dev placeholder that marks a session paid through the same receipt/grant issuance path; it is disabled in production and is not crypto settlement.

## Direct-host production/test-mode guidance

Set `PAYJENT_ENV=production` before serving real traffic. At startup, production mode fails closed unless:

- `PAYJENT_SIGNING_SECRET` is changed from the dev default.
- `PAYJENT_PUBLIC_BASE_URL` is an `https://` URL.
- If `PAYJENT_CHECKOUT_PROVIDER=stripe`, both `PAYJENT_STRIPE_SECRET_KEY` and `PAYJENT_STRIPE_WEBHOOK_SECRET` are present.

Direct-host deployments can run with your process manager of choice, for example `uvicorn payjent.main:app --host 127.0.0.1 --port 8000` behind an HTTPS reverse proxy. Do not claim a production deployment is complete until TLS, secret management, database backups, and webhook delivery are verified for your host.

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

## Bot gate / pending request resume

`payjent.bot_adapter.PayjentBotGate` is a small integration adapter for Discord, Hermes, C3PO, or similar bots. It wraps the SDK and keeps a local pending-request record keyed to the Payjent `quote_id` and `payment_session_id`:

- bot/user context: `bot_id`, `external_user_id`, optional channel/thread/message ids
- immutable request context: `request_hash`, human summary, execution envelope
- lifecycle context: status, expiry, grant id, fulfillment id

The important safety rule is: **do not trust fresh prompt text after payment**. Quote and store the execution envelope before payment, then resume only that stored envelope after Payjent verifies and consumes the paid grant.

```python
from payjent.bot_adapter import PayjentBotGate, MemoryPendingRequestStore
from payjent.sdk import PayjentClient

client = PayjentClient("http://127.0.0.1:8000", api_key="test-bot-key")
gate = PayjentBotGate(client, MemoryPendingRequestStore())

pending = gate.quote_pending_request(
    bot_id="bot-1",
    external_user_id="discord-user-123",
    summary="Generate a research brief",
    execution_envelope={"command": "/brief", "topic": "payments"},
    amount_minor=750,
    currency="USD",
    cost_breakdown=[{"label": "brief", "amount_minor": 750}],
)
print(pending.checkout_url)

# Later, after the payment rail/webhook/operator flow gives the bot a grant id:
resume = gate.resume_paid_request(
    pending.id,
    grant_id="grant_...",
    bot_id=pending.bot_id,
    external_user_id=pending.external_user_id,
    request_hash=pending.request_hash,
)
# Execute resume["execution_envelope"], never fresh user text.
gate.record_fulfillment(pending.id, "fulfilled", {"discord_message_id": "reply-1"})
```

Run the one-command local agent prompt/resume demo (no credentials, server, Discord token, Stripe, Link, or network needed):

```bash
python -m payjent.demo agent-prompt
```

This is the first end-to-end local UX: ask agent → pay prompt → mock pay → grant consume → resume stored envelope → fulfill. The older hosted-style resume example still exists in `examples/discord_resume_flow.py` for a running API plus seeded credentials.

## Dashboard v0

Payjent now includes a built-in FastAPI dashboard/control-plane at `/dashboard` for agent developers and operators. It is intentionally server-rendered and credential-safe for local development: browser pages are read-only setup surfaces that show operator-authenticated `curl` commands instead of unauthenticated production actions.

Dashboard v0 covers:

- **Agent registration** via `POST /api/v1/agents/register` with an operator key. This creates an `AgentProfile` and returns a bot API key once. Payjent stores only the keyed hash in `BotCredential`; copy the plaintext key immediately.
- **Stripe Connect funding rail setup** via `POST /api/v1/agents/{agent_id}/stripe-connect/start` and `/complete`. In local/test mode this returns a simulated `acct_test_...` account and onboarding URL, writes a non-secret `RailConnection`, and does not call Stripe. In production it fails closed with `503` until real Connect OAuth/configuration exists.
- **x402 spend rail configuration** via `POST /api/v1/agents/{agent_id}/x402/configure`. The stored config is non-secret only: network, optional `pay_to`, optional facilitator URL, request/call caps, and enabled state. Do not place private keys, wallet seeds, Stripe secrets, or bearer tokens in `config_json`.
- **Integration snippets and state** on `/dashboard/agents/{agent_id}` including agent identity, rail statuses, a `discord-aggregator-stripe-smoke` snippet, recent quotes, and spend ledger entries.

Example local registration:

```bash
curl -X POST http://localhost:8000/api/v1/agents/register \
  -H "X-Payjent-Bot-Key: $PAYJENT_OPERATOR_KEY" \
  -H "Content-Type: application/json" \
  -d '{"name":"Hermes Research","platform":"discord","bot_id":"demo-stripe-smoke-bot","default_currency":"USD"}'
```

This maps directly to the `python -m payjent.demo discord-aggregator-stripe-smoke` flow: the dashboard agent `bot_id` and one-time bot key are the control-plane version of the in-process seeded credentials used by the smoke demo. The demo still uses safe local/mock settlement by default; live Stripe Connect OAuth and real x402 facilitator/wallet settlement are the next integration layer, not part of this v0.
