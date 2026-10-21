"""
그래프 - 질문 분류부터 조사·작성·검증까지의 순서를 정합니다. 단계와 분기를 한곳에서 볼 수 있게 합니다.

진단 순서의 중심입니다. 분류 → 계획 → 조사 → 작성 → 검증 노드와 분기를 정의합니다.
"""

"""v2 Supervisor — 실제 langgraph.StateGraph (Wave 4·5·6·7·8·10b·15 통합).

route → (chat 직행 | incident 파이프라인)
incident: plan(MCP Prompt 절차 반영) → gate(권위 게이트) → research(가드+병렬+재시도)
          → replan_check(부족 시 gap task 후 재조사) → analysis(needs_analysis=True일 때만
          실제 실행) → write → validate(반려 시 재작성) → finalize

모든 노드는 SupervisorDeps를 functools.partial로 주입받는 순수 함수다(Wave 13).
각 노드는 @traceable로 LangSmith에 계층 트리로 기록된다.
"""
import asyncio
import time
from functools import partial

from langgraph.config import get_stream_writer
from langgraph.graph import END, StateGraph
from langgraph.types import interrupt
from langsmith import traceable

from .deps import SupervisorDeps
from .retry import with_retry
from .runbook_select import group_by_type, keyword_classify, select_runbooks
from .safety import find_risky_actions, strip_actions
from .state import AgentState
from .systems import mentioned_systems


def _cost(state: AgentState, usage: dict) -> float:
    return round(state.get("cost_usd", 0.0) + usage["usd"], 6)


# 질문을 어느 길로 보낼지 결정하는 자리
@traceable(name="route_node")
async def route_node(state: AgentState, deps: SupervisorDeps) -> dict:
    """Wave 5 — 모든 쿼리가 무거운 파이프라인을 탈 필요는 없다."""
    result, usage = deps.fast_llm.route(state["query"])
    event = {"type": "RouteDecided", "route": result["route"]}
    return {"route": result["route"], "needs_analysis": result["needs_analysis"],
            "cost_usd": _cost(state, usage),
            "events": state.get("events", []) + [event]}


@traceable(name="chat_answer_node")
async def chat_answer_node(state: AgentState, deps: SupervisorDeps) -> dict:
    """Wave 5 direct 경로 — 히스토리 기반 응답 (Wave 12 세션 증명 지점).

    토큰 단위 스트리밍 — get_stream_writer()는 /chat/stream처럼 custom 모드로 소비될
    때만 실제로 전송되고, /chat(ainvoke)처럼 스트리밍하지 않는 호출에서는 자동으로
    no-op이 된다."""
    writer = get_stream_writer()
    reply, usage = await deps.fast_llm.chat(
        state["query"], state.get("history", []),
        on_token=lambda piece: writer({"type": "AnswerToken", "delta": piece}))
    return {"final": reply, "last_prompt": usage.get("prompt"),
            "cost_usd": _cost(state, usage),
            "events": state["events"] + [{"type": "Finalize", "pipeline": "chat-direct"}]}


@traceable(name="plan_node")
async def plan_node(state: AgentState, deps: SupervisorDeps) -> dict:
    """Wave 4 — Plan/Task 분해. Wave 2의 MCP Prompt가 표준 절차를 깔아준다.

    resource와 마찬가지로 이름을 어느 서버가 들고 있는지 미리 가정하지 않고,
    find_prompt()로 연결된 서버들을 먼저 조회해 찾은 뒤에만 get_prompt를 보낸다."""
    prompt_server = await deps.mcp.find_prompt("incident_analysis_prompt")
    procedure = await deps.mcp.get_prompt(
        "incident_analysis_prompt",
        {"system_name": (mentioned_systems(state["query"]) or ["(질문에 시스템 이름 없음)"])[0],
         "symptom": state["query"]},
        server=prompt_server,
    )
    result, usage = deps.fast_llm.plan(state["query"], procedure)
    targets = [t["target"] for t in result["tasks"]
               if t["agent"] == "research" and t.get("target")]
    event = {"type": "PlanCreated", "task_count": len(result["tasks"]),
             "targets": targets}
    return {"tasks": result["tasks"], "needs_analysis": result["needs_analysis"],
            "cost_usd": _cost(state, usage), "events": state["events"] + [event]}


