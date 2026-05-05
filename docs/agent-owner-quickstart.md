# Agent Owner Quickstart: add Payjent in 10 minutes

Payjent is a generic paid-agent-action layer for **any agent owner**. It is not C3PO-specific and it is not a generic API marketplace. Your agent uses Payjent to create a payment gate, show the user a hosted Payjent payment prompt, poll Payjent with bot authentication, and then resume the exact stored action after payment.

For pay.sh-backed premium data, Payjent only gates the payment and returns a pay.sh execution envelope/`command_preview`. Your agent runtime executes and settles pay.sh externally after Payjent says the action is paid.

## 1. Configure your agent

Use real values only in your secret manager or untracked local `.env`. Do not paste public user tokens into chat; the default flow has no public token paste step.

```bash
PAYJENT_BASE_URL="https://payjent.example.com"
PAYJENT_BOT_ID="agent_<your_agent_id>"
PAYJENT_BOT_KEY="payjent_<redacted_bot_key>"

# Local/demo only, for Payjent mock payment while testing without Stripe/webhooks.
# Do not ship this to public agent runtime paths.
PAYJENT_OPERATOR_KEY="payjent_<redacted_operator_key>"
```

For local smoke testing without a running server or secrets, Payjent's demo command seeds temporary credentials in an isolated SQLite database automatically.

## 2. Install and create the bridge

```bash
pip install -e .
```

```python
import os

from payjent.agent_bridge import AgentPayjentBridge, JsonFilePendingPremiumRequestStore
from payjent.sdk import PayjentClient

payjent_client = PayjentClient(
    os.environ["PAYJENT_BASE_URL"],
    api_key=os.environ["PAYJENT_BOT_KEY"],
)

bridge = AgentPayjentBridge(
    payjent_client,
    bot_id=os.environ["PAYJENT_BOT_ID"],
    store=JsonFilePendingPremiumRequestStore("./state/payjent-pending.json"),
)
```

## 3. Request premium pay.sh data and send the payment prompt

```python
def handle_agent_request(user_id: str, city: str):
    pending, payment_message = bridge.request_pay_sh_data(
        agent_user_id=user_id,
        request_summary=f"Premium pay.sh forecast for {city}",
        amount_minor=800,
        currency="USD",
        cost_breakdown=[{"label": "Premium pay.sh data", "amount_minor": 800}],
        service_url="https://api.weather.ai/forecast",
        method="POST",
        body={"city": city, "units": "metric"},
        description="Premium weather data via external pay.sh runtime",
    )

    # Send this to the user. It contains a Payjent checkout link and action id,
    # not a grant id or payment token.
    send_message(user_id, pending.payment_message or payment_message)

    # Keep pending.action_id so your agent can poll/resume later.
    return pending.action_id
```

Public users never paste grant ids or payment tokens in the default flow. They only open the Payjent payment link. Your agent polls Payjent using `PAYJENT_BOT_KEY` and Payjent returns an unconsumed token to the bot only after payment is complete.

## 4. Poll with bot auth, resume, execute pay.sh externally, and fulfill

```python
def resume_if_paid(user_id: str, pending_id: str):
    status = bridge.check_payment(pending_id)
    if not status.get("payment_token"):
        return "Still awaiting payment."

    resumed = bridge.resume_when_paid(
        pending_id=pending_id,
        agent_user_id=user_id,
        timeout_seconds=0,
    )
    if resumed.get("status") == "awaiting_payment":
        return "Still awaiting payment."

    pay_sh_envelope = resumed["execution_envelope"]

    # Payjent stops here. Your integrating agent runtime executes/settles pay.sh.
    # Example placeholder only:
    result = agent_pay_sh_runtime.execute(pay_sh_envelope)

    bridge.mark_fulfilled(
        pending_id,
        "fulfilled",
        {"result_id": result.id, "executed_by": "external_agent_pay_sh_runtime"},
    )
    return result.text
```

Security invariants:

- The payment prompt is public-safe: checkout URL + action id, no grant/payment token.
- Public Payjent pages do not expose grant ids/payment tokens.
- The agent uses bot-authenticated polling (`check_payment` / `resume_when_paid`) to discover readiness.
- The resumed work uses Payjent's stored envelope, not fresh post-payment user text.
- Payjent does not claim live pay.sh settlement; your agent's pay.sh runtime handles execution/settlement externally.

## 5. Polling vs webhook resume

Polling is the safest default: your agent calls `check_payment` / `resume_when_paid` with bot auth until Payjent reports readiness.

