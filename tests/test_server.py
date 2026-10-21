import json

import pytest
from fastapi.testclient import TestClient

QUESTION = "cache-service 메모리 장애 진단해줘"


@pytest.fixture(scope="module")
def client():
    mp = pytest.MonkeyPatch()
    mp.setenv("RUNBOOK_INDEX", "off")
    mp.setenv("LLM_BACKEND", "fake")
    import server
    mp.setattr(server.SqliteSessionStore.__init__, "__defaults__", (":memory:",))
    with TestClient(server.app) as c:
        yield c
    mp.undo()


def sse_events(text):
    out = []
    for block in text.split("\n\n"):
        if block.startswith("data: ") and block != "data: [DONE]":
            out.append(json.loads(block[len("data: "):]))
    return out


def test_chat_returns_approval_required_then_resume_finishes(client):
    r = client.post("/chat", json={"message": QUESTION, "thread_id": "s1"}).json()
    assert r["status"] == "approval_required"
    assert [a["action"] for a in r["approval"]["actions"]] == ["프로세스 재시작"]

    blocked = client.post("/chat", json={"message": "다른 질문 진단해줘", "thread_id": "s1"})
    assert blocked.status_code == 409

    done = client.post("/chat/resume", json={"thread_id": "s1", "decision": "approve"}).json()
    assert done["status"] == "done"
    assert "재시작" in done["reply"]


def test_resume_without_pending_run_is_409(client):
    r = client.post("/chat/resume", json={"thread_id": "nobody", "decision": "approve"})
    assert r.status_code == 409


def test_resume_rejects_unknown_decision(client):
    r = client.post("/chat/resume", json={"thread_id": "s1", "decision": "maybe"})
    assert r.status_code == 422


def test_stream_pauses_and_resume_stream_continues(client):
    first = client.get("/chat/stream", params={"message": QUESTION, "thread_id": "s2"})
    events = sse_events(first.text)
    assert events[-1]["type"] == "ApprovalRequired"
    assert not any(e["type"] == "FinalAnswer" for e in events)
    assert first.text.rstrip().endswith("data: [DONE]")

    second = client.post("/chat/resume/stream", json={"thread_id": "s2", "decision": "reject"})
    events = sse_events(second.text)
    types = [e["type"] for e in events]
    assert "ApprovalDecided" in types and types[-1] == "FinalAnswer"
    assert "PlanCreated" not in types
    assert "재시작" not in events[-1]["reply"]
