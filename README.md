# Payjent

Payjent is an agent spend-control and payment authorization service for paid, bounded agent actions: exact quote -> hosted checkout or funded budget -> signed receipt/grant -> request-bound execution -> fulfillment/refund evidence.

Payjent is the paid action control plane for quotes, checkout, grants, runtime-priced toolbox execution records, and spend authorization; it is not the card or payment rail itself. User funding, Decal checkout, x402-style paid calls, Link one-time credentials, legacy Stripe checkout, and card credentials are downstream rails under the same bounded grant and spend ledger model.

**v0 defaults to mock/local payment rails for local development, while Decal is the primary active hosted checkout rail.** Configure production with `PAYJENT_CHECKOUT_PROVIDER=decal`, `PAYJENT_DECAL_API_KEY`, and `PAYJENT_PUBLIC_BASE_URL=https://www.payjent.com`. Stripe Checkout remains a legacy fallback only when explicitly configured (`PAYJENT_CHECKOUT_PROVIDER=stripe` or `X-Payjent-Provider: stripe`) and the optional Stripe SDK extra is installed for live calls. Link is available as an experimental one-time credential rail (`X-Payjent-Provider: link`) for downstream agent-mediated merchant purchases, not as Payjent settlement. Checkout creation never marks a session paid; Decal receipt/grant issuance happens only after Payjent verifies the Decal checkout session server-side. Crypto support is a dev/operator manual `mark-paid` placeholder only; there is no wallet monitoring, on-chain confirmation, custody, or live crypto settlement.

## Agent owner quickstart (10 minutes)

Pricing safety rule for installed agents: before creating a Payjent paid action, the agent must obtain the exact provider/merchant quoted price and send a matching `cost_breakdown`. It must not use placeholder/default/test amounts such as `$1.00` or `100` minor units. If the exact quote is unknown, the agent should not create the paid action and should tell the user Payjent is awaiting an exact provider quote.

If you are comparing Payjent with Zero-like agent discovery or paid API activation tools, see [`docs/agent-spend-control-positioning.md`](docs/agent-spend-control-positioning.md). Payjent is not a generic tool search engine; it is the spend-control/payment authorization layer for exact quotes, human-funded budgets, request-bound grants, spend ledgers, and fulfillment/refund evidence around external provider execution.

If you own an agent and want to gate a premium action, start here:

```bash
cp .env.agent.example .env.agent
python -m payjent.demo agent-owner-quickstart
python -m payjent.demo agent-webhook-resume
# after `pip install -e .`, the console script works too:
payjent agent-owner-quickstart
payjent agent-webhook-resume
```

Then follow [`docs/agent-owner-quickstart.md`](docs/agent-owner-quickstart.md). The quickstart is generic for any agent owner: your agent creates a Payjent-gated pay.sh action with `AgentPayjentBridge` + `PayjentClient`, sends the user a public-safe payment message, polls Payjent with bot auth or receives a signed callback webhook, resumes the stored envelope after payment, executes/settles pay.sh externally in your runtime, and calls `mark_fulfilled`. Public users never paste grant ids or payment tokens in the default flow, and callback payloads do not include grant/payment tokens.

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

## Durable Postgres storage

SQLite is fine for local demos, but Vercel serverless filesystems are ephemeral and are not suitable for durable Payjent state. For hosted deployments, set `PAYJENT_DATABASE_URL` to a managed, pooled Postgres connection string such as a Vercel/Supabase pooler URL. Payjent accepts provider-style `postgres://...` and driverless `postgresql://...` URLs and normalizes them internally to SQLAlchemy's psycopg3 driver form.

```bash
PAYJENT_DATABASE_URL="postgres://<user>:***@<pooler-host>:6543/<database>?sslmode=require"
```

Store the real value only in your deployment secret manager or local untracked `.env`. Treat the full database URL as a secret because it contains credentials and host details. The `/healthz` endpoint reports only a non-secret database backend label and whether `select 1` succeeds; it never returns the configured URL, host, username, or password.

Payjent is pre-live/disposable-DB today: there is intentionally no Alembic/migration layer yet. For local/dev only, reset all SQLModel tables with:

```bash
python -m payjent.demo reset-db
```

That command drops and recreates the configured database schema. It refuses to run when `PAYJENT_ENV=production` unless `PAYJENT_ALLOW_UNSAFE_DB_RESET=true` is also set for an intentional pre-live reset.

For a one-command local API exercise after seeding, export the keys printed by `python -m payjent.demo seed` and run:

```bash
python -m payjent.demo run-flow
```

