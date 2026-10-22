import asyncio
import importlib

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from agent.dependency import dependency_reason, expansion_origins
from agent.fake_llm import QualityLLM
from agent.graph import _after_dependency, _after_research, build_graph
from agent.mcp_client import MCPClient
from agent.nodes.dependency import dependency_node
from agent.runbook_select import select_runbooks
from agent.state import turn_input
from tests.conftest import DEPENDENCIES, MONITORING, FakeIndex, FakeMCP, make_deps, monitoring


def test_dependency_data_covers_exactly_the_monitored_systems():
    assert set(DEPENDENCIES) == set(MONITORING)
    for system, depends_on in DEPENDENCIES.items():
        assert set(depends_on) <= set(MONITORING)
        assert system not in depends_on


def test_get_dependencies_tool_through_real_mcp_server():
    async def go():
        client = MCPClient(flaky_first_tool_call=False)
        await client.start()
        try:
            return [await client.call_tool("get_dependencies", {"system_name": s})
                    for s in ("web-01", "gateway-service", "billing-service")]
        finally:
            await client.stop()

    assert asyncio.run(go()) == [
        {"system_name": "web-01", "depends_on": ["auth-service"]},
        {"system_name": "gateway-service", "depends_on": ["payments-service"]},
        {"system_name": "billing-service", "depends_on": []},
    ]


def test_fake_mcp_answers_get_dependencies_and_counts_the_call():
    mcp = FakeMCP()
    data = asyncio.run(mcp.call_tool("get_dependencies", {"system_name": "payments-service"}))
    assert data == {"system_name": "payments-service", "depends_on": ["cache-service"]}
    assert mcp.calls["call_tool"] == 1


CASCADE = "web-01 로그인하면 에러 페이지가 떠요"
WEB_TO_AUTH = {"source": "dependency", "data": {"system": "web-01", "depends_on": ["auth-service"]}}


def classify(text):
    return FakeIndex().classify(text)


def monitor_task(target):
    return {"id": f"task-{target}", "agent": "research", "kind": "monitoring", "target": target, "status": "done"}


def test_healthy_named_system_with_unmatched_symptom_expands():
    assert expansion_origins(CASCADE, monitoring("web-01"), classify) == ["web-01"]


@pytest.mark.parametrize("query", ["web-01 서버 정상인지 확인해줘", "web-01 오늘 점검 일정 있어?",
                                   "web-01 배포 담당자가 누구야?", "web-01 크론 작업 시간대 설정 알려줘"])
def test_no_symptom_means_no_dependency(query):
    assert expansion_origins(query, monitoring("web-01"), classify) == []


def test_degraded_system_means_no_dependency():
    query = "payments-service 결제가 자꾸 실패해요"
    assert expansion_origins(query, monitoring("payments-service", "web-01"), classify) == []


@pytest.mark.parametrize("query", ["web-01 응답이 느려졌어요", "web-01 DB 연결이 안 돼요"])
def test_symptom_with_its_own_runbook_means_no_dependency(query):
    assert expansion_origins(query, monitoring("web-01"), classify) == []


def test_healthy_system_not_named_in_question_is_not_expanded():
    assert expansion_origins("로그인하면 에러가 나요", monitoring("auth-service", "web-01"), classify) == []


def run_node(query, collected, mcp):
    state = {"query": query, "tasks": [monitor_task(c["data"]["system_name"]) for c in collected],
             "collected": collected, "events": []}
    return state, asyncio.run(dependency_node(state, make_deps(mcp=mcp, index=FakeIndex())))


def test_dependency_node_adds_monitoring_task_and_reason_entry():
    mcp = FakeMCP()
    state, out = run_node(CASCADE, monitoring("web-01"), mcp)
    added = [t for t in out["tasks"] if t.get("dependency_of")]
    assert [(t["target"], t["kind"], t["dependency_of"], t["status"]) for t in added] == [
        ("auth-service", "monitoring", "web-01", "pending")]
    assert out["collected"][-1] == WEB_TO_AUTH
    assert out["events"][-1] == {"type": "DependencyAdded", "system": "web-01", "depends_on": ["auth-service"]}
    assert mcp.calls["call_tool"] == 1
    assert _after_dependency({**state, **out}) == "research"
    assert _after_research({**state, **out}) == "replan_check"


def test_dependency_node_without_expansion_makes_no_mcp_call():
    mcp = FakeMCP()
    state, out = run_node("web-01 배포 담당자가 누구야?", monitoring("web-01"), mcp)
    assert out["dependency_checked"] is True
    assert out["tasks"] == state["tasks"] and out["events"] == []
    assert mcp.calls["call_tool"] == 0
    assert _after_dependency({**state, **out}) == "replan_check"


def test_dependency_already_under_research_is_not_added_twice():
    _, out = run_node("web-01 에러가 나요, auth-service도 같이 봐줘", monitoring("web-01", "auth-service"), FakeMCP())
    assert not any(t.get("dependency_of") for t in out["tasks"])
    assert out["events"] == []


