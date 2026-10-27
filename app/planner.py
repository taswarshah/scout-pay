"""Planners that drive the agent's tools: AWS Bedrock (Converse tool use) or a deterministic mock."""
import json, os, re

from .agent import SYSTEM, TOOL_SPECS
from . import catalog

TXN_RE = re.compile(r"\b([a-z0-9]{6,10})\b")


def bedrock_available():
    mode = os.getenv("LLM_PROVIDER", "auto").lower()
    if mode == "mock":
        return False
    if mode == "bedrock":
        return True
    try:
        import boto3
        return boto3.Session().get_credentials() is not None
    except Exception:
        return False


def plan(agent, sid, message, human):
    if bedrock_available():
        try:
            return bedrock_plan(agent, sid, message, human)
        except _NoFallback as e:
            return e.args[0]
        except Exception as e:
            out = mock_plan(agent, sid, message, human)
            out["provider"] = f"mock (bedrock unavailable: {type(e).__name__})"
            return out
    return mock_plan(agent, sid, message, human)


class _NoFallback(Exception):
    pass


# ---------------- Bedrock ----------------
def _tool_config():
    tools = []
    for t in TOOL_SPECS:
        props = {k: {"type": v[0], "description": v[1]} for k, v in t["params"].items()}
        tools.append({"toolSpec": {"name": t["name"], "description": t["description"],
                                   "inputSchema": {"json": {"type": "object", "properties": props, "required": t["required"]}}}})
    return {"tools": tools}


def bedrock_plan(agent, sid, message, human):
    import boto3
    client = boto3.client("bedrock-runtime", region_name=os.getenv("AWS_REGION", "us-east-1"))
    model = os.getenv("BEDROCK_MODEL_ID", "amazon.nova-lite-v1:0")
    st = agent.state(sid)
    msgs = []
    for h in st.get("history", [])[-10:]:
        msgs.append({"role": h["role"], "content": [{"text": h["text"] or "."}]})
    ctx = {"cart": agent.cart_view(st), "proposal": st.get("proposal_id") and agent.repo.get_proposal(st["proposal_id"]),
           "last_txn": st.get("last_txn"), "human_action_this_turn": human}
    msgs.append({"role": "user", "content": [{"text": f"{message}\n\n[context: {json.dumps(ctx, default=str)}]"}]})
    calls = []
    for _ in range(6):
        r = client.converse(modelId=model, system=[{"text": SYSTEM}], messages=msgs, toolConfig=_tool_config(),
                            inferenceConfig={"maxTokens": 800, "temperature": 0})
        out = r["output"]["message"]
        msgs.append(out)
        uses = [c["toolUse"] for c in out["content"] if "toolUse" in c]
        if not uses:
            text = " ".join(c.get("text", "") for c in out["content"]).strip()
            return {"reply": text or "Done.", "tool_calls": calls, "provider": "bedrock"}
        results = []
        for u in uses:
            res = agent.run_tool(sid, u["name"], u.get("input") or {})
            calls.append({"name": u["name"], "args": u.get("input"), "result": res})
            results.append({"toolResult": {"toolUseId": u["toolUseId"], "content": [{"json": json.loads(json.dumps(res, default=str))}]}})
        msgs.append({"role": "user", "content": results})
    if calls:  # tools already ran; don't re-run them via the mock
        raise _NoFallback({"reply": "I ran out of steps; here is what I did.", "tool_calls": calls, "provider": "bedrock"})
    raise RuntimeError("no result")


# ---------------- Mock (deterministic, offline) ----------------
def _fmt_cart(cart):
    if not cart["items"]:
        return "Your cart is empty."
    lines = [f"- {i['name']} x{i['quantity']}: ${i['line_total']}" for i in cart["items"]]
    return "Cart:\n" + "\n".join(lines) + f"\nTotal: ${cart['total']}"


def _payment_reply(res):
    if res.get("success"):
        t = res["transaction"]
        return f"Paid ${t['amount']}. Transaction {t['id']} is {t['status']}."
    return f"Payment not made: {res.get('error')}." + (f" {res['hint']}" if res.get("hint") else "")


