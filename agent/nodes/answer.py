"""답을 쓰고, 형식을 검사하고, 위험 조치는 운영자 승인을 받아 확정한다 (Wave 6, 8 + 승인 노드)."""
from langgraph.config import get_stream_writer
from langgraph.types import interrupt
from langsmith import traceable

from ..deps import SupervisorDeps
from ..state import AgentState
from ..safety import find_risky_actions, strip_actions
from .common import _cost


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
    MCP나 LLM 호출 없이 순수 함수만 둔다. 승인은 조치 "종류" 단위로 기억해 재작성본에 같은
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