To demo the first-class paid agent action API surface with no env keys, running server, Discord token, Stripe, Link, or network access, run:

```bash
python -m payjent.demo paid-action
```

This calls `/api/v1/agent-actions` to create a quote and checkout in one bot-authenticated request, prints `action_id`, `payment_url`, and a user-facing payment prompt, completes local dev mock payment, consumes the returned `payment_token` for that exact action/request hash, resumes the stored execution envelope, and records completion. MVP note: `action_id` is currently an alias for the underlying `quote_id` so the existing quote/grant request-hash binding is preserved without a migration.

## Payjent-managed FAL image generation

For normal/default FAL image generation, agents should use the toolbox tool `fal.image.generate` (`POST /api/v1/toolbox/fal.image.generate/quote`, `/checkout`, and `/executions`). This is the canonical Payjent-managed FAL route: Payjent gates the exact agent-supplied runtime quote and the managed execution adapter uses the Payjent-managed FAL provider runtime.

`paysh.fal_image` is not the default FAL path. It remains only as an advanced external pay.sh/x402 fallback. Checkout, quote, or execution creation for `paysh.fal_image` must include `external_runtime: true` in `arguments`; otherwise Payjent rejects the request with `422` guidance to use `fal.image.generate`.

## Pay.sh premium action provider

Payjent can scaffold a downstream pay.sh premium action without replacing Payjent's payment gate. A bot calls `POST /api/v1/premium-actions/pay-sh` with the usual paid action fields plus either `service_url` or `service_fqn` + `resource`, optional `method`, `body`, `headers`, and `description`. Payjent stores a normalized execution envelope with `provider=pay_sh`, `kind=premium_api_call`, `command_preview` such as `paycurl https://api.weather.ai/forecast` for generic x402 or `npx spongewallet pay fetch --url https://fal.mpp.tempo.xyz/fal-ai/fast-sdxl ...` for SpongeWallet MPP/Tempo endpoints, setup guidance, and `settlement=external_x402_runtime`. Payjent never POSTs `service_url` or runs `paycurl`/SpongeWallet; it only gates payment and authorizes bounded x402/pay.sh spend. Do not use the obsolete `paysponge/fal:...` target; discover current fal.ai catalog details with `npx spongewallet pay discover fal` and pass the executable URL `https://fal.mpp.tempo.xyz/<resource>`.

Concrete flow: create the pay.sh premium action, send the Stripe/Payjent payment URL to the Discord user, poll `/api/v1/agent-actions/{action_id}/status` until `payment_token_status=available`, consume the `payment_token` through `/api/v1/agent-actions/{action_id}/consume` or `/start`, call `/api/v1/grants/{grant_id}/spend-authorizations` with `rail=x402`, `capture=true`, and the exact action budget, then execute the returned envelope in the agent's external pay.sh/paycurl/x402 runtime and call `/api/v1/agent-actions/{action_id}/complete`. Payjent gates the paid agent action and issues the request-bound paid rail authorization; pay.sh remains the downstream premium API runtime. The local scaffold does not execute `paycurl`, require pay.sh CLI installation/secrets, resolve pay.sh skill gateways, or verify live pay.sh settlement.

```bash
python -m payjent.demo pay-sh-action
```

The demo creates a Payjent-gated pay.sh action, performs local operator mock-pay, consumes the grant, and prints the normalized `command_preview` only.

## Hook up any agent to Payjent

Any agent owner can add a Payjent payment gate around premium pay.sh work with the generic bridge in `payjent.agent_bridge`. C3PO is just one possible caller; the API surface is agent-neutral.

Give your agent these Payjent settings (store real values in your secret manager):

```bash
export PAYJENT_BASE_URL="https://www.payjent.com"
export PAYJENT_BOT_KEY="payjent_bot_key_..."
export PAYJENT_BOT_ID="my-agent"      # or export AGENT_ID="my-agent" and map it to bot_id
```

Install/import the Payjent SDK and bridge, then create one bridge during agent startup:

```python
import os
from payjent.agent_bridge import AgentPayjentBridge, JsonFilePendingPremiumRequestStore
from payjent.sdk import PayjentClient

payjent = PayjentClient(os.environ["PAYJENT_BASE_URL"], api_key=os.environ["PAYJENT_BOT_KEY"])
agent_id = os.getenv("PAYJENT_BOT_ID") or os.environ["AGENT_ID"]
bridge = AgentPayjentBridge(
    payjent,
    bot_id=agent_id,
    store=JsonFilePendingPremiumRequestStore("./state/payjent-agent-pending.json"),
)
```

