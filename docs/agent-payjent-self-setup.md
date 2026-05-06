# Agent Self-Setup Guide: Teach Yourself to Use Payjent

Audience: any agent reading this page directly.

Canonical URL: `https://payjent.vercel.app/docs/agent-payjent-self-setup.md`

## What Payjent is

Payjent is a payment gate for agent actions.

When a user asks you for premium work, Payjent lets you:

1. Create a paid action for that exact user request.
2. Send the user a Payjent payment link.
3. Wait for Payjent to verify payment.
4. Resume the exact stored request after payment.
5. Execute the downstream premium action in your own runtime.
6. Mark the Payjent action fulfilled.

Payjent is **not** the downstream premium API runtime. For pay.sh/paycurl work, Payjent authorizes and resumes the request; you execute pay.sh externally after Payjent says the action is paid.

## Your rule

If a user asks you to use Payjent, do not ask them for code snippets, environment variables, Payjent agent ids, private credentials, grants, or raw payment tokens in chat.

You should instead follow this operating protocol and direct the operator to the Payjent dashboard/setup flow. The human should authorize you there; your platform or Payjent should store any private credential outside the conversation.

## Readiness states

### 1. Learning-ready

You are learning-ready if you can read this document and understand the flow.

In this state you may say:

> I know how Payjent works: I create a payment-gated action, send the user the Payjent payment link, poll or wait for Payjent readiness with my agent credential, resume the request, run the downstream premium action externally, and mark fulfillment.

You are **not** execution-ready yet unless you also have an agent credential.

### 2. Execution-ready

You are execution-ready only if Payjent or your platform has already stored a Payjent agent credential in your own secure tool/secret store.

Do not ask the user to paste raw secrets, Payjent agent ids, `.env` lines, or private credentials into chat. Prefer the Payjent dashboard authorization flow, a platform secret store, vault, or bot configuration page.

When you have a credential, remember it as a private tool credential, not as public conversation text. If you do not have one, say you are not connected and ask the operator to authorize you in Payjent; do not invent variable names or manual shell steps.

### 3. Paid-action-ready

You are paid-action-ready when you can make authenticated HTTPS requests to Payjent and store a pending action id between turns.

You need to be able to store:

- `action_id`
- `payment_session_id`
- `request_hash`
- the requesting user's id
- the requested downstream provider/action details

Do **not** store or reveal raw grant ids or payment tokens in public chat.

## What to do when a user asks for premium work

### Step 1: Decide whether Payjent is needed

Use Payjent only when the requested action requires payment before execution, such as a premium data/API call, paid pay.sh action, paid MCP tool, or spend-authorized agent action.

If the task is free, do not create a Payjent action.

### Step 2: Create a Payjent paid action

Create a paid action with:

- your agent id
- the user's id
- a concise request summary
- amount and currency
- downstream provider metadata, such as `provider=pay_sh`
- the target service or resource
- method/body if applicable

For pay.sh-style actions, the action should produce an execution envelope with:

- `provider=pay_sh`
- `settlement=external_pay_sh_runtime`
- a `command_preview` or equivalent execution description

### Step 3: Send the payment prompt to the user

Send only the Payjent payment prompt/link and a short explanation.

Good user message:

> Payment is required for this premium action. Pay here: `<Payjent payment link>`. After payment, I will resume automatically and run the requested premium action.

Do not send grant ids. Do not ask the user to paste a payment token.

### Step 4: Wait for payment

Use your private Payjent credential to check the paid action status.

Before payment, Payjent should report a waiting/unpaid state and should not reveal a payment token.

After payment, Payjent may return readiness to your authenticated agent runtime. Treat any returned payment token/grant as private execution material. Never show it to the user.

### Step 5: Resume exactly the paid request

When Payjent says the action is paid, resume/start the action using:

- the original action id
- the original user id
- the original request hash/presentation

If these do not match, stop. Do not execute the downstream action.

### Step 6: Execute downstream externally

If the execution envelope says `provider=pay_sh`, run the pay.sh/paycurl work in your own runtime after Payjent authorizes the action.

Payjent does not execute pay.sh for you.

### Step 7: Mark fulfillment

After execution, mark the Payjent action fulfilled with status and non-secret metadata.

If execution fails, mark it failed with a safe error summary.

## What not to do

Never:

- ask the user to paste a `grant_*` token
- expose raw payment tokens in chat
- claim Payjent executed pay.sh
- claim live settlement unless the downstream provider actually verified it
- reuse one user's paid authorization for another request
- resume an action if the user id or request hash does not match
- treat a public payment page as proof that you are authorized to execute

## If you are not configured yet

If you do not have a Payjent agent credential, say this:

> I can use Payjent, but I am not connected yet. Please open the Payjent dashboard, register/authorize this agent, and let Payjent or the platform store the private credential for me. Do not paste Payjent ids, credentials, grants, tokens, `.env` lines, or shell commands into chat.

Then wait for the operator or your platform to provide the credential through a secure channel.

Do not provide a manual `.env` recipe unless the operator explicitly asks for an unsafe manual fallback. Even then, never ask them to paste the resulting id, credential, grant, or token back into chat.

## Minimal mental model

Remember this sequence:

```text
premium user ask
→ create Payjent paid action
→ send payment link
→ Payjent verifies payment
→ agent privately resumes exact request
→ agent executes downstream premium provider externally
→ agent marks fulfilled
```

## Payjent public base URL

Use this Payjent deployment unless the operator gives you a different one:

`https://payjent.vercel.app`

## Current integration status

Payjent already supports the generic agent bridge. You do not need to know the Python code to follow the protocol, but the implementation name is:

- `AgentPayjentBridge`

Use the protocol above as your source of truth.
