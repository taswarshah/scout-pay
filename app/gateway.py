"""Payment gateway abstraction: Braintree sandbox (cards + PayPal) or in-memory mock."""
import datetime, os, uuid
from decimal import Decimal

PAYPAL_DISABLED_MSG = "PayPal Accounts are not accepted by this merchant account."


class MockGateway:
    name = "mock"

    def __init__(self):
        self.txns = {}

    def client_token(self):
        return "mock-client-token"

    def sale(self, amount, nonce=None, description="", token=None, order_id=None):
        nonce = nonce or ""
        if "paypal" in nonce and os.getenv("MOCK_PAYPAL_DISABLED"):
            return {"success": False, "error": PAYPAL_DISABLED_MSG}
        if nonce == "fake-processor-declined-visa-nonce":
            return {"success": False, "error": "Do Not Honor"}
        tid = uuid.uuid4().hex[:8]
        self.txns[tid] = {"id": tid, "amount": str(Decimal(amount)), "status": "submitted_for_settlement",
                          "description": description, "order_id": order_id,
                          "payment_type": "paypal_account" if "paypal" in nonce else "credit_card"}
        return {"success": True, "transaction": dict(self.txns[tid])}

    def list(self):
        return [dict(t) for t in self.txns.values()]

    def find(self, tid):
        t = self.txns.get(tid)
        return {"success": True, "transaction": dict(t)} if t else {"success": False, "error": "not found"}

    def vault(self, nonce):
        return {"success": True, "token": "mock-token-" + uuid.uuid4().hex[:6]}

    def refund(self, tid):
        t = self.txns.get(tid)
        if not t:
            return {"success": False, "error": "not found"}
        if t["status"] == "voided":
            return {"success": False, "error": "Transaction already voided"}
        t["status"] = "voided"  # unsettled -> void (mirrors Braintree)
        return {"success": True, "transaction": dict(t)}


class BraintreeGateway:
    name = "braintree"

    def __init__(self):
        import braintree
        self.bt = braintree
        env = braintree.Environment.Production if os.getenv("BRAINTREE_ENVIRONMENT") == "production" else braintree.Environment.Sandbox
        self.gw = braintree.BraintreeGateway(braintree.Configuration(
            environment=env,
            merchant_id=os.environ["BRAINTREE_MERCHANT_ID"],
            public_key=os.environ["BRAINTREE_PUBLIC_KEY"],
            private_key=os.environ["BRAINTREE_PRIVATE_KEY"]))

    @staticmethod
    def _t(t):
        return {"id": t.id, "amount": str(t.amount), "status": t.status, "type": t.type,
                "payment_type": t.payment_instrument_type, "order_id": t.order_id,
                "processor_response": getattr(t, "processor_response_text", None)}

    def client_token(self):
        return self.gw.client_token.generate()

    def sale(self, amount, nonce=None, description="", token=None, order_id=None):
        req = {"amount": str(amount), "order_id": (order_id or "sp-" + uuid.uuid4().hex[:12])[:255],
               "options": {"submit_for_settlement": True}}
        if token:
            req["payment_method_token"] = token
        else:
            req["payment_method_nonce"] = nonce
        try:
            r = self.gw.transaction.sale(req)
        except Exception as e:  # network/auth errors must not crash the agent
            return {"success": False, "error": type(e).__name__}
        if r.is_success:
            return {"success": True, "transaction": self._t(r.transaction)}
        if r.transaction is not None:  # gateway/processor rejection: surface status + id
            return {"success": False, "error": r.message, "transaction": self._t(r.transaction)}
        return {"success": False, "error": r.message}

    def list(self):
        since = datetime.datetime.utcnow() - datetime.timedelta(days=30)
        res = self.gw.transaction.search(self.bt.TransactionSearch.created_at >= since)
        return [self._t(t) for t in res.items]

    def find(self, tid):
        try:
            return {"success": True, "transaction": self._t(self.gw.transaction.find(tid))}
        except Exception as e:
            return {"success": False, "error": type(e).__name__}

    def vault(self, nonce):
        r = self.gw.customer.create({"payment_method_nonce": nonce})
        if r.is_success:
            return {"success": True, "token": r.customer.payment_methods[0].token}
        return {"success": False, "error": r.message}

    def refund(self, tid):
        try:
            t = self.gw.transaction.find(tid)
        except Exception as e:
            return {"success": False, "error": type(e).__name__}
        r = self.gw.transaction.refund(tid) if t.status in ("settled", "settling") else self.gw.transaction.void(tid)
        if r.is_success:
            return {"success": True, "transaction": self._t(r.transaction)}
        return {"success": False, "error": r.message}


def get_gateway():
    if all(os.getenv(k) for k in ("BRAINTREE_MERCHANT_ID", "BRAINTREE_PUBLIC_KEY", "BRAINTREE_PRIVATE_KEY")):
        return BraintreeGateway()
    return MockGateway()
