"""Scout Pay agent: tool-calling relocation payments agent.

Safety is enforced in code, not by the LLM:
  * propose_payment only creates a proposal; nothing is charged.
  * A proposal becomes payable only after the *human* confirms it (their own chat
    message or the UI button) and, above APPROVAL_THRESHOLD, after an approver approves it.
  * Spending limits (per payment and rolling 24h) are checked at propose and at execute.
  * Every charge uses an idempotency key ("proposal:<id>"), so retries never double-charge.
  * Every tool call and human decision is written to the audit log.
"""
import datetime, json, os, re, uuid
from decimal import Decimal, InvalidOperation

from . import catalog
from .gateway import PAYPAL_DISABLED_MSG

CONFIRM_RE = re.compile(r"^\s*(yes|y|confirm(ed)?|go ahead|ok,? pay|pay now|proceed)\b", re.I)
CANCEL_RE = re.compile(r"^\s*(no|cancel|stop|don'?t)\b", re.I)


def D(x, default="0"):
    try:
        return Decimal(str(x))
    except (InvalidOperation, TypeError):
        return Decimal(default)


def limits():
    return {"approval_threshold": D(os.getenv("APPROVAL_THRESHOLD", "500")),
            "max_single": D(os.getenv("MAX_SINGLE_PAYMENT", "2000")),
            "daily": D(os.getenv("DAILY_LIMIT", "3000"))}


TOOL_SPECS = [
    {"name": "search_services", "description": "Search the relocation services catalog (movers, housing deposits, visa fees, utilities, transport).",
     "params": {"query": ("string", "free-text search"), "category": ("string", "optional: movers|housing|visa|utilities|transport"),
                "max_price": ("number", "optional max price USD")}, "required": ["query"]},
    {"name": "add_to_cart", "description": "Add a catalog service to the user's cart by its id.",
     "params": {"service_id": ("string", "catalog id"), "quantity": ("integer", "default 1")}, "required": ["service_id"]},
    {"name": "remove_from_cart", "description": "Remove a service from the cart.",
     "params": {"service_id": ("string", "catalog id")}, "required": ["service_id"]},
    {"name": "view_cart", "description": "Show the cart / quote with the total.", "params": {}, "required": []},
    {"name": "propose_payment", "description": "Turn the current cart into a payment proposal. Does NOT charge; the user must confirm, and payments over the approval threshold need an approver.",
     "params": {"description": ("string", "short description")}, "required": []},
    {"name": "execute_payment", "description": "Charge a proposal that is ready (confirmed by the user and approved if required) using the user's card/PayPal payment method.",
     "params": {"proposal_id": ("string", "proposal id")}, "required": ["proposal_id"]},
    {"name": "check_status", "description": "Check a Braintree transaction status.",
     "params": {"transaction_id": ("string", "transaction id")}, "required": ["transaction_id"]},
    {"name": "refund_payment", "description": "Refund (if settled) or void (if not yet settled) a transaction made by this agent.",
     "params": {"transaction_id": ("string", "transaction id")}, "required": ["transaction_id"]},
]

SYSTEM = ("You are Scout Pay, an agent that helps someone relocating abroad find and pay for relocation services "
          "(movers, rental deposits, visa fees, utilities). Use the tools. Always search the catalog before adding items. "
          "Never claim a payment happened unless execute_payment returned success. After propose_payment, ask the user to "
          "reply 'confirm'. Only call execute_payment when the proposal status is 'ready'. If PayPal is not available, "
          "suggest paying by card. Be concise and state amounts in USD.")


