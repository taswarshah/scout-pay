# Persistence

All state goes through `app/store.py`'s `Repo` interface:
sessions (chat + cart state), proposals (payments awaiting confirmation/approval),
payments (idempotency ledger + spend limits) and an append-only audit log.

- **Local / tests:** `SQLiteRepo` (`SCOUT_DB`, default `data/scout.db`; tests use `:memory:`).
- **Cloudflare (today):** `SQLiteRepo` inside the container, so state resets on each deploy or idle restart (15 min).
- **Cloudflare (D1, planned):** the schema in `migrations/0001_init.sql` is D1-compatible SQLite.
  `wrangler d1 create scout-pay && wrangler d1 migrations apply scout-pay` creates it.
  A `D1Repo` only needs `_exec`/`_query`, calling a Worker route (`/internal/db`) that runs
  `env.DB.prepare(sql).bind(...params)`. Nothing else in the app changes.

Transactions themselves are always re-read from Braintree, so the DB is never the source of truth for money;
it's the source of truth for *who approved what* and for idempotency. Payment nonces are single-use (~3h) and are
never stored in the DB; approvals older than that re-collect a payment method (or use a vaulted token).
