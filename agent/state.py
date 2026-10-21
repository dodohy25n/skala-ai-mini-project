"""그래프 노드가 주고받는 State와 한 턴의 입력.

체크포인터는 같은 thread_id의 마지막 State 위에서 다음 실행을 시작합니다. 입력에 없는 키는
지난 턴 값이 그대로 남아, 같은 대화의 다음 질문이 이전 질문의 수집 결과와 승인 기록을
이어받습니다. turn_input()은 State의 모든 키를 빈 값으로 채운 입력을 만들어 이를 막습니다.
키를 손으로 나열하지 않고 AgentState 선언에서 읽으므로 필드를 추가해도 초기화가 빠지지 않습니다.
"""
from typing import Any, TypedDict, get_origin, get_type_hints


class AgentState(TypedDict, total=False):
    query: str
    thread_id: str
    history: list[dict]
    route: str
    needs_analysis: bool
    tasks: list[dict]
    gate_removed: int
    collected: list[dict]
    replan_rounds: int
    research_sufficient: bool
    runbook_picks: dict
    retry_count: int
    parallel_elapsed: float
    draft: str
    validation_attempts: int
    validation_feedback: str | None
    approval: str
    approved_actions: list[str]
    rejected_actions: list[str]
    approval_feedback: str | None
    approval_rewrites: int
    rewrite_requested: bool
    final: str
    last_prompt: str | None
    cost_usd: float
    events: list[dict]


_EMPTY: dict[Any, Any] = {list: list, dict: dict, int: int, float: float, bool: bool, str: str}


def turn_input(query: str, thread_id: str, history: list[dict]) -> dict:
    state = {}
    for key, hint in get_type_hints(AgentState).items():
        factory = _EMPTY.get(get_origin(hint) or hint)
        state[key] = factory() if factory else None
    state.update(query=query, thread_id=thread_id, history=history)
    return state