Agent ask handler pseudo-code:

```python
def on_agent_ask(user_id: str, ask: str):
    pending, payment_message = bridge.request_pay_sh_data(
        agent_user_id=user_id,
        request_summary="Premium pay.sh forecast for Lisbon",
        amount_minor=800,
        service_url="https://api.weather.ai/forecast",
        method="POST",
        body={"city": "Lisbon", "units": "metric"},
        description="premium weather data",
    )
    post_to_user(user_id, payment_message)
    # Persist pending.action_id if your chat/runtime needs to correlate callbacks.
```

The default payment prompt text is user-facing and looks like:

```text
Payment required for premium pay.sh data: Premium pay.sh forecast for Lisbon
Pay here: https://www.payjent.com/pay/ps_...
Action: q_...
After payment, your agent can poll Payjent and resume this stored request automatically.
```

Resume handler pseudo-code (recommended polling/status flow; no community user token paste required):

```python
def on_later_check(user_id: str, pending_id: str):
    resumed = bridge.resume_when_paid(
        pending_id=pending_id,
        agent_user_id=user_id,
        timeout_seconds=0,  # single check; use >0 with poll_interval for short polling
    )
    if resumed.get("status") == "awaiting_payment":
        return  # keep waiting, or ask the user to finish checkout
    envelope = resumed["execution_envelope"]
    # Your agent executes this externally in its pay.sh/paycurl runtime; Payjent never runs it.
    result = agent_pay_sh_runtime.execute(envelope)
    bridge.mark_fulfilled(pending_id, "fulfilled", {"result_id": result.id})
    post_to_user(user_id, result.text)
```

`resume_when_paid` calls the bot-authenticated agent action status endpoint, discovers the unconsumed `payment_token` only after payment, then consumes it with the stored presentation. The manual `resume_after_payment(..., payment_token=...)` path remains for custom integrations. Production runtimes can also pass `callback_url` so Payjent sends a signed, token-free `agent_action.ready` webhook; the callback handler should verify the signature and then call `resume_when_paid` with bot auth.

For a no-network local walkthrough, run:

```bash
python -m payjent.demo agent-pay-sh-poll
```

For the hosted/base-URL agent-owner smoke rail, first bootstrap staging/test credentials from an authenticated operator-only action, then run the smoke against a running Payjent API:

```bash
export PAYJENT_BASE_URL="https://www.payjent.com"
export PAYJENT_BOOTSTRAP_TOKEN="$PAYJENT_OPERATOR_PROVIDED_VALUE"
export PAYJENT_BOT_ID="agent-smoke-1"
# Optional public HTTPS receiver for signed, token-free callbacks:
export PAYJENT_CALLBACK_URL="https://your-agent.example.com/payjent/callback"

# Prints shell exports containing one-time plaintext keys; store them securely.
python -m payjent.demo hosted-smoke-bootstrap

python -m payjent.demo hosted-agent-webhook-smoke
```

The hosted bootstrap endpoint is disabled unless the server is configured with `PAYJENT_BOOTSTRAP_TOKEN`; there is no default/dev token. It accepts the `X-Payjent-Bootstrap-Token` header or a Bearer authorization value, compares the token in constant time, creates/reuses the agent profile for the requested `bot_id`, and mints new bot/operator credentials every call because Payjent stores only key hashes and cannot recover old plaintext. For convenience, `python -m payjent.demo hosted-smoke-bootstrap --run-smoke` immediately runs the smoke with the returned keys without printing raw keys unless `--print-exports` is also passed.

A production app can avoid flaky local CLI network paths by asking the server to run the bounded protected status smoke itself:

```bash
curl -X POST "$PAYJENT_BASE_URL/api/v1/smoke/agent-webhook" \
  -H "X-Payjent-Bootstrap-Token: $PAYJENT_BOOTSTRAP_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"bot_id":"agent-smoke-1"}'
```

`POST /api/v1/smoke/agent-webhook` is also disabled by default and uses the same bootstrap token protection. It creates fresh hashed bot/operator credentials only for request-scope internal auth, creates a generic pay.sh premium action, verifies the unpaid/paid poll states, uses only the explicit operator mock/test rail for internal settlement, consumes the redacted payment token internally, records `fulfilled`, and returns a redacted JSON artifact. The response labels `provider=pay_sh`, `settlement=external_pay_sh_runtime`, and `operator_mock_pay=test_rail_only`; it does **not** return Payjent API keys, raw `payment_token`, grant IDs, or callback fields containing payment/grant tokens. It never executes or settles pay.sh. If the hosted environment disables mock-pay, the endpoint fails with an explicit test-rail-unavailable error rather than weakening public mock-pay guardrails.