def mock_plan(agent, sid, message, human):
    calls = []

    def call(name, **args):
        res = agent.run_tool(sid, name, args)
        calls.append({"name": name, "args": args, "result": res})
        return res

    def done(reply):
        return {"reply": reply, "tool_calls": calls, "provider": "mock"}

    m = message.lower().strip()
    st = agent.state(sid)

    if human:
        if not human.get("ok"):
            return done("There was nothing to confirm.")
        if human["status"] == "cancelled":
            return done("Okay, I cancelled that payment proposal. Nothing was charged.")
        if human["status"] == "awaiting_approval":
            return done("Confirmed. This payment is over the approval limit, so it's now waiting for an approver. "
                        "Once it's approved, say 'pay' and I'll charge it.")
        return done(_payment_reply(call("execute_payment", proposal_id=st["proposal_id"])))

    ids = [x for x in TXN_RE.findall(m) if x not in ("refund", "status", "payment", "transaction", "please", "cancel")]
    if re.search(r"\b(refund|void|reverse)\b", m):
        tid = ids[-1] if ids and agent._owned(sid, ids[-1]) else st.get("last_txn")
        if not tid:
            return done("I don't see a payment to refund yet.")
        r = call("refund_payment", transaction_id=tid)
        return done(f"Done: transaction {tid} is now {r['transaction']['status']}." if r.get("success")
                    else f"Couldn't refund {tid}: {r.get('error')}.")
    if re.search(r"\b(status|track|check)\b", m):
        tid = ids[-1] if ids and agent._owned(sid, ids[-1]) else st.get("last_txn")
        if not tid:
            return done("There's no payment to check yet.")
        r = call("check_status", transaction_id=tid)
        return done(f"Transaction {tid}: {r['transaction']['status']} (${r['transaction']['amount']})." if r.get("success")
                    else f"Couldn't check {tid}: {r.get('error')}.")
    if re.search(r"\b(pay|checkout|check out|propose|purchase)\b", m):
        p = st.get("proposal_id") and agent.repo.get_proposal(st["proposal_id"])
        if p and p["status"] == "ready":
            return done(_payment_reply(call("execute_payment", proposal_id=p["id"])))
        if p and p["status"] == "awaiting_approval":
            return done(f"Proposal {p['id']} (${p['amount']}) is still waiting for an approver.")
        r = call("propose_payment")
        if "error" in r:
            return done(f"I can't propose that payment: {r['error']}.")
        extra = " It's over the approval limit, so after you confirm an approver must also approve it." if r["needs_approval"] else ""
        return done(f"Proposed payment {r['proposal_id']} for ${r['amount']}.{extra} Reply 'confirm' to go ahead, or 'cancel'.")
    if m.startswith("remove") or m.startswith("delete"):
        for it in agent.cart_view(st)["items"]:
            if any(w in it["name"].lower() for w in m.split()[1:] if len(w) > 2):
                r = call("remove_from_cart", service_id=it["service_id"])
                return done(f"Removed {it['name']}. " + _fmt_cart(r["cart"]))
        return done("I couldn't find that in your cart.")
    if re.search(r"\b(cart|quote|total)\b", m):
        return done(_fmt_cart(call("view_cart")["cart"]))
    if re.search(r"\b(add|book|need|get|want|reserve|buy)\b", m):
        added = []
        for part in re.split(r",| and | plus |&", m):
            part = re.sub(r"\b(i|we|please|add|book|need|get|want|reserve|buy|a|an|the|to|some|also|my)\b", " ", part)
            if not part.strip():
                continue
            hits = call("search_services", query=part.strip())["results"]
            if hits:
                call("add_to_cart", service_id=hits[0]["id"])
                added.append(hits[0]["name"])
        if not added:
            return done("I couldn't find a matching service. Try movers, rental deposit, visa fee, internet or airport pickup.")
        cart = agent.cart_view(agent.state(sid))
        return done("Added " + "; ".join(added) + ".\n" + _fmt_cart(cart) + "\nSay 'pay' when you're ready and I'll prepare the payment.")
    if re.search(r"\b(search|find|show|options|list|what|which|catalog)\b", m) or len(m.split()) <= 4:
        q = re.sub(r"\b(search|find|show|me|options|for|list|what|which|are|is|there|catalog|services?)\b", " ", m).strip()
        res = call("search_services", query=q)["results"]
        if res:
            return done("Here's what I found:\n" + "\n".join(f"- {s['name']} (${s['price']}) [{s['id']}]" for s in res)
                        + "\nTell me what to add, e.g. 'add movers and visa fee'.")
    return done("I can find relocation services (movers, deposits, visa fees, utilities), build a quote, and pay for it "
                "with your card or PayPal. Try: 'I need movers, a visa fee and internet'.")
