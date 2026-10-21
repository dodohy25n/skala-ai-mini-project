"""모니터링 대상 시스템 목록을 한 곳에서 관리합니다.

베이스는 같은 목록 로더가 graph.py, openai_llm.py, ollama_llm.py에 복사돼 있었습니다.
여기서는 서버가 기동할 때 MCP 서버의 list_systems Tool에 한 번 물어 목록을 채우고, MCP에
물을 수 없으면 data/monitoring.json을 직접 읽습니다. 집합을 제자리에서 갱신하므로 이 모듈을
import해 둔 어댑터도 같은 목록을 봅니다.
"""
import json
from pathlib import Path

MONITORING_FILE = Path(__file__).resolve().parent.parent / "data" / "monitoring.json"


def _from_file() -> set[str]:
    try:
        return set(json.loads(MONITORING_FILE.read_text()).keys())
    except (OSError, json.JSONDecodeError):
        return set()


KNOWN_SYSTEMS: set[str] = _from_file()


async def refresh_from_mcp(mcp) -> str:
    """MCP list_systems로 목록을 갱신하고, 목록을 어디서 얻었는지 돌려준다."""
    try:
        data = await mcp.call_tool("list_systems", {})
        systems = set(data["systems"])
    except Exception:
        return "file"
    if systems:
        KNOWN_SYSTEMS.clear()
        KNOWN_SYSTEMS.update(systems)
    return "mcp"


def mentioned_systems(query: str) -> list[str]:
    """질문에 나온 알려진 시스템을 등장 순서대로."""
    return [s for _, s in sorted((query.index(s), s) for s in KNOWN_SYSTEMS if s in query)]


def guess_target(query: str) -> str:
    """plan 정규화의 안전망 — 모델이 target을 안 내놓거나 오타를 내면 질문에서 시스템을 다시 찾는다.
    아무것도 없으면 auth-service로 둔다(로그인 같은 시스템명 없는 질문의 기본 조사 대상)."""
    found = mentioned_systems(query)
    return found[0] if found else "auth-service"


def guess_targets(query: str) -> list[str]:
    """research task가 하나도 없을 때 질문에 나온 시스템 전부를 복구한다."""
    return sorted(mentioned_systems(query)) or ["auth-service"]
