"""MCP로 근거를 모으고, 부족하면 시스템별로 runbook을 골라 한 번 더 조사한다 (Wave 2, 7, 9, 10b, 15)."""
import asyncio
import time

from langsmith import traceable

from ..deps import SupervisorDeps
from ..state import AgentState
from ..retry import with_retry
from ..runbook_select import group_by_type, keyword_classify, select_runbooks


@traceable(name="research_node")
async def research_node(state: AgentState, deps: SupervisorDeps) -> dict:
    """Wave 15 가드 + Wave 10b 병렬 + Wave 9 재시도 + Wave 2 Tool/Resource 실호출."""
    tasks = state["tasks"]
    research_indices = [i for i, t in enumerate(tasks)
                        if t["agent"] == "research" and t["status"] == "pending"]
    # Wave 15 — 객체가 아니라 '실행할 목록'을 검사한다 (not research_agent 버그의 수정판)
    if not research_indices:
        return {"events": state["events"] +
                [{"type": "ResearchSkipped", "reason": "pending research task 0개"}]}

    retry_total = state.get("retry_count", 0)

    # URI를 코드에 하드코딩하지 않는다. 여러 MCP 서버가 연결돼 있고 그중 어느 서버가
    # "get_incident_runbook" resource를 제공하는지 모른다고 가정하고, 먼저 연결된
    # 서버들을 조회(list)해 유효한 서버를 찾은 뒤에만 그 서버로 요청한다.
    needs_runbook = any(tasks[i]["kind"] != "monitoring" for i in research_indices)
    runbook_server = runbook_uri_template = None
    if needs_runbook:
        runbook_server, runbook_uri_template = await deps.mcp.find_resource_template(
            "get_incident_runbook")

    async def run_one(idx: int) -> dict:
        task = tasks[idx]
        await asyncio.sleep(0.35)   # 태스크당 고정 지연 — 병렬 효과를 실측하기 위함
        if task["kind"] == "monitoring":
            data, retries = await with_retry(
                deps.mcp.call_tool, "get_monitoring_status",
                {"system_name": task["target"]})
            return {"idx": idx, "source": "monitoring", "data": data, "retries": retries}
        uri = runbook_uri_template.format(incident_type=task["target"])
        data = await deps.mcp.read_resource(uri, server=runbook_server)
        return {"idx": idx, "source": "runbook", "data": data, "retries": 0}

    started = time.perf_counter()
    results = await asyncio.gather(*(run_one(i) for i in research_indices))
    elapsed = round(time.perf_counter() - started, 2)

    new_tasks = [dict(t) for t in tasks]
    collected = list(state.get("collected", []))
    events = list(state["events"])
    for r in results:
        new_tasks[r["idx"]]["status"] = "done"
        collected.append({"source": r["source"], "data": r["data"],
                          "systems": new_tasks[r["idx"]].get("systems", [])})
        retry_total += r["retries"]
        events.append({"type": "TaskCompleted", "id": new_tasks[r["idx"]]["id"],
                       "source": r["source"], "target": new_tasks[r["idx"]]["target"],
                       "retries": r["retries"]})
    return {"tasks": new_tasks, "collected": collected, "retry_count": retry_total,
            "parallel_elapsed": elapsed, "events": events}


def _classifier(deps: SupervisorDeps):
    index = getattr(deps, "runbook_index", None)
    return index.classify if index is not None else keyword_classify


@traceable(name="replan_check_node")
async def replan_check_node(state: AgentState, deps: SupervisorDeps) -> dict:
    """Wave 7 — 1차 수집 결과 평가. runbook이 없으면 시스템별로 유형을 골라 gap task를 추가해 재조사.

    유형 판단은 LLM에게 다시 묻지 않고 코드가 내린다(gate·validate와 같은 원칙).
    분기는 진행 로그의 마지막 줄이 아니라 research_sufficient 필드로 한다."""
    collected = state.get("collected", [])
    rounds = state.get("replan_rounds", 0)
    events = list(state["events"])
    has_runbook = any(c["source"] == "runbook" for c in collected)
    if has_runbook or rounds >= 1:
        events.append({"type": "ReplanCheck", "round": rounds, "is_sufficient": True})
        return {"research_sufficient": True, "events": events}

    started = time.perf_counter()
    picks, misses = select_runbooks(state["query"], collected, _classifier(deps))
    events.append({"type": "RunbookSelected", "picks": picks,
                   "ms": round((time.perf_counter() - started) * 1000, 1)})
    if misses:
        collected = collected + [{"source": "runbook_miss", "data": {"systems": misses}}]
        events.append({"type": "RunbookMiss", "systems": [m["system"] for m in misses]})

    by_type = group_by_type(picks)
    picked = {p["system"]: p["incident_type"] for p in picks}
    if not by_type:
        events.append({"type": "ReplanCheck", "round": rounds, "is_sufficient": True})
        return {"collected": collected, "runbook_picks": picked,
                "research_sufficient": True, "events": events}

    gap_tasks = [{"id": f"task-gap-runbook-{i}", "agent": "research", "kind": "runbook",
                  "target": incident_type, "systems": systems, "status": "pending"}
                 for i, (incident_type, systems) in enumerate(by_type.items())]
    events.append({"type": "ReplanCheck", "round": rounds, "is_sufficient": False,
                   "gap_tasks": [t["id"] for t in gap_tasks], "target": ", ".join(by_type)})
    return {"tasks": state["tasks"] + gap_tasks, "collected": collected,
            "replan_rounds": rounds + 1, "runbook_picks": picked,
            "research_sufficient": False, "events": events}
