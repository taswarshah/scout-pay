"""Payment gateway abstraction: Braintree sandbox (PayPal) or in-memory mock."""
import os, uuid
from decimal import Decimal


class MockGateway:
    name = "mock"

    def __init__(self):
        self.txns = {}

    def client_token(self):
        return "mock-client-token"

    def sale(self, amount, nonce, description=""):
        tid = uuid.uuid4().hex[:8]
        self.txns[tid] = {"id": tid, "amount": str(Decimal(amount)), "status": "submitted_for_settlement",
                          "description": description, "payment_type": "paypal_account" if "paypal" in nonce else "credit_card"}
        return {"success": True, "transaction": self.txns[tid]}

    def list(self):
        return list(self.txns.values())

    def refund(self, tid):
        t = self.txns.get(tid)
        if not t:
            return {"success": False, "error": "not found"}
        # Unsettled transactions are voided, settled ones refunded (mirrors Braintree)
        t["status"] = "voided"
        return {"success": True, "transaction": t}


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
        return {"id": t.id, "amount": str(t.amount), "status": t.status,
                "payment_type": t.payment_instrument_type,
                "description": (t.custom_fields or {}).get("description", "") if hasattr(t, "custom_fields") else ""}

    def client_token(self):
        return self.gw.client_token.generate()

    def sale(self, amount, nonce, description=""):
        r = self.gw.transaction.sale({"amount": str(amount), "payment_method_nonce": nonce,
                                      "options": {"submit_for_settlement": True}})
        if r.is_success:
            return {"success": True, "transaction": self._t(r.transaction)}
        return {"success": False, "error": r.message}

    def list(self):
        import datetime
        since = datetime.datetime.utcnow() - datetime.timedelta(days=30)
        res = self.gw.transaction.search(self.bt.TransactionSearch.created_at >= since)
        return [self._t(t) for t in res.items]

    def refund(self, tid):
        t = self.gw.transaction.find(tid)
        r = self.gw.transaction.refund(tid) if t.status in ("settled", "settling") else self.gw.transaction.void(tid)
        if r.is_success:
            return {"success": True, "transaction": self._t(r.transaction)}
        return {"success": False, "error": r.message}


def get_gateway():
    if all(os.getenv(k) for k in ("BRAINTREE_MERCHANT_ID", "BRAINTREE_PUBLIC_KEY", "BRAINTREE_PRIVATE_KEY")):
        return BraintreeGateway()
    return MockGateway()