For production runtimes that can receive HTTPS callbacks, pass a `callback_url` when creating the action:

```python
pending, payment_message = bridge.request_pay_sh_data(
    agent_user_id=user_id,
    request_summary=f"Premium pay.sh forecast for {city}",
    amount_minor=800,
    cost_breakdown=[{"label": "Premium pay.sh data", "amount_minor": 800}],
    service_url="https://api.weather.ai/forecast",
    method="POST",
    body={"city": city, "units": "metric"},
    callback_url="https://your-agent.example.com/payjent/callback",
)
```

When payment becomes ready, Payjent sends a signed webhook with:

- `X-Payjent-Timestamp`
- `X-Payjent-Signature`

The payload identifies the paid action and payment session but intentionally does **not** include a grant id or `payment_token`. Your callback handler verifies the signature, then calls `resume_when_paid` with bot auth:

```python
from payjent.sdk import verify_agent_action_webhook

def payjent_callback(headers: dict[str, str], payload: dict):
    ok = verify_agent_action_webhook(
        payload,
        headers["X-Payjent-Timestamp"],
        headers["X-Payjent-Signature"],
        webhook_secret=os.environ["PAYJENT_WEBHOOK_SECRET"],
    )
    if not ok:
        raise PermissionError("invalid Payjent webhook signature")

    resumed = bridge.resume_when_paid(
        pending_id=payload["action_id"],
        agent_user_id=payload["external_user_id"],
        timeout_seconds=0,
    )
    pay_sh_envelope = resumed["execution_envelope"]
    result = agent_pay_sh_runtime.execute(pay_sh_envelope)
    bridge.mark_fulfilled(payload["action_id"], "fulfilled", {"result_id": result.id})
```

Use Payjent's configured signing secret, or a dedicated webhook secret in your own agent runtime if you proxy/rotate verification separately; local demos use Payjent's development signing secret so the flow works without extra credentials.

## 6. Run the owner smokes

Local safe smokes (temporary SQLite credentials, no network, no pay.sh execution):

```bash
python -m payjent.demo agent-owner-quickstart
python -m payjent.demo agent-webhook-resume
python -m payjent.demo hosted-agent-webhook-smoke --in-process
# or, after installing the package:
payjent agent-owner-quickstart
payjent agent-webhook-resume
payjent hosted-agent-webhook-smoke --in-process
```

Hosted/base-URL smoke against a running Payjent deployment:

```bash
export PAYJENT_BASE_URL="https://payjent.vercel.app"   # or your staging Payjent URL
export PAYJENT_BOT_ID="agent_<your_agent_id>"
export PAYJENT_BOT_KEY="payjent_<redacted_bot_key>"
export PAYJENT_OPERATOR_KEY="payjent_<redacted_operator_key>"  # test/staging operator only

# Optional: public HTTPS callback receiver that records/verifies the signed webhook.
export PAYJENT_CALLBACK_URL="https://your-agent.example.com/payjent/callback"

python -m payjent.demo hosted-agent-webhook-smoke
# or explicitly:
python -m payjent.demo hosted-agent-webhook-smoke --base-url "$PAYJENT_BASE_URL" --callback-url "$PAYJENT_CALLBACK_URL"
```

The polling smoke proves: create premium pay.sh action, generate payment link/message, unpaid poll with no token, local/test mock payment, bot-auth poll discovers readiness, resume request, inspect the pay.sh command preview, and mark fulfilled. Output redacts any grant/payment token as `grant_...`.

The webhook smoke proves: register `callback_url`, receive a signed callback after local/test payment, verify the signature, confirm the callback payload contains no grant/payment token, then resume with bot auth and mark fulfilled.

The hosted smoke proves the same API flow against `PAYJENT_BASE_URL`: create generic pay.sh premium action, require a payment link, perform operator-auth dev/test mock-pay, optionally validate a webhook when an observable test receiver is available, use bot-auth `resume_when_paid`, and mark fulfilled. If no callback URL is supplied, it prints `callback_mode=not_provided` and an explicit skip reason; if a callback URL is supplied to a real hosted deployment, inspect that receiver's logs for delivery/signature validation. If hosted production disables mock-pay, the command fails instead of pretending success; use a staging/test deployment or a future real test checkout/webhook rail.

Security notes for all smokes: output redacts grant ids/payment tokens, public pages/prompts must not expose them, mock-pay is an operator-auth dev/test rail only, and Payjent still does not execute or settle pay.sh.
