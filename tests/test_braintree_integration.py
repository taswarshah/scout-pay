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


@pytest.fixture()
def bt_agent(monkeypatch):
    for k, v in creds.items():
        if v:
            monkeypatch.setenv(k, v)
    monkeypatch.setenv("BRAINTREE_ENVIRONMENT", "sandbox")
    monkeypatch.setenv("LLM_PROVIDER", "mock")
    from app.agent import Agent
    from app.gateway import BraintreeGateway
    from app.store import SQLiteRepo
    return Agent(SQLiteRepo(":memory:"), BraintreeGateway())


def test_sandbox_agent_end_to_end(bt_agent):
    ag, ids = bt_agent, {}
    # 1) small cart, card nonce, confirm -> charged
    ag.chat("it1", "I need movers, a visa fee and internet")
    r = ag.chat("it1", "pay", nonce="fake-valid-nonce")
    assert r["proposal"]["status"] == "awaiting_confirmation"
    r = ag.chat("it1", "confirm")
    assert r["proposal"]["status"] == "paid", r["reply"]
    ids["small"] = r["last_txn"]
    assert ag.chat("it1", "status")["tool_calls"][0]["result"]["transaction"]["status"] in ("submitted_for_settlement", "settling", "settled")
    # idempotent replay: no second charge
    assert ag.run_tool("it1", "execute_payment", {"proposal_id": r["proposal"]["id"]})["idempotent_replay"]
    r = ag.chat("it1", "refund")
    assert r["tool_calls"][0]["result"]["transaction"]["status"] == "voided"
    # 2) over threshold with vaulted card -> needs approver
    ag.set_payment_method("it2", nonce="fake-valid-nonce", vault=True)
    assert ag.state("it2")["token"]
    ag.chat("it2", "add rental deposit")
    pid = ag.chat("it2", "pay")["proposal"]["id"]
    assert ag.chat("it2", "confirm")["proposal"]["status"] == "awaiting_approval"
    ag.decide(pid, "approve")
    r = ag.chat("it2", "pay")
    assert r["proposal"]["status"] == "paid", r["reply"]
    ids["big_vaulted"] = r["last_txn"]
    ids["big_void"] = ag.chat("it2", "refund")["tool_calls"][0]["result"]["transaction"]["status"]
    # 3) PayPal nonce: either works or degrades to a card hint
    ag.chat("it3", "add sim card"); ag.chat("it3", "pay")
    r = ag.chat("it3", "confirm", nonce="fake-paypal-one-time-nonce")
    if r["proposal"]["status"] != "paid":
        assert "card" in r["reply"]
        r = ag.chat("it3", "pay", nonce="fake-valid-nonce")
        assert r["proposal"]["status"] == "paid"
        ids["paypal"] = "rejected->card"
    ids["sim"] = r["last_txn"]
    ag.chat("it3", "refund")
    print("\nAGENT IDS", ids)
