"""Persistence behind a small repository interface.

`SQLiteRepo` is used locally and in tests. The SQL is plain SQLite so the same
statements (and migrations/0001_init.sql) run unchanged on Cloudflare D1; a D1
adapter only has to implement `_exec`/`_query` (see docs/PERSISTENCE.md).
"""
import datetime, json, os, pathlib, sqlite3, threading
from decimal import Decimal

MIGRATIONS = pathlib.Path(__file__).resolve().parent.parent / "migrations"


def now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


class Repo:
    """Interface. Subclasses implement _exec(sql, params) and _query(sql, params) -> list[dict]."""

    def _exec(self, sql, params=()): raise NotImplementedError
    def _query(self, sql, params=()): raise NotImplementedError

    # sessions
    def get_session(self, sid):
        r = self._query("SELECT state FROM sessions WHERE id=?", (sid,))
        return json.loads(r[0]["state"]) if r else None

    def save_session(self, sid, state):
        t = now()
        self._exec("INSERT INTO sessions(id,state,created_at,updated_at) VALUES(?,?,?,?) "
                   "ON CONFLICT(id) DO UPDATE SET state=excluded.state, updated_at=excluded.updated_at",
                   (sid, json.dumps(state), t, t))

    # proposals
    def add_proposal(self, p):
        self._exec("INSERT INTO proposals(id,session_id,amount,description,items,status,needs_approval,created_at) "
                   "VALUES(?,?,?,?,?,?,?,?)", (p["id"], p["session_id"], str(p["amount"]), p["description"],
                   json.dumps(p["items"]), p["status"], int(p["needs_approval"]), now()))

    def get_proposal(self, pid):
        r = self._query("SELECT * FROM proposals WHERE id=?", (pid,))
        return self._p(r[0]) if r else None

    def list_proposals(self, status=None):
        if status:
            return [self._p(r) for r in self._query("SELECT * FROM proposals WHERE status=? ORDER BY created_at", (status,))]
        return [self._p(r) for r in self._query("SELECT * FROM proposals ORDER BY created_at")]

    def update_proposal(self, pid, status, txn_id=None):
        self._exec("UPDATE proposals SET status=?, txn_id=COALESCE(?,txn_id), decided_at=? WHERE id=?",
                   (status, txn_id, now(), pid))

    @staticmethod
    def _p(r):
        r = dict(r); r["items"] = json.loads(r["items"]); r["needs_approval"] = bool(r["needs_approval"]); return r

    # payments / idempotency
    def get_payment(self, key):
        r = self._query("SELECT * FROM payments WHERE idempotency_key=?", (key,))
        if not r: return None
        d = dict(r[0]); d["result"] = json.loads(d["result"]); return d

    def record_payment(self, key, proposal_id, session_id, amount, result):
        t = result.get("transaction") or {}
        self._exec("INSERT OR IGNORE INTO payments(idempotency_key,proposal_id,session_id,amount,txn_id,status,success,result,created_at) "
                   "VALUES(?,?,?,?,?,?,?,?,?)", (key, proposal_id, session_id, str(amount), t.get("id"),
                   t.get("status", "error"), int(bool(result.get("success"))), json.dumps(result), now()))

    def spent_since(self, since_iso, session_id=None):
        q = "SELECT amount FROM payments WHERE success=1 AND created_at>=?"
        args = [since_iso]
        if session_id: q += " AND session_id=?"; args.append(session_id)
        return sum((Decimal(r["amount"]) for r in self._query(q, tuple(args))), Decimal("0"))

    # audit
    def audit(self, actor, event, session_id=None, ref=None, amount=None, detail=None):
        self._exec("INSERT INTO audit(at,session_id,actor,event,ref,amount,detail) VALUES(?,?,?,?,?,?,?)",
                   (now(), session_id, actor, event, ref, None if amount is None else str(amount),
                    None if detail is None else json.dumps(detail, default=str)))

    def audit_log(self, limit=200, session_id=None):
        if session_id:
            return self._query("SELECT * FROM audit WHERE session_id=? ORDER BY id DESC LIMIT ?", (session_id, limit))
        return self._query("SELECT * FROM audit ORDER BY id DESC LIMIT ?", (limit,))


class SQLiteRepo(Repo):
    def __init__(self, path=None):
        path = path or os.getenv("SCOUT_DB", "data/scout.db")
        if path != ":memory:":
            pathlib.Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.Lock()
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        for f in sorted(MIGRATIONS.glob("*.sql")):
            self.db.executescript(f.read_text())

    def _exec(self, sql, params=()):
        with self.lock:
            self.db.execute(sql, params); self.db.commit()

    def _query(self, sql, params=()):
        with self.lock:
            return [dict(r) for r in self.db.execute(sql, params).fetchall()]
