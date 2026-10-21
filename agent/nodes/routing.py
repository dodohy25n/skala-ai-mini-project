"""질문을 장애 경로와 대화 경로로 나누고, 대화 경로는 바로 답한다 (Wave 5)."""
from langgraph.config import get_stream_writer
from langsmith import traceable

from ..deps import SupervisorDeps
from ..state import AgentState
from .common import _cost


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
