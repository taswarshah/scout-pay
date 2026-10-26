import os, uuid
from decimal import Decimal
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from .gateway import get_gateway
from .llm import plan_move

app = FastAPI(title="Scout Pay")
gateway = get_gateway()
pending = {}  # approval_id -> payment request awaiting a human
THRESHOLD = lambda: Decimal(os.getenv("APPROVAL_THRESHOLD", "500"))
STATIC = os.path.join(os.path.dirname(__file__), "..", "static")


class PlanReq(BaseModel):
    origin: str
    destination: str
    people: int = 1
    budget: float = 3000


class PayReq(BaseModel):
    amount: Decimal
    nonce: str
    description: str = ""


@app.get("/")
def index():
    return FileResponse(os.path.join(STATIC, "index.html"))


@app.get("/api/config")
def config():
    return {"gateway": gateway.name, "approval_threshold": str(THRESHOLD())}


@app.get("/api/client-token")
def client_token():
    return {"token": gateway.client_token(), "gateway": gateway.name}


@app.post("/api/plan")
def plan(r: PlanReq):
    return plan_move(r.origin, r.destination, r.people, r.budget)


@app.post("/api/checkout")
def checkout(r: PayReq):
    if r.amount <= 0:
        raise HTTPException(400, "amount must be positive")
    if r.amount > THRESHOLD():
        aid = uuid.uuid4().hex[:8]
        pending[aid] = r.model_dump(mode="json")
        return {"status": "pending_approval", "approval_id": aid}
    return gateway.sale(r.amount, r.nonce, r.description)


@app.get("/api/approvals")
def approvals():
    return [{"approval_id": k, **v} for k, v in pending.items()]


@app.post("/api/approvals/{aid}/{decision}")
def decide(aid: str, decision: str):
    if aid not in pending or decision not in ("approve", "reject"):
        raise HTTPException(404, "unknown approval")
    req = pending.pop(aid)
    if decision == "reject":
        return {"status": "rejected"}
    return gateway.sale(Decimal(req["amount"]), req["nonce"], req["description"])


@app.get("/api/transactions")
def transactions():
    return gateway.list()


@app.post("/api/transactions/{tid}/refund")
def refund(tid: str):
    r = gateway.refund(tid)
    if not r["success"]:
        raise HTTPException(400, r.get("error"))
    return r


app.mount("/static", StaticFiles(directory=STATIC), name="static")
