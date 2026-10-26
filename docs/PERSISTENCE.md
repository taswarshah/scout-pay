# Persistence plan
Today pending approvals live in container memory (lost when the container sleeps after 15 min idle); transactions are always read back from Braintree, so nothing payment-critical is lost.

Plan: Cloudflare **D1** (SQLite) bound to the Worker.
- `approvals(id, amount, description, nonce_ref, status, created_at, decided_at)`
- `audit(id, event, txn_id, amount, at)`
The Worker exposes `/internal/db/*` to the container (or the container calls a Worker route) so Python stays unchanged except a storage adapter in `app/store.py`. Nonces are single-use and short-lived, so for approvals older than ~3h we'd re-collect payment instead of storing nonces.
