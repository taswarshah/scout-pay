import os
os.environ["LLM_PROVIDER"] = "mock"
import pytest
from fastapi.testclient import TestClient
from app.agent import Agent
from app.gateway import MockGateway
from app.store import SQLiteRepo


@pytest.fixture()
def ag(monkeypatch):
    for k, v in {"APPROVAL_THRESHOLD": "500", "MAX_SINGLE_PAYMENT": "2000", "DAILY_LIMIT": "3000"}.items():
        monkeypatch.setenv(k, v)
    return Agent(SQLiteRepo(":memory:"), MockGateway())


def events(ag, sid):
    return [e["event"] for e in reversed(ag.repo.audit_log(500, sid))]


def test_search_add_propose_confirm_pay_status_refund(ag):
    r = ag.chat("s1", "I need movers, a visa fee and internet")
    assert {i["service_id"] for i in r["cart"]["items"]} == {"mov-std", "visa-fee", "util-net"}
    assert r["cart"]["total"] == "330.00"
    r = ag.chat("s1", "pay")
    p = r["proposal"]; assert p["status"] == "awaiting_confirmation"
    assert not ag.gw.txns, "nothing charged before confirmation"
    r = ag.chat("s1", "confirm")
    assert r["proposal"]["status"] == "paid" and r["last_txn"]
    tid = r["last_txn"]
    assert "status" in ag.chat("s1", "status")["reply"].lower() or tid in ag.chat("s1", "status")["reply"]
    r = ag.chat("s1", f"refund {tid}")
    assert "voided" in r["reply"]
    ev = events(ag, "s1")
    for e in ("proposal_created", "proposal_confirmed", "payment_succeeded", "refund_succeeded"):
        assert e in ev


def test_llm_cannot_execute_without_confirmation(ag):
    ag.chat("s2", "add visa fee")
    pid = ag.run_tool("s2", "propose_payment", {})["proposal_id"]
    res = ag.run_tool("s2", "execute_payment", {"proposal_id": pid})
    assert "not confirmed" in res["error"] and not ag.gw.txns
    assert "execute_blocked" in events(ag, "s2")


def test_over_threshold_needs_approver(ag):
    ag.chat("s3", "add rental deposit")
    r = ag.chat("s3", "pay"); pid = r["proposal"]["id"]
    assert r["proposal"]["needs_approval"]
    r = ag.chat("s3", "confirm")
    assert r["proposal"]["status"] == "awaiting_approval" and not ag.gw.txns
    assert "error" in ag.run_tool("s3", "execute_payment", {"proposal_id": pid})
    assert ag.decide(pid, "approve")["status"] == "ready"
    r = ag.chat("s3", "pay")
    assert r["proposal"]["status"] == "paid" and len(ag.gw.txns) == 1


def test_reject_and_cancel(ag):
    ag.chat("s4", "add rental deposit"); pid = ag.chat("s4", "pay")["proposal"]["id"]
    ag.chat("s4", "confirm"); ag.decide(pid, "reject")
    assert ag.repo.get_proposal(pid)["status"] == "rejected"
    ag.chat("s5", "add sim card"); ag.chat("s5", "pay")
    assert ag.chat("s5", "cancel")["proposal"]["status"] == "cancelled" and not ag.gw.txns


def test_idempotent_execute(ag):
    ag.chat("s6", "add airport pickup"); ag.chat("s6", "pay"); r = ag.chat("s6", "confirm")
    again = ag.run_tool("s6", "execute_payment", {"proposal_id": r["proposal"]["id"]})
    assert again["idempotent_replay"] and len(ag.gw.txns) == 1


def test_spending_limits(ag, monkeypatch):
    monkeypatch.setenv("MAX_SINGLE_PAYMENT", "100")
    ag.chat("s7", "add movers")
    assert "per-payment limit" in ag.chat("s7", "pay")["reply"]
    monkeypatch.setenv("MAX_SINGLE_PAYMENT", "2000"); monkeypatch.setenv("DAILY_LIMIT", "200")
    ag.chat("s7", "pay"); ag.chat("s7", "confirm")  # 150 spent
    ag.chat("s7", "add visa fee")
    assert "24h spending limit" in ag.chat("s7", "pay")["reply"]


def test_paypal_disabled_degrades(ag, monkeypatch):
    monkeypatch.setenv("MOCK_PAYPAL_DISABLED", "1")
    ag.chat("s8", "add sim card"); ag.chat("s8", "pay")
    r = ag.chat("s8", "confirm", nonce="fake-paypal-one-time-nonce")
    assert "pay by card" in r["reply"] and r["proposal"]["status"] == "ready"
    r = ag.chat("s8", "pay", nonce="fake-valid-nonce")
    assert r["proposal"]["status"] == "paid"


def test_cannot_refund_foreign_txn(ag):
    tid = ag.gw.sale("5", "fake-valid-nonce")["transaction"]["id"]
    assert "not found" in ag.run_tool("s9", "refund_payment", {"transaction_id": tid})["error"]


def test_vaulted_token(ag):
    ag.set_payment_method("s10", nonce="fake-valid-nonce", vault=True)
    assert ag.state("s10")["token"].startswith("mock-token")


def test_chat_api_end_to_end():
    import app.main as m
    m.gateway = MockGateway()
    c = TestClient(m.app)
    r = c.post("/api/chat", json={"message": "I need movers"}).json(); sid = r["session_id"]
    r = c.post("/api/chat", json={"session_id": sid, "message": "pay"}).json()
    pid = r["proposal"]["id"]
    assert c.post(f"/api/proposals/{pid}/confirm", json={"session_id": sid}).json()["status"] == "ready"
    r = c.post("/api/chat", json={"session_id": sid, "message": "pay"}).json()
    assert r["proposal"]["status"] == "paid"
    assert any(e["event"] == "payment_succeeded" for e in c.get(f"/api/audit?session_id={sid}").json())


def test_checkout_idempotency_header():
    import app.main as m
    m.gateway = MockGateway()
    c = TestClient(m.app)
    h = {"Idempotency-Key": "abc123"}
    a = c.post("/api/checkout", json={"amount": "20", "nonce": "fake-valid-nonce"}, headers=h).json()
    b = c.post("/api/checkout", json={"amount": "20", "nonce": "fake-valid-nonce"}, headers=h).json()
    assert b["idempotent_replay"] and a["transaction"]["id"] == b["transaction"]["id"]
