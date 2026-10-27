"""Live Braintree sandbox test. Skipped unless creds are present (env or .env.braintree)."""
import os, pathlib
import pytest
from fastapi.testclient import TestClient

KEYS = ("BRAINTREE_MERCHANT_ID", "BRAINTREE_PUBLIC_KEY", "BRAINTREE_PRIVATE_KEY")


def _creds():
    vals = {k: os.getenv(k) for k in KEYS}
    f = pathlib.Path(__file__).resolve().parent.parent / ".env.braintree"
    if not all(vals.values()) and f.exists():
        for line in f.read_text().splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                vals.setdefault(k.strip(), None)
                if not vals.get(k.strip()):
                    vals[k.strip()] = v.strip().strip('"').strip("'")
    return vals


creds = _creds()
pytestmark = pytest.mark.skipif(not all(creds.get(k) for k in KEYS), reason="Braintree sandbox creds absent")


@pytest.fixture()
def client(monkeypatch):
    for k, v in creds.items():
        if v:
            monkeypatch.setenv(k, v)
    monkeypatch.setenv("BRAINTREE_ENVIRONMENT", "sandbox")
    import app.main as m
    from app.gateway import BraintreeGateway
    monkeypatch.setattr(m, "gateway", BraintreeGateway())
    return TestClient(m.app)


def test_sandbox_flow(client):
    assert client.get("/api/config").json()["gateway"] == "braintree"
    assert len(client.get("/api/client-token").json()["token"]) > 20
    card = client.post("/api/checkout", json={"amount": "10.00", "nonce": "fake-valid-nonce", "description": "it card"}).json()
    assert card["success"], card
    ref = client.post(f"/api/transactions/{card['transaction']['id']}/refund")
    assert ref.status_code == 200, ref.text
    assert ref.json()["transaction"]["status"] in ("voided", "submitted_for_settlement", "settling", "settled")
    big = client.post("/api/checkout", json={"amount": "750", "nonce": "fake-valid-nonce"}).json()
    assert big["status"] == "pending_approval" and "transaction" not in big
    print("\nIDS card=%s/%s refund=%s/%s" % (card["transaction"]["id"], card["transaction"]["status"],
          ref.json()["transaction"]["id"], ref.json()["transaction"]["status"]))


def test_sandbox_paypal(client):
    pp = client.post("/api/checkout", json={"amount": "15.00", "nonce": "fake-paypal-one-time-nonce", "description": "it paypal"}).json()
    if not pp["success"] and "PayPal Accounts are not accepted" in pp.get("error", ""):
        pytest.skip("PayPal not enabled on this sandbox merchant account (Settings > Processing)")
    assert pp["success"], pp
    assert pp["transaction"]["payment_type"] == "paypal_account"
    print("\nIDS paypal=%s/%s" % (pp["transaction"]["id"], pp["transaction"]["status"]))
