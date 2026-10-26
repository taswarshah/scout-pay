# Scout Pay

**An AI relocation agent that plans your move and pays for it with PayPal (via Braintree).**

Built for the [PayPal AI Hackathon](https://paypalaihackathon.devpost.com/). Scout Pay asks where you're moving and your budget, an LLM turns that into a move plan with the payments it needs (mover deposit, rental deposit, utility setup), and you pay each one through Braintree Drop-in, which shows the **PayPal button** alongside cards. Payments above a threshold stop for a **human approval** step before the agent can charge anything.

> **Hackathon note:** this repository was created and built during the hackathon (after Oct 1, 2026). It's a new codebase inspired by my earlier Relocation Scout project; the payment agent, approval flow and Braintree/PayPal integration are all new.

## Features
- AI move plan → list of payments (LLM provider abstraction: AWS Bedrock Converse API, or a deterministic mock)
- Checkout with Braintree Drop-in (PayPal + cards), sandbox by default
- Human-in-the-loop approval for payments above `APPROVAL_THRESHOLD`
- Transaction list (last 30 days) and refunds (void if unsettled, refund if settled)
- Runs with **no keys at all**: falls back to a mock gateway and mock LLM

## Architecture
```
browser (static/index.html, Braintree Drop-in)
   │  /api/plan  /api/checkout  /api/approvals  /api/transactions
FastAPI (app/main.py)
   ├── app/llm.py      → Bedrock (LLM_PROVIDER=bedrock) or mock
   └── app/gateway.py  → BraintreeGateway (keys set) or MockGateway
```

## How PayPal / Braintree is used
| Step | Braintree API |
|---|---|
| Render PayPal + card UI | `client_token.generate()` + Drop-in `paypal: {flow: 'vault'}` |
| Charge | `transaction.sale(..., submit_for_settlement=True)` |
| History | `transaction.search(created_at >= 30d)` |
| Refund | `transaction.void` / `transaction.refund` |

Braintree is a PayPal service; PayPal personal sign-up is unavailable in Pakistan, so this project uses a Braintree sandbox account.

## Setup
```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # fill in Braintree sandbox keys (optional)
set -a; . ./.env; set +a
uvicorn app.main:app --reload   # http://localhost:8000
pytest -q                        # runs without keys
```
Sandbox test values: PayPal button logs in with a sandbox buyer; card `4111 1111 1111 1111`, any future expiry.

## License
MIT
