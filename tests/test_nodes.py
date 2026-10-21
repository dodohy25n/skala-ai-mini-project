import asyncio

from agent.graph import _after_replan_check, gate_node, replan_check_node, research_node, validate_node
from tests.conftest import FakeIndex, FakeMCP, make_deps, monitoring


def task(target, agent="research", kind="monitoring"):
    return {"id": f"task-{target}", "agent": agent, "kind": kind, "target": target, "status": "pending"}


def run(coro):
    return asyncio.run(coro)


def test_gate_removes_analysis_task_when_not_needed():
    state = {"tasks": [task("web-01"), task(None, agent="analysis")], "needs_analysis": False, "events": []}
    out = run(gate_node(state, make_deps()))
    assert [t["agent"] for t in out["tasks"]] == ["research"]
    assert out["gate_removed"] == 1


def test_replan_adds_one_gap_task_per_runbook_type():
    state = {"query": "payments-service랑 cache-service 둘 다 저하", "tasks": [],
             "collected": monitoring("payments-service", "cache-service", "web-01"), "events": []}
    out = run(replan_check_node(state, make_deps()))
    targets = {t["target"]: t["systems"] for t in out["tasks"]}
    assert targets == {"cpu-spike": ["payments-service"], "memory-leak": ["cache-service"]}
    assert out["research_sufficient"] is False
    assert _after_replan_check({**state, **out}) == "research"


def test_replan_stops_after_one_round():
    state = {"query": "cache-service", "tasks": [], "collected": monitoring("cache-service"),
             "replan_rounds": 1, "events": []}
    out = run(replan_check_node(state, make_deps()))
    assert out["research_sufficient"] is True
    assert "tasks" not in out


def test_replan_records_miss_and_does_not_add_tasks():
    state = {"query": "web-01 배포 담당자가 누구야?", "tasks": [], "collected": monitoring("web-01"), "events": []}
    out = run(replan_check_node(state, make_deps(index=FakeIndex())))
    assert out["research_sufficient"] is True
    assert out["collected"][-1]["source"] == "runbook_miss"
    assert any(e["type"] == "RunbookMiss" for e in out["events"])


def test_branch_does_not_depend_on_last_event():
    state = {"research_sufficient": True, "events": [{"type": "RunbookMiss", "systems": ["web-01"]}]}
    assert _after_replan_check(state) == "analysis"


def test_research_attaches_systems_to_runbook_results():
    gap = {**task("cpu-spike", kind="runbook"), "systems": ["payments-service"]}
    out = run(research_node({"tasks": [gap], "events": []}, make_deps()))
    assert out["collected"][0]["source"] == "runbook"
    assert out["collected"][0]["systems"] == ["payments-service"]


def test_validate_gives_up_after_three_attempts():
    state = {"draft": "짧음", "validation_attempts": 2, "events": []}
    out = run(validate_node(state, make_deps()))
    assert out["validation_feedback"] is None
    assert "통과 처리" in out["events"][-1]["verdict"]


def test_fake_mcp_counts_calls():
    mcp = FakeMCP()
    run(research_node({"tasks": [task("web-01")], "events": []}, make_deps(mcp=mcp)))
    assert mcp.calls["call_tool"] == 1
