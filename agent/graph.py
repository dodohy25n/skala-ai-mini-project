"""그래프 배선 — 노드를 잇고 분기를 정합니다. 노드 구현은 agent/nodes/에 있습니다.

route → (chat_answer | plan → gate → research → dependency → research ⇄ replan_check
         → analysis → write ⇄ validate → approve ⇄ write → finalize)

dependency는 첫 조사 직후 한 번만 지나고, 더한 의존 대상이 없으면 바로 replan_check로 갑니다.

모든 노드는 SupervisorDeps를 functools.partial로 주입받습니다(Wave 13).
분기 함수는 진행 로그가 아니라 State 필드만 읽습니다.
"""
from functools import partial

from langgraph.graph import END, StateGraph

from .deps import SupervisorDeps
from .nodes.answer import analysis_node, approve_node, finalize_node, validate_node, write_node
from .nodes.dependency import dependency_node
from .nodes.planning import gate_node, plan_node
from .nodes.research import replan_check_node, research_node
from .nodes.routing import chat_answer_node, route_node
from .state import AgentState


def _after_route(state: AgentState) -> str:
    return "chat_answer" if state["route"] == "chat" else "plan"


def _after_research(state: AgentState) -> str:
    return "replan_check" if state.get("dependency_checked") else "dependency"


def _after_dependency(state: AgentState) -> str:
    pending = any(t.get("dependency_of") and t["status"] == "pending" for t in state["tasks"])
    return "research" if pending else "replan_check"


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
    g.add_node("dependency", node(dependency_node))
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
    g.add_conditional_edges("research", _after_research,
                            {"dependency": "dependency", "replan_check": "replan_check"})
    g.add_conditional_edges("dependency", _after_dependency,
                            {"research": "research", "replan_check": "replan_check"})
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