@traceable(name="gate_node")
async def gate_node(state: AgentState, deps: SupervisorDeps) -> dict:
    """Wave 6 — 권위 게이트. needs_analysis=False인데 섞여 들어온 analysis task를
    프롬프트가 아니라 코드로 강제 제거한다."""
    tasks = state["tasks"]
    if not state.get("needs_analysis", False):
        kept = [t for t in tasks if t["agent"] != "analysis"]
        removed = len(tasks) - len(kept)
    else:
        kept, removed = tasks, 0
    event = {"type": "GateApplied", "removed_analysis_tasks": removed}
    return {"tasks": kept, "gate_removed": removed,
            "events": state["events"] + [event]}


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


@traceable(name="analysis_node")
async def analysis_node(state: AgentState, deps: SupervisorDeps) -> dict:
    """Wave 6 보완 — needs_analysis=True가 정당한 경우(모델의 자기모순이 아니라
    실제로 패턴 비교가 필요하다고 판단한 경우)를 위한 실행 경로. gate가 살려둔
    agent=='analysis' task가 있을 때만 수집된 monitoring 결과들을 비교 분석한다."""
    tasks = state["tasks"]
    pending = [t for t in tasks if t["agent"] == "analysis" and t["status"] == "pending"]
    if not pending:
        return {"events": state["events"] +
                [{"type": "AnalysisSkipped", "reason": "analysis task 0개"}]}
    summary, usage = deps.fast_llm.analyze(state.get("collected", []))
    new_tasks = [dict(t) for t in tasks]
    for t in new_tasks:
        if t["agent"] == "analysis" and t["status"] == "pending":
            t["status"] = "done"
    collected = state.get("collected", []) + [{"source": "analysis", "data": summary}]
    event = {"type": "AnalysisCompleted", "length": len(summary)}
    return {"tasks": new_tasks, "collected": collected,
            "cost_usd": _cost(state, usage), "events": state["events"] + [event]}


@traceable(name="write_node")
async def write_node(state: AgentState, deps: SupervisorDeps) -> dict:
    """Wave 8/10a — 고품질 모델로 초안 작성 (1차는 의도적 저품질).

    토큰마다 attempt 번호를 같이 실어 보낸다 — validate가 반려해 재작성(attempt 증가)
    될 때 프론트가 새 말풍선으로 끊어서 보여줄 수 있게 하기 위함(Wave 8 재작성 루프를
    그대로 노출)."""
    attempt = state.get("validation_attempts", 0) + 1
    feedback = "\n".join(f for f in (state.get("validation_feedback"),
                                      state.get("approval_feedback")) if f) or None
    writer = get_stream_writer()
    draft, usage = await deps.quality_llm.write(
        state["query"], state.get("collected", []),
        attempt, feedback,
        on_token=lambda piece: writer(
            {"type": "AnswerToken", "attempt": attempt, "delta": piece}))
    return {"draft": draft, "last_prompt": usage.get("prompt"),
            "cost_usd": _cost(state, usage),
            "events": state["events"] + [{"type": "DraftWritten", "attempt": attempt,
                                          "length": len(draft)}]}


@traceable(name="validate_node")
async def validate_node(state: AgentState, deps: SupervisorDeps) -> dict:
    """Wave 8 — 코드 기준 검증: 30자 이상 + 절차 번호 포함이어야 합격."""
    draft = state["draft"]
    attempts = state.get("validation_attempts", 0) + 1
    ok = len(draft) >= 30 and "1)" in draft
    verdict = "합격" if ok else "불합격"
    if not ok and attempts >= 3:   # 실모델 무한 재작성 방지 — 최대 3회 후 통과 처리
        ok, verdict = True, "합격(최대 시도 도달 — 통과 처리)"
    feedback = None if ok else (
        f"응답이 너무 짧습니다({len(draft)}자, 최소 30자). 절차를 1) 2) 3) 번호로 "
        f"구체적으로 설명하세요.")
    event = {"type": "Validation", "attempt": attempts, "verdict": verdict}
    return {"validation_attempts": attempts, "validation_feedback": feedback,
            "events": state["events"] + [event]}