For a safe local fallback that uses TestClient, temporary credentials, and an in-process callback capture, run `python -m payjent.demo hosted-agent-webhook-smoke --in-process` or set `PAYJENT_BOOTSTRAP_TOKEN` to any test value and run `python -m payjent.demo hosted-smoke-bootstrap --in-process --run-smoke`. The hosted smoke creates a generic pay.sh premium action, verifies a payment link exists, uses operator-auth dev/test mock-pay, validates a webhook when an observable test receiver is available (otherwise prints an explicit callback skip reason), resumes with bot auth, and marks fulfilled. It redacts grant/payment tokens and never executes or settles pay.sh. If the hosted environment disables mock-pay, the command fails with an actionable staging/test-rail message rather than pretending success.

The older `python -m payjent.demo c3po-pay-sh` command remains as a compatibility alias. Caveat: Payjent gates payment and returns a stored pay.sh execution envelope/`command_preview`; pay.sh execution and settlement happen in the integrating agent runtime. This scaffold does not verify live pay.sh settlement or execute `paycurl`.

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

## Dashboard account auth

The developer dashboard at `/dashboard` and `/dashboard/agents/{agent_id}` requires a Payjent dashboard account session in both local/dev and production. Browser sessions are HTTP-only signed cookies derived from `PAYJENT_SIGNING_SECRET` (`Secure` in production). Use `POST /auth/logout`, `GET /auth/logout`, or the dashboard logout button to clear the local Payjent session.

First-party auth remains available as a fallback: visit `/auth/register` to create the first account, or `/auth/login` for an existing account. Passwords are stored as PBKDF2-HMAC-SHA256 hashes with per-user salts.

### WorkOS AuthKit dashboard login

Payjent can optionally use WorkOS AuthKit hosted login for the dashboard. Configure all values with the existing `PAYJENT_` env prefix:

```bash
PAYJENT_WORKOS_API_KEY=<set in your secret manager>
PAYJENT_WORKOS_CLIENT_ID=<your WorkOS client id>
PAYJENT_PUBLIC_BASE_URL=https://www.payjent.com
# Optional override if it differs from PUBLIC_BASE_URL + /auth/workos/callback:
PAYJENT_WORKOS_REDIRECT_URI=https://www.payjent.com/auth/workos/callback
```

In the WorkOS Dashboard, add this redirect URI to the AuthKit application:

```text
https://www.payjent.com/auth/workos/callback
```

When `PAYJENT_WORKOS_API_KEY` and `PAYJENT_WORKOS_CLIENT_ID` are configured, `/auth/register` and `/auth/login` show a **Sign in with WorkOS AuthKit** CTA that redirects through `/auth/workos/login`. If WorkOS is not configured, the WorkOS route fails closed with `503` and the first-party Payjent account form remains usable for local/dev and deployed smoke checks. Do not commit WorkOS API keys or paste them into chat/logs; tests use fakes and never call WorkOS.

This auth layer protects dashboard pages only. Operator/bot API routes remain protected by API credentials exactly as before. For production, set a strong non-default `PAYJENT_SIGNING_SECRET` and HTTPS `PAYJENT_PUBLIC_BASE_URL`. The current Vercel/serverless SQLite setup is ephemeral, so dashboard accounts are acceptable for demos only until durable database storage/migrations are added.

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

Payjent treats rails as replaceable downstream categories underneath the same paid grant and spend authorization flow. New spend authorization ledger entries must use a supported canonical rail: `stripe_funding`, `x402_payment`, `link_credential`, or `card_credential`. For API compatibility, `stripe` normalizes to `stripe_funding` and `x402` normalizes to `x402_payment`; unsupported new rails are rejected with `422` before a spend ledger row is written. Existing historical/free-form ledger data is not migrated.

`card_credential` means a downstream card-credential rail category: for example, a MoonAgents-style agent card could sit under Payjent's controls as the credential used at a merchant. This repository does not integrate MoonPay or MoonAgents, does not issue cards, and does not claim support for MoonAgents Card.

