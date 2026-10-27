# Scout Pay

**An AI relocation agent that plans your move and pays for it with PayPal (via Braintree).**

Built for the [PayPal AI Hackathon](https://paypalaihackathon.devpost.com/). Scout Pay asks where you're moving and your budget, an LLM turns that into a move plan with the payments it needs (mover deposit, rental deposit, utility setup), and you pay each one through Braintree Drop-in, which shows the **PayPal button** alongside cards. Payments above a threshold stop for a **human approval** step before the agent can charge anything.

> **Hackathon note:** this repository was created and built during the hackathon (after Oct 1, 2026). It's a new codebase inspired by my earlier Relocation Scout project; the payment agent, approval flow and Braintree/PayPal integration are all new.

## Features
- **Agentic chat** (`/api/chat`): the agent calls tools to search a relocation catalog, build a cart/quote, propose a payment, charge it via Braintree (card nonce, vaulted card token, or PayPal), check status, and refund/void.
- **Guardrails enforced in code, not by the LLM**
  - Proposing never charges. A proposal becomes payable only after **you confirm** (your own "confirm" message or the Confirm button); the LLM has no confirm tool.
  - Over `APPROVAL_THRESHOLD` (default $500) an **approver** must also approve (`/api/approvals`).
  - **Spending limits**: `MAX_SINGLE_PAYMENT` (default $2000) and rolling 24h `DAILY_LIMIT` (default $3000), checked at propose and at charge.
  - **Idempotency**: each charge uses key `proposal:<id>` (and `Idempotency-Key` header on `/api/checkout`), so retries never double-charge.
  - **Audit log** of every message, tool call, human decision and gateway result (`/api/audit`). Refunds only for transactions this session made.
- **PayPal degrades gracefully**: if the merchant account rejects PayPal, the agent keeps the proposal and asks you to pay by card.
- **LLM**: AWS Bedrock Converse tool use when AWS credentials exist (`BEDROCK_MODEL_ID`, default `amazon.nova-lite-v1:0`), else a **deterministic mock planner** so the demo runs offline. `LLM_PROVIDER=mock|bedrock` forces one.
- **D1-ready persistence**: `Repo` interface with a SQLite adapter; the same schema (`migrations/0001_init.sql`) runs on Cloudflare D1. See `docs/PERSISTENCE.md`.
- Runs with **no keys at all** (mock gateway + mock planner).

## Architecture
```
browser (static/index.html): chat · Braintree Drop-in · approver panel · audit log
   │  /api/chat  /api/proposals/{id}/confirm|cancel  /api/approvals  /api/audit  /api/checkout
FastAPI (app/main.py)
   ├── app/agent.py    tools + policy (confirmation, approval, limits, idempotency, audit)
   ├── app/planner.py  Bedrock Converse tool loop  |  deterministic mock planner
   ├── app/catalog.py  relocation services catalog
   ├── app/gateway.py  BraintreeGateway (sale/vault/find/void/refund)  |  MockGateway
   └── app/store.py    Repo → SQLiteRepo (local)  /  D1 (Cloudflare, same SQL)
```
Payment lifecycle: `propose → awaiting_confirmation → (user confirms) → [awaiting_approval → approver] → ready → execute → paid → refund/void`.

## Demo script (≈2 min)
1. Open the app. Header shows gateway (braintree), LLM, approval limit and daily limit.
2. Chat: **"I need movers, a visa fee and internet"** → agent searches the catalog and builds a $330 quote (tool calls shown inline).
3. In *Payment method* pick a card (sandbox `4111 1111 1111 1111`, any future expiry) → *Use this payment method*.
4. Chat **"pay"** → proposal created, nothing charged. Click **Confirm** → charged; the reply shows the Braintree transaction ID.
5. **"status"** → live status from Braintree. **"refund"** → voided (unsettled) / refunded (settled).
6. **"add rental deposit"** → **"pay"** → **"confirm"** → $900 is over the limit, so it waits in *Approvals*. Click **Approve**, then **"pay"** → charged.
7. Pick PayPal in Drop-in: if PayPal isn't enabled on the merchant, the agent says so and asks for a card instead.
8. Show the *Audit log* panel: every tool call, confirmation, approval and charge.

## How PayPal / Braintree is used
| Step | Braintree API |
|---|---|
| Render PayPal + card UI | `client_token.generate()` + Drop-in `paypal: {flow: 'vault'}` |
| Charge | `transaction.sale(..., submit_for_settlement=True)` |
| History | `transaction.search(created_at >= 30d)` |
| Refund | `transaction.void` / `transaction.refund` |
| Vault card | `customer.create(payment_method_nonce)` → `payment_method_token` |
| Status | `transaction.find` |

Braintree is a PayPal service; PayPal personal sign-up is unavailable in Pakistan, so this project uses a Braintree sandbox account.

## Setup
```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # fill in Braintree sandbox keys (optional)
set -a; . ./.env; set +a
uvicorn app.main:app --reload   # http://localhost:8000
pytest -q                        # runs without keys; sandbox tests run if .env.braintree / BRAINTREE_* exist
```
Sandbox test values: PayPal button logs in with a sandbox buyer; card `4111 1111 1111 1111`, any future expiry.

## Deploy to Cloudflare
Approach: **Cloudflare Containers** — a small Worker (`cloudflare/src/index.ts`) routes every request to this FastAPI app running from the `Dockerfile`. Chosen because the Braintree Python SDK uses `requests`/blocking HTTP and native deps that Python Workers (Pyodide) don't reliably support; a container runs the app unchanged.

**Plan requirement:** Containers need the **Workers Paid** plan ($5/month, includes container usage allowance). Free plan cannot run containers.

```bash
cd cloudflare && npm install
npx wrangler login                      # or CLOUDFLARE_API_TOKEN + CLOUDFLARE_ACCOUNT_ID
npx wrangler secret put BRAINTREE_PUBLIC_KEY
npx wrangler secret put BRAINTREE_PRIVATE_KEY
# only if LLM_PROVIDER=bedrock:
npx wrangler secret put AWS_ACCESS_KEY_ID
npx wrangler secret put AWS_SECRET_ACCESS_KEY
npx wrangler deploy                     # needs Docker running locally
```
Or run the **deploy-cloudflare** GitHub Action manually (repo secrets `CLOUDFLARE_API_TOKEN`, `CLOUDFLARE_ACCOUNT_ID`).

Optional custom domain (`pay.taswar.dev`): `cd infra/terraform`, copy `terraform.tfvars.example` to `terraform.tfvars`, set `zone_id`, then `terraform init && terraform apply`.

Persistence plan (D1): see `docs/PERSISTENCE.md`.

## License
MIT
