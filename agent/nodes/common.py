"""노드들이 함께 쓰는 작은 도우미."""
from ..state import AgentState


def _cost(state: AgentState, usage: dict) -> float:
    return round(state.get("cost_usd", 0.0) + usage["usd"], 6)
