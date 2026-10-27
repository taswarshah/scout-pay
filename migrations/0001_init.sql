-- Scout Pay schema. Same SQL runs on local SQLite and Cloudflare D1.
CREATE TABLE IF NOT EXISTS sessions (id TEXT PRIMARY KEY, state TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS proposals (
  id TEXT PRIMARY KEY, session_id TEXT NOT NULL, amount TEXT NOT NULL, description TEXT NOT NULL,
  items TEXT NOT NULL, status TEXT NOT NULL, -- awaiting_confirmation|awaiting_approval|ready|paid|failed|rejected|cancelled
  needs_approval INTEGER NOT NULL DEFAULT 0, txn_id TEXT, created_at TEXT NOT NULL, decided_at TEXT);
CREATE TABLE IF NOT EXISTS payments (
  idempotency_key TEXT PRIMARY KEY, proposal_id TEXT, session_id TEXT, amount TEXT NOT NULL,
  txn_id TEXT, status TEXT NOT NULL, success INTEGER NOT NULL, result TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS audit (
  id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL, session_id TEXT, actor TEXT NOT NULL,
  event TEXT NOT NULL, ref TEXT, amount TEXT, detail TEXT);
CREATE INDEX IF NOT EXISTS idx_proposals_session ON proposals(session_id);
CREATE INDEX IF NOT EXISTS idx_payments_created ON payments(created_at);
