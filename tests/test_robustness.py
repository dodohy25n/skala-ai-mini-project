import asyncio
import time

import pytest

from agent.mcp_client import MCPClient
from agent.nodes.research import research_node
from tests.conftest import FakeMCP, make_deps


def task(target):
    return {"id": f"task-{target}", "agent": "research", "kind": "monitoring", "target": target, "status": "pending"}


def test_one_failed_lookup_keeps_the_other_results():
    state = {"tasks": [task("web-01"), task("auth-service")], "events": []}
    out = asyncio.run(research_node(state, make_deps(mcp=FakeMCP(broken={"auth-service"}))))
    assert [c["data"]["system_name"] for c in out["collected"] if c["source"] == "monitoring"] == ["web-01"]
    failed = [e for e in out["events"] if e["type"] == "TaskFailed"]
    assert [e["target"] for e in failed] == ["auth-service"]
    assert {t["target"]: t["status"] for t in out["tasks"]} == {"web-01": "done", "auth-service": "failed"}
    assert out["collected"][-1] == {"source": "research_error",
                                    "data": {"system": "auth-service", "error": "ValueError: tool failed"},
                                    "systems": ["auth-service"]}


class HangingSession:
    async def call_tool(self, name, arguments):
        await asyncio.sleep(10)


def test_mcp_call_has_a_time_limit():
    client = MCPClient(flaky_first_tool_call=False, call_timeout_s=0.2)
    client.sessions = {"ops": HangingSession()}
    started = time.perf_counter()
    with pytest.raises(TimeoutError):
        asyncio.run(client.call_tool("get_monitoring_status", {"system_name": "web-01"}))
    assert time.perf_counter() - started < 1.0


def test_answer_says_which_lookup_failed_instead_of_inventing_numbers():
    from langgraph.checkpoint.memory import InMemorySaver
    from langgraph.types import Command

    from agent.graph import build_graph
    from agent.state import turn_input

    graph = build_graph(make_deps(mcp=FakeMCP(broken={"payments-service"}), checkpointer=InMemorySaver()))

    async def go():
        cfg = {"configurable": {"thread_id": "r"}, "recursion_limit": 40}
        r = await graph.ainvoke(turn_input("payments-service랑 cache-service 둘 다 장애 진단해줘", "r", []), cfg)
        while "__interrupt__" in r:
            r = await graph.ainvoke(Command(resume="approve"), cfg)
        return r

    result = asyncio.run(go())
    assert "payments-service: 조회에 실패해" in result["final"]
    assert "cache-service" in result["final"]