class Agent:
    def __init__(self, repo, gateway, llm=None):
        self.repo, self.gw = repo, gateway
        self.nonces = {}  # session_id -> single-use nonce, memory only (never persisted)
        self.llm = llm

    # ---------- state ----------
    def state(self, sid):
        return self.repo.get_session(sid) or {"cart": [], "proposal_id": None, "last_txn": None, "token": None,
                                              "payment_kind": None, "history": []}

    def cart_view(self, st):
        items, total = [], Decimal("0")
        for it in st["cart"]:
            s = catalog.BY_ID[it["service_id"]]
            line = D(s["price"]) * it["quantity"]
            total += line
            items.append({"service_id": s["id"], "name": s["name"], "unit_price": s["price"],
                          "quantity": it["quantity"], "line_total": str(line)})
        return {"items": items, "total": str(total)}

    # ---------- human actions (never exposed as LLM tools) ----------
    def confirm(self, sid, pid, actor="user"):
        p = self.repo.get_proposal(pid)
        if not p or p["session_id"] != sid or p["status"] != "awaiting_confirmation":
            return {"ok": False, "error": "nothing to confirm"}
        status = "awaiting_approval" if p["needs_approval"] else "ready"
        self.repo.update_proposal(pid, status)
        self.repo.audit(actor, "proposal_confirmed", sid, pid, p["amount"], {"new_status": status})
        return {"ok": True, "status": status}

    def cancel(self, sid, pid, actor="user"):
        p = self.repo.get_proposal(pid)
        if not p or p["session_id"] != sid or p["status"] in ("paid", "rejected", "cancelled"):
            return {"ok": False, "error": "nothing to cancel"}
        self.repo.update_proposal(pid, "cancelled")
        self.repo.audit(actor, "proposal_cancelled", sid, pid, p["amount"])
        return {"ok": True, "status": "cancelled"}

    def decide(self, pid, decision, approver="approver"):
        p = self.repo.get_proposal(pid)
        if not p or p["status"] != "awaiting_approval" or decision not in ("approve", "reject"):
            return {"ok": False, "error": "no pending approval with that id"}
        status = "ready" if decision == "approve" else "rejected"
        self.repo.update_proposal(pid, status)
        self.repo.audit(approver, f"proposal_{decision}d", p["session_id"], pid, p["amount"])
        return {"ok": True, "status": status, "proposal_id": pid}

    def set_payment_method(self, sid, nonce=None, token=None, vault=False, kind=None):
        st = self.state(sid)
        if nonce and vault:
            r = self.gw.vault(nonce)
            if r["success"]:
                st["token"], nonce = r["token"], None
                self.repo.audit("user", "payment_method_vaulted", sid)
        if token:
            st["token"] = token
        if nonce:
            self.nonces[sid] = nonce
            st["token"] = None if not vault else st["token"]
        st["payment_kind"] = kind or ("paypal" if nonce and "paypal" in nonce else "card")
        self.repo.save_session(sid, st)
        self.repo.audit("user", "payment_method_set", sid, detail={"kind": st["payment_kind"], "vaulted": bool(st["token"])})

    # ---------- tools ----------
    def run_tool(self, sid, name, args):
        fn = getattr(self, "t_" + name, None)
        if not fn:
            return {"error": f"unknown tool {name}"}
        try:
            res = fn(sid, **(args or {}))
        except TypeError as e:
            res = {"error": f"bad arguments: {e}"}
        self.repo.audit("agent", "tool:" + name, sid, detail={"args": args, "ok": "error" not in res})
        return res

    def t_search_services(self, sid, query="", category=None, max_price=None):
        return {"results": catalog.search(query, category, max_price)[:5]}

    def t_add_to_cart(self, sid, service_id, quantity=1):
        if service_id not in catalog.BY_ID:
            return {"error": "unknown service_id"}
        q = max(1, min(int(quantity or 1), 10))
        st = self.state(sid)
        for it in st["cart"]:
            if it["service_id"] == service_id:
                it["quantity"] = q; break
        else:
            st["cart"].append({"service_id": service_id, "quantity": q})
        self.repo.save_session(sid, st)
        return {"added": catalog.BY_ID[service_id]["name"], "cart": self.cart_view(st)}

    def t_remove_from_cart(self, sid, service_id):
        st = self.state(sid)
        st["cart"] = [i for i in st["cart"] if i["service_id"] != service_id]
        self.repo.save_session(sid, st)
        return {"cart": self.cart_view(st)}

    def t_view_cart(self, sid):
        return {"cart": self.cart_view(self.state(sid))}

    def _limit_error(self, sid, amount):
        L = limits()
        if amount <= 0:
            return "amount must be positive"
        if amount > L["max_single"]:
            return f"amount ${amount} exceeds the per-payment limit of ${L['max_single']}"
        since = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=24)).isoformat(timespec="seconds")
        spent = self.repo.spent_since(since, sid)
        if spent + amount > L["daily"]:
            return f"would exceed the 24h spending limit of ${L['daily']} (already spent ${spent})"
        return None

    def t_propose_payment(self, sid, description=None):
        st = self.state(sid)
        cart = self.cart_view(st)
        amount = D(cart["total"])
        if not cart["items"]:
            return {"error": "cart is empty"}
        err = self._limit_error(sid, amount)
        if err:
            self.repo.audit("policy", "proposal_blocked", sid, amount=amount, detail={"reason": err})
            return {"error": err}
        old = st.get("proposal_id") and self.repo.get_proposal(st["proposal_id"])
        if old and old["status"] in ("awaiting_confirmation", "awaiting_approval", "ready"):
            self.repo.update_proposal(old["id"], "cancelled")  # superseded
        needs = amount > limits()["approval_threshold"]
        p = {"id": "p_" + uuid.uuid4().hex[:10], "session_id": sid, "amount": amount,
             "description": description or ", ".join(i["name"] for i in cart["items"])[:200],
             "items": cart["items"], "status": "awaiting_confirmation", "needs_approval": needs}
        self.repo.add_proposal(p)
        st["proposal_id"] = p["id"]
        self.repo.save_session(sid, st)
        self.repo.audit("agent", "proposal_created", sid, p["id"], amount, {"needs_approval": needs})
        return {"proposal_id": p["id"], "amount": str(amount), "status": p["status"], "needs_approval": needs,
                "next": "Ask the user to reply 'confirm'." + (" Then an approver must approve it (over threshold)." if needs else "")}

    def t_execute_payment(self, sid, proposal_id=None):
        st = self.state(sid)
        proposal_id = proposal_id or st.get("proposal_id")
        p = proposal_id and self.repo.get_proposal(proposal_id)
        if not p or p["session_id"] != sid:
            return {"error": "unknown proposal"}
        key = "proposal:" + p["id"]
        prior = self.repo.get_payment(key)
        if prior and prior["success"]:
            return {"idempotent_replay": True, **prior["result"]}
        if p["status"] != "ready":
            msg = {"awaiting_confirmation": "the user has not confirmed this payment yet",
                   "awaiting_approval": "this payment is over the approval threshold and is waiting for an approver",
                   }.get(p["status"], f"proposal is {p['status']}")
            self.repo.audit("policy", "execute_blocked", sid, p["id"], p["amount"], {"reason": msg})
            return {"error": msg, "status": p["status"]}
        amount = D(p["amount"])
        err = self._limit_error(sid, amount)
        if err:
            self.repo.audit("policy", "execute_blocked", sid, p["id"], amount, {"reason": err})
            return {"error": err}
        nonce, token = self.nonces.pop(sid, None), st.get("token")
        if not nonce and not token:
            if self.gw.name == "mock":
                nonce = "fake-valid-nonce"
            else:
                return {"error": "no payment method: add a card or PayPal in the payment box first"}
        res = self.gw.sale(amount, nonce=None if token else nonce, token=token, description=p["description"],
                           order_id="sp-" + p["id"])
        self.repo.record_payment(key, p["id"], sid, amount, res)
        t = res.get("transaction") or {}
        if res.get("success"):
            self.repo.update_proposal(p["id"], "paid", t.get("id"))
            st["last_txn"] = t.get("id"); self.repo.save_session(sid, st)
            self.repo.audit("gateway", "payment_succeeded", sid, t.get("id"), amount, {"proposal": p["id"]})
            return res
        self.repo.audit("gateway", "payment_failed", sid, t.get("id"), amount, {"error": res.get("error")})
        # Nonce consumed; let user retry with a new method. PayPal degrades gracefully to card.
        if PAYPAL_DISABLED_MSG.split(" by")[0] in (res.get("error") or ""):
            res["hint"] = "PayPal isn't enabled on this merchant account yet; please pay by card instead."
        self.repo._exec("DELETE FROM payments WHERE idempotency_key=? AND success=0", (key,))  # allow retry
        return res

    def _owned(self, sid, tid):
        p = self.repo.payment_by_txn(tid)
        return p and p["session_id"] == sid

    def t_check_status(self, sid, transaction_id):
        if not self._owned(sid, transaction_id):
            return {"error": "transaction not found for this session"}
        return self.gw.find(transaction_id)

    def t_refund_payment(self, sid, transaction_id):
        if not self._owned(sid, transaction_id):
            return {"error": "transaction not found for this session"}
        res = self.gw.refund(transaction_id)
        self.repo.audit("gateway", "refund_" + ("succeeded" if res.get("success") else "failed"), sid,
                        transaction_id, detail={"error": res.get("error"), "status": (res.get("transaction") or {}).get("status")})
        return res

    # ---------- chat ----------
    def chat(self, sid, message, nonce=None, token=None, vault=False):
        if nonce or token:
            self.set_payment_method(sid, nonce=nonce, token=token, vault=vault)
        st = self.state(sid)
        self.repo.audit("user", "message", sid, detail={"text": message[:500]})
        human = None
        pid = st.get("proposal_id")
        p = pid and self.repo.get_proposal(pid)
        if p and p["status"] == "awaiting_confirmation" and CONFIRM_RE.match(message):
            human = self.confirm(sid, pid)
        elif p and p["status"] in ("awaiting_confirmation", "awaiting_approval", "ready") and CANCEL_RE.match(message):
            human = self.cancel(sid, pid)
        from .planner import plan
        out = plan(self, sid, message, human)
        st = self.state(sid)
        st["history"] = (st.get("history", []) + [{"role": "user", "text": message},
                                                 {"role": "assistant", "text": out["reply"]}])[-20:]
        self.repo.save_session(sid, st)
        prop = st.get("proposal_id") and self.repo.get_proposal(st["proposal_id"])
        return {"session_id": sid, "reply": out["reply"], "tool_calls": out["tool_calls"], "provider": out["provider"],
                "cart": self.cart_view(st), "proposal": prop or None, "last_txn": st.get("last_txn")}
