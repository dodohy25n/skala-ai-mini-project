"""정상 시스템의 증상 질문이면 그 시스템이 의존하는 시스템을 모니터링 조사에 더한다."""
from langsmith import traceable

from ..dependency import expansion_origins
from ..deps import SupervisorDeps
from ..state import AgentState
from .research import _classifier


@traceable(name="dependency_node")
async def dependency_node(state: AgentState, deps: SupervisorDeps) -> dict:
    """첫 조사 직후 한 번만 실행됩니다.

    시스템이 정상인지는 모니터링 결과가 있어야 알 수 있어 plan 단계에서는 정할 수 없습니다. 그래서 첫 조사가
    끝난 뒤 코드 규칙(agent/dependency.py)으로 판단하고, 의존 목록은 MCP get_dependencies Tool에서 받습니다.
    이미 조사 중인 시스템은 다시 더하지 않습니다."""
    tasks = list(state["tasks"])
    collected = list(state.get("collected", []))
    events = list(state["events"])
    queued = {t["target"] for t in tasks if t.get("kind") == "monitoring"}
    for origin in expansion_origins(state["query"], collected, _classifier(deps)):
        try:
            data = await deps.mcp.call_tool("get_dependencies", {"system_name": origin})
        except Exception as e:
            events.append({"type": "DependencyLookupFailed", "system": origin,
                           "error": f"{type(e).__name__}: {e}"})
            continue
        new = [d for d in data.get("depends_on", []) if d not in queued]
        if not new:
            continue
        queued.update(new)
        tasks += [{"id": f"task-dep-{origin}-{d}", "agent": "research", "kind": "monitoring",
                   "target": d, "dependency_of": origin, "status": "pending"} for d in new]
        collected.append({"source": "dependency", "data": {"system": origin, "depends_on": new}})
        events.append({"type": "DependencyAdded", "system": origin, "depends_on": new})
    return {"tasks": tasks, "collected": collected, "dependency_checked": True, "events": events}
