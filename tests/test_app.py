import os
for k in ("BRAINTREE_PUBLIC_KEY", "BRAINTREE_PRIVATE_KEY"):
    os.environ.pop(k, None)
os.environ["APPROVAL_THRESHOLD"] = "500"
os.environ["LLM_PROVIDER"] = "mock"
from fastapi.testclient import TestClient
from app.main import app

c = TestClient(app)


def test_uses_mock_without_keys():
    assert c.get("/api/config").json()["gateway"] == "mock"


def test_plan():
    p = c.post("/api/plan", json={"origin": "Lahore", "destination": "Berlin", "budget": 3000}).json()
    assert len(p["payments"]) == 3


def test_small_payment_and_refund():
    r = c.post("/api/checkout", json={"amount": "100", "nonce": "fake-paypal-nonce"}).json()
    assert r["success"]
    tid = r["transaction"]["id"]
    assert any(t["id"] == tid for t in c.get("/api/transactions").json())
    assert c.post(f"/api/transactions/{tid}/refund").json()["transaction"]["status"] == "voided"


def test_large_payment_needs_approval():
    r = c.post("/api/checkout", json={"amount": "900", "nonce": "x"}).json()
    assert r["status"] == "pending_approval"
    assert c.post(f"/api/approvals/{r['approval_id']}/approve").json()["success"]


def test_reject_and_invalid():
    r = c.post("/api/checkout", json={"amount": "900", "nonce": "x"}).json()
    assert c.post(f"/api/approvals/{r['approval_id']}/reject").json()["status"] == "rejected"
    assert c.post("/api/checkout", json={"amount": "-1", "nonce": "x"}).status_code == 400
