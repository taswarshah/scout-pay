import hmac, os, uuid
from decimal import Decimal
from typing import Optional
from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from . import catalog
from .agent import Agent, limits
from .gateway import get_gateway
from .llm import plan_move
from .planner import bedrock_available
from .store import SQLiteRepo

app = FastAPI(title="Scout Pay")
gateway = get_gateway()
repo = SQLiteRepo()
agent = Agent(repo, gateway)
STATIC = os.path.join(os.path.dirname(__file__), "..", "static")


def THRESHOLD():
    return limits()["approval_threshold"]


class PlanReq(BaseModel):
    origin: str
    destination: str
    people: int = 1
    budget: float = 3000


class PayReq(BaseModel):
    amount: Decimal
    nonce: str
    description: str = ""


class ChatReq(BaseModel):
    message: str
    session_id: Optional[str] = None
    nonce: Optional[str] = None
    payment_method_token: Optional[str] = None
    vault: bool = False


class SessionReq(BaseModel):
    session_id: str


def require_approver(x_approver_key: Optional[str] = Header(None)):
    """Approver/admin endpoints need X-Approver-Key when APPROVER_KEY is set (open otherwise, for local dev)."""
    key = os.getenv("APPROVER_KEY")
    if key and not hmac.compare_digest(x_approver_key or "", key):
        raise HTTPException(401, "approver key required")


def _agent():
    # tests swap `gateway` at module level; keep the agent pointed at it
    agent.gw = gateway
    return agent


@app.get("/")
def index():
    return FileResponse(os.path.join(STATIC, "index.html"))


@app.get("/healthz")
def healthz():
    return {"ok": True}


@app.get("/api/config")
def config():
    L = limits()
    return {"gateway": gateway.name, "approval_threshold": str(THRESHOLD()), "max_single_payment": str(L["max_single"]),
            "daily_limit": str(L["daily"]), "llm": "bedrock" if bedrock_available() else "mock",
            "approver_key_required": bool(os.getenv("APPROVER_KEY"))}


@app.get("/api/client-token")
def client_token():
    return {"token": gateway.client_token(), "gateway": gateway.name}


@app.get("/api/catalog")
def get_catalog(q: str = ""):
    return catalog.search(q)


@app.post("/api/plan")
def plan(r: PlanReq):
    return plan_move(r.origin, r.destination, r.people, r.budget)


# ---------- agent ----------
@app.post("/api/chat")
def chat(r: ChatReq):
    sid = r.session_id or "s_" + uuid.uuid4().hex[:12]
    return _agent().chat(sid, r.message, nonce=r.nonce, token=r.payment_method_token, vault=r.vault)


@app.post("/api/proposals/{pid}/confirm")
def confirm(pid: str, r: SessionReq):
    res = _agent().confirm(r.session_id, pid, actor="user:ui")
    if not res["ok"]:
        raise HTTPException(409, res["error"])
    return res


@app.post("/api/proposals/{pid}/cancel")
def cancel(pid: str, r: SessionReq):
    res = _agent().cancel(r.session_id, pid, actor="user:ui")
    if not res["ok"]:
        raise HTTPException(409, res["error"])
    return res


@app.get("/api/proposals")
def proposals(status: Optional[str] = None, session_id: Optional[str] = None, x_approver_key: Optional[str] = Header(None)):
    # Your own session's proposals are always visible; everyone's needs the approver key.
    if not session_id:
        require_approver(x_approver_key)
    return [p for p in repo.list_proposals(status) if not session_id or p["session_id"] == session_id]


@app.get("/api/audit")
def audit(session_id: Optional[str] = None, limit: int = 100, x_approver_key: Optional[str] = Header(None)):
    if not session_id:
        require_approver(x_approver_key)
    return repo.audit_log(limit, session_id)


# ---------- direct checkout (non-agent) ----------
@app.post("/api/checkout")
def checkout(r: PayReq, idempotency_key: Optional[str] = Header(None)):
    a = _agent()
    if r.amount <= 0:
        raise HTTPException(400, "amount must be positive")
    if idempotency_key:
        prior = repo.get_payment("direct:" + idempotency_key)
        if prior and prior["success"]:
            return {**prior["result"], "idempotent_replay": True}
    sid = "direct-" + uuid.uuid4().hex[:8]
    err = a._limit_error(None, r.amount)
    if err:
        repo.audit("policy", "checkout_blocked", sid, amount=r.amount, detail={"reason": err})
        raise HTTPException(422, err)
    if r.amount > THRESHOLD():
        pid = "p_" + uuid.uuid4().hex[:10]
        repo.add_proposal({"id": pid, "session_id": sid, "amount": r.amount, "description": r.description,
                           "items": [], "status": "awaiting_approval", "needs_approval": True})
        a.nonces[sid] = r.nonce
        repo.audit("user", "checkout_needs_approval", sid, pid, r.amount)
        return {"status": "pending_approval", "approval_id": pid}
    res = gateway.sale(r.amount, r.nonce, r.description)
    repo.record_payment("direct:" + (idempotency_key or uuid.uuid4().hex), None, sid, r.amount, res)
    repo.audit("gateway", "payment_" + ("succeeded" if res["success"] else "failed"), sid,
               (res.get("transaction") or {}).get("id"), r.amount, {"error": res.get("error")})
    return res


@app.get("/api/approvals", dependencies=[Depends(require_approver)])
def approvals():
    return [{"approval_id": p["id"], "amount": p["amount"], "description": p["description"], "session_id": p["session_id"]}
            for p in repo.list_proposals("awaiting_approval")]


@app.post("/api/approvals/{aid}/{decision}", dependencies=[Depends(require_approver)])
def decide(aid: str, decision: str):
    a = _agent()
    res = a.decide(aid, decision, approver="approver:ui")
    if not res["ok"]:
        raise HTTPException(404, "unknown approval")
    if decision == "reject":
        return {"status": "rejected"}
    p = repo.get_proposal(aid)
    if p["session_id"].startswith("direct-"):  # direct checkout: charge now with the held nonce
        return a.t_execute_payment(p["session_id"], aid)
    return {"status": "ready", "proposal_id": aid, "next": "tell the agent 'pay'"}


@app.get("/api/transactions", dependencies=[Depends(require_approver)])
def transactions():
    return gateway.list()


@app.post("/api/transactions/{tid}/refund", dependencies=[Depends(require_approver)])
def refund(tid: str):
    r = gateway.refund(tid)
    repo.audit("user:ui", "refund_" + ("succeeded" if r["success"] else "failed"), None, tid, detail={"error": r.get("error")})
    if not r["success"]:
        raise HTTPException(400, r.get("error"))
    return r


app.mount("/static", StaticFiles(directory=STATIC), name="static")