- Mock/local remains the default in local/dev. `POST /api/v1/quotes/{quote_id}/checkout` returns a local `/pay/{payment_session_id}` URL unless Decal, Stripe, or another provider is requested. Mock completion endpoints are disabled in production even if dev flags are accidentally left enabled.
- Decal Checkout is the primary active hosted checkout rail: set `PAYJENT_CHECKOUT_PROVIDER=decal`, `PAYJENT_DECAL_API_KEY`, and `PAYJENT_PUBLIC_BASE_URL=https://www.payjent.com`. Optional Decal settings include `PAYJENT_DECAL_PAYMENT_DESTINATION`, `PAYJENT_DECAL_SUCCESS_URL_TEMPLATE`, and `PAYJENT_DECAL_CALLBACK_URL_TEMPLATE`. Payjent creates Decal checkout sessions, stores Decal's session id and hosted URL, receives callbacks at `/api/v1/webhooks/decal`, then verifies the Decal session with Decal's API before issuing receipts/grants.
- Decal feature gaps to track before deleting legacy rails entirely: automatic paid refunds, disputes/chargebacks lifecycle, documented webhook signature/retry semantics, and missed-webhook reconciliation/polling. See `docs/decal-checkout.md`.
- Stripe Checkout remains a legacy fallback: install `pip install -e '.[stripe]'`, set `PAYJENT_CHECKOUT_PROVIDER=stripe` (or send `X-Payjent-Provider: stripe` per request), `PAYJENT_STRIPE_SECRET_KEY`, `PAYJENT_PUBLIC_BASE_URL`, and `PAYJENT_STRIPE_WEBHOOK_SECRET`. Checkout sessions are created with quote amount/currency/summary metadata and an idempotency key; Payjent stores Stripe's Checkout Session id and hosted URL.
- Stripe webhook URL: configure Stripe to send Checkout events to `https://<your-payjent-host>/api/v1/webhooks/stripe`.
- Test/live caveat: automated tests monkeypatch/fake Decal and Stripe adapters and never call live payment rails. Use provider test-mode credentials while testing and keep all keys out of source control.
- `POST /api/v1/webhooks/stripe` verifies `Stripe-Signature` with `PAYJENT_STRIPE_WEBHOOK_SECRET`, maps Stripe Checkout Session ids or metadata back to Payjent payment sessions, and only then issues receipts/grants. If the secret is unset, Payjent returns `503` and does not mark anything paid.
- Link one-time credential rail (experimental): create checkout with `X-Payjent-Provider: link`, then an operator calls `POST /api/v1/payment-sessions/{session_id}/link/spend-request` with an http/https `merchant_url` and an explicit `credential_type` (`card` or `bank_account`) chosen after evaluating the merchant site. Payjent does **not** infer/default to `card`. Link integration is MCP-first (`payment-methods_list`, `spend-request_create`, `spend-request_retrieve`) with `link-cli` fallback. CLI fallback runs `link-cli auth status --format json` before creating a spend request or retrieving status; if not authenticated, operators must run `link-cli auth login --client-name "Payjent"` separately and approve it out-of-band. The spend-request endpoint returns `approval_url` plus a polling command/hint; show the approval URL to the user. Operators can call `POST /api/v1/payment-sessions/{session_id}/link/poll` after a `provider_session_id` is stored to retrieve a normalized status (`pending`, `approved_not_settled`, `credential_created_not_settled`, `settled`, `failed`, or `unknown`). Approval URL creation, spend requests, approval-only states, credential creation, missing/unknown status, and current poll results are not Payjent settlement by themselves and do not mark Payjent paid, issue a receipt, or issue a grant. The poll endpoint is fail-closed and returns `settlement_mapping_required` even for parser-level explicit settled-ish values until verified terminal payment evidence is production-mapped. See `docs/link-settlement-boundary.md`.
- `POST /api/v1/payment-sessions/{session_id}/crypto/mark-paid` is an operator-only dev placeholder that marks a session paid through the same receipt/grant issuance path; it is disabled in production and is not crypto settlement.

## Direct-host production/test-mode guidance

Set `PAYJENT_ENV=production` before serving real traffic. At startup, production mode fails closed unless:

- `PAYJENT_SIGNING_SECRET` is changed from the dev default.
- `PAYJENT_PUBLIC_BASE_URL` is an `https://` URL.
- If `PAYJENT_CHECKOUT_PROVIDER=decal`, `PAYJENT_DECAL_API_KEY` is present.
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

Payjent now includes a built-in FastAPI dashboard/control-plane at `/dashboard` for agent developers and operators. Dashboard v0 is a local/dev UI: it is intentionally server-rendered and credential-safe for local development, with browser pages as read-only setup surfaces that show operator-authenticated `curl` commands instead of unauthenticated production actions. In production mode, `/dashboard` and `/dashboard/agents/{agent_id}` fail closed with `403` until proper browser UI authentication is added; operator APIs remain credential-protected with bot/operator keys.

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
