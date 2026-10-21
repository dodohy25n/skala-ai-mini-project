import asyncio

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from agent.graph import build_graph
from tests.conftest import FakeIndex, FakeMCP, make_deps


def first_input(query, thread_id="t"):
    return {"query": query, "thread_id": thread_id, "history": [], "events": [], "cost_usd": 0.0}


def run_until_done(graph, query, decisions, thread_id="t"):
    """승인 요청이 올 때마다 decisions에서 하나씩 꺼내 재개한다."""
    async def go():
        cfg = {"configurable": {"thread_id": thread_id}, "recursion_limit": 40}
        result = await graph.ainvoke(first_input(query, thread_id), cfg)
        asked = []
        for decision in decisions:
            if "__interrupt__" not in result:
                break
            asked.append([a["action"] for a in result["__interrupt__"][0].value["actions"]])
            result = await graph.ainvoke(Command(resume=decision), cfg)
        return result, asked
    return asyncio.run(go())


def graph_with(mcp=None):
    return build_graph(make_deps(mcp=mcp or FakeMCP(), index=FakeIndex(), checkpointer=InMemorySaver()))


def decided(result):
    return [(e["decision"], e.get("next")) for e in result["events"] if e["type"] == "ApprovalDecided"]


def test_answer_without_risky_action_is_not_paused():
    result, asked = run_until_done(graph_with(), "web-01 배포 담당자가 누구야? 진단해줘", [])
    assert "__interrupt__" not in result
    assert "매뉴얼을 찾지 못했습니다" in result["final"]


def test_risky_answer_waits_for_approval():
    result, asked = run_until_done(graph_with(), "cache-service 메모리 장애 진단해줘", [])
    payload = result["__interrupt__"][0].value
    assert payload["type"] == "ApprovalRequired"
    assert [a["action"] for a in payload["actions"]] == ["프로세스 재시작"]
    assert "final" not in result


def test_approve_keeps_action_and_records_it():
    result, asked = run_until_done(graph_with(), "cache-service 메모리 장애 진단해줘", ["approve"])
    assert "재시작" in result["final"]
    assert result["approved_actions"] == ["프로세스 재시작"]


def test_first_reject_rewrites_without_the_action():
    result, asked = run_until_done(graph_with(), "cache-service 메모리 장애 진단해줘", ["reject"])
    assert "재시작" not in result["final"]
    assert decided(result) == [("reject", "rewrite")]


def test_second_reject_strips_instead_of_rewriting_again():
    result, asked = run_until_done(graph_with(), "payments-service CPU 급증 진단해줘", ["reject", "reject", "reject"])
    assert asked == [["인스턴스 증설"], ["배포 롤백"]]
    assert decided(result) == [("reject", "rewrite"), ("reject", "strip")]
    assert "롤백" not in result["final"] and "한 대 더" not in result["final"]


def test_resume_does_not_repeat_tool_calls():
    mcp = FakeMCP()
    graph = graph_with(mcp)

    async def go():
        cfg = {"configurable": {"thread_id": "t"}, "recursion_limit": 40}
        await graph.ainvoke(first_input("cache-service 메모리 장애 진단해줘"), cfg)
        before = dict(mcp.calls)
        await graph.ainvoke(Command(resume="approve"), cfg)
        return before, dict(mcp.calls)

    before, after = asyncio.run(go())
    assert before == after