@traceable(name="approve_node")
async def approve_node(state: AgentState, deps: SupervisorDeps) -> dict:
    """위험 조치가 든 초안은 운영자 결정을 받고 나서 내보낸다.

    interrupt()로 실행을 멈추면 체크포인터가 State를 저장하고, 같은 thread_id로
    Command(resume=결정)를 보내면 이 노드가 처음부터 다시 실행된다. 그래서 interrupt 앞에는
    MCP·LLM 호출 없이 순수 함수만 둔다. 승인은 조치 "종류" 단위로 기억해 재작성본에 같은
    종류가 다시 나와도 두 번 묻지 않는다."""
    approved = set(state.get("approved_actions", []))
    rejected = list(state.get("rejected_actions", []))
    draft = strip_actions(state["draft"], rejected) if rejected else state["draft"]
    found = find_risky_actions(draft)
    pending = sorted({f["action"] for f in found} - approved)
    events = list(state["events"])
    if not pending:
        events.append({"type": "ApprovalSkipped",
                       "reason": "이미 승인된 조치" if found else "위험 조치 없음"})
        return {"draft": draft, "approval": "approved" if found else "not_required",
                "rewrite_requested": False, "events": events}

    decision = interrupt({"type": "ApprovalRequired", "draft": draft,
                          "actions": [f for f in found if f["action"] in pending]})

    if decision == "approve":
        approved |= set(pending)
        events.append({"type": "ApprovalDecided", "decision": "approve", "actions": pending})
        return {"draft": draft, "approval": "approved", "approved_actions": sorted(approved),
                "rewrite_requested": False, "events": events}

    rejected += [a for a in pending if a not in rejected]
    if state.get("approval_rewrites", 0) < 1:
        events.append({"type": "ApprovalDecided", "decision": "reject", "actions": pending,
                       "next": "rewrite"})
        feedback = (f"운영자가 다음 조치를 거절했습니다: {', '.join(rejected)}. "
                    f"이 조치를 빼고 다른 절차로 다시 작성하세요.")
        return {"draft": draft, "approval": "rejected", "rejected_actions": rejected,
                "approval_feedback": feedback, "approval_rewrites": 1,
                "rewrite_requested": True, "events": events}
    events.append({"type": "ApprovalDecided", "decision": "reject", "actions": pending,
                   "next": "strip"})
    return {"draft": strip_actions(draft, rejected), "approval": "rejected",
            "rejected_actions": rejected, "rewrite_requested": False, "events": events}


@traceable(name="finalize_node")
async def finalize_node(state: AgentState, deps: SupervisorDeps) -> dict:
    return {"final": state["draft"],
            "events": state["events"] + [{"type": "Finalize", "pipeline": "incident-v2"}]}


def _after_route(state: AgentState) -> str:
    return "chat_answer" if state["route"] == "chat" else "plan"


def _after_replan_check(state: AgentState) -> str:
    return "analysis" if state.get("research_sufficient") else "research"


def _after_validate(state: AgentState) -> str:
    return "approve" if state.get("validation_feedback") is None else "write"


def _after_approve(state: AgentState) -> str:
    return "write" if state.get("rewrite_requested") else "finalize"


def build_graph(deps: SupervisorDeps):
    g = StateGraph(AgentState)
    node = lambda fn: partial(fn, deps=deps)   # Wave 13 — 명시적 의존성 주입
    g.add_node("route", node(route_node))
    g.add_node("chat_answer", node(chat_answer_node))
    g.add_node("plan", node(plan_node))
    g.add_node("gate", node(gate_node))
    g.add_node("research", node(research_node))
    g.add_node("replan_check", node(replan_check_node))
    g.add_node("analysis", node(analysis_node))
    g.add_node("write", node(write_node))
    g.add_node("validate", node(validate_node))
    g.add_node("approve", node(approve_node))
    g.add_node("finalize", node(finalize_node))

    g.set_entry_point("route")
    g.add_conditional_edges("route", _after_route,
                            {"chat_answer": "chat_answer", "plan": "plan"})
    g.add_edge("chat_answer", END)
    g.add_edge("plan", "gate")
    g.add_edge("gate", "research")
    g.add_edge("research", "replan_check")
    g.add_conditional_edges("replan_check", _after_replan_check,
                            {"analysis": "analysis", "research": "research"})
    g.add_edge("analysis", "write")
    g.add_edge("write", "validate")
    g.add_conditional_edges("validate", _after_validate,
                            {"approve": "approve", "write": "write"})
    g.add_conditional_edges("approve", _after_approve,
                            {"finalize": "finalize", "write": "write"})
    g.add_edge("finalize", END)
    return g.compile(checkpointer=deps.checkpointer)