def test_failed_dependency_lookup_is_recorded_and_skipped():
    _, out = run_node(CASCADE, monitoring("web-01"), FakeMCP(broken={"web-01"}))
    assert not any(t.get("dependency_of") for t in out["tasks"])
    assert out["events"][-1]["type"] == "DependencyLookupFailed"


def test_added_system_is_judged_by_its_own_alerts_with_dependency_basis():
    picks, misses = select_runbooks(CASCADE, monitoring("web-01", "auth-service") + [WEB_TO_AUTH], classify)
    assert picks == [{"system": "auth-service", "incident_type": "high-latency", "score": 1.0, "basis": "dependency"}]
    assert misses == []


def test_origin_miss_stays_when_dependency_explains_nothing():
    healthy_dep = {"source": "dependency", "data": {"system": "web-01", "depends_on": ["log-service"]}}
    collected = monitoring("web-01") + [{"source": "monitoring", "data": {
        "system_name": "log-service", "status": "healthy", "alerts": []}}, healthy_dep]
    picks, misses = select_runbooks(CASCADE, collected, classify)
    assert picks == []
    assert [m["system"] for m in misses] == ["web-01"]


def test_reason_names_the_degraded_dependency():
    reason = dependency_reason(WEB_TO_AUTH, monitoring("web-01", "auth-service"))
    assert reason.startswith("web-01 지표는 정상이지만 의존 대상인 auth-service 지표가 저하 상태")


def test_reason_when_dependency_is_healthy_or_unchecked():
    entry = {"source": "dependency", "data": {"system": "x-service", "depends_on": ["web-01"]}}
    assert "web-01 지표도 정상이라 원인을 좁히지 못했습니다" in dependency_reason(entry, monitoring("web-01"))
    assert "상태는 확인하지 못했습니다" in dependency_reason(entry, [])


def test_fake_writer_states_the_reason():
    collected = monitoring("web-01", "auth-service") + [WEB_TO_AUTH]
    draft, _ = asyncio.run(QualityLLM("q", 0.005).write(CASCADE, collected, 2, None))
    assert "2) 의존 관계: web-01 지표는 정상이지만 의존 대상인 auth-service 지표가 저하 상태" in draft


@pytest.mark.parametrize("module, cls, method", [("agent.ollama_llm", "OllamaQualityLLM", "_chat_stream"),
                                                 ("agent.openai_llm", "OpenAIQualityLLM", "_chat")])
def test_real_model_writers_pass_the_reason_as_a_fact(module, cls, method):
    llm = getattr(importlib.import_module(module), cls)("m", 0.005)
    seen = {}

    def capture(system, user, *args, **kwargs):
        seen["user"] = user
        return user, {}

    async def capture_async(system, user, *args, **kwargs):
        return capture(system, user)

    setattr(llm, method, capture_async if method == "_chat_stream" else capture)
    asyncio.run(llm.write(CASCADE, monitoring("web-01", "auth-service") + [WEB_TO_AUTH], 1, None))
    assert "- 의존 관계: web-01 지표는 정상이지만 의존 대상인 auth-service 지표가 저하 상태" in seen["user"]


def run_graph(query, mcp):
    deps = make_deps(mcp=mcp, index=FakeIndex(), checkpointer=InMemorySaver())
    deps.fast_llm.route = lambda q: ({"route": "incident", "needs_analysis": False}, {"usd": 0.0})
    graph = build_graph(deps)

    async def go():
        cfg = {"configurable": {"thread_id": "dep"}, "recursion_limit": 40}
        result = await graph.ainvoke(turn_input(query, "dep", []), cfg)
        while "__interrupt__" in result:
            result = await graph.ainvoke(Command(resume="approve"), cfg)
        return result

    return asyncio.run(go())


def test_cascade_question_reaches_the_dependency_runbook_end_to_end():
    mcp = FakeMCP()
    result = run_graph(CASCADE, mcp)
    picks = [p for e in result["events"] if e["type"] == "RunbookSelected" for p in e["picks"]]
    assert [(p["system"], p["incident_type"], p["basis"]) for p in picks] == [
        ("auth-service", "high-latency", "dependency")]
    assert "web-01 지표는 정상이지만 의존 대상인 auth-service 지표가 저하" in result["final"]
    assert "매뉴얼을 찾지 못했습니다" not in result["final"]
    assert mcp.calls["call_tool"] == 3


def test_own_runbook_question_runs_without_dependency_step_end_to_end():
    mcp = FakeMCP()
    result = run_graph("web-01 응답이 느려졌어요", mcp)
    assert not any(e["type"] == "DependencyAdded" for e in result["events"])
    assert "의존 관계" not in result["final"]
    assert mcp.calls["call_tool"] == 1
