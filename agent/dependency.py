"""정상인 시스템에 증상이 있다는 질문에서, 그 시스템이 의존하는 시스템을 함께 조사할지 정합니다.

web-01처럼 지표가 정상인 시스템의 증상은 그 시스템 자체의 runbook과 맞지 않는 경우가 있습니다.
원인이 web-01이 호출하는 auth-service 쪽에 있을 수 있기 때문입니다. 반대로 정상 시스템을 물을 때마다
의존 대상을 더하면 "web-01 디스크가 꽉 찼어요"처럼 web-01 자체의 문제에도 다른 시스템의 runbook이 섞입니다.
그래서 판단은 LLM이 아니라 코드가 내리고, 아래 세 조건이 모두 맞을 때만 의존 대상을 더합니다.

1. 질문에 이름이 나온 시스템의 모니터링 상태가 healthy입니다. 저하된 시스템은 자기 알림으로 runbook을 고릅니다.
2. 질문에 증상 표현(SYMPTOM)이 있습니다. "정상인지 확인해줘", "담당자가 누구야?" 같은 질문은 제외됩니다.
3. 질문 문장으로 그 시스템의 runbook을 고르지 못했습니다. 고를 수 있으면 그 시스템 자체의 문제로 봅니다.
"""
import re
from typing import Callable

from .systems import mentioned_systems

SYMPTOM = re.compile(r"에러|오류|실패|안\s?[되돼된]|끊[기겨긴]|먹통|느려|지연|타임아웃")

ABNORMAL = ("degraded", "down")


def has_symptom(query: str) -> bool:
    return bool(SYMPTOM.search(query))


def expansion_origins(query: str, collected: list[dict],
                      classify: Callable[[str], dict | None]) -> list[str]:
    """의존 대상을 더 조사할 정상 시스템을 질문에 나온 순서대로 돌려준다."""
    if not has_symptom(query):
        return []
    healthy = {c["data"].get("system_name") for c in collected
               if c.get("source") == "monitoring" and c["data"].get("status") == "healthy"}
    named = [s for s in mentioned_systems(query) if s in healthy]
    if not named or classify(query) is not None:
        return []
    return named


def dependency_of(collected: list[dict]) -> dict[str, str]:
    """의존 조사로 더한 시스템마다 그 조사를 부른 정상 시스템을 돌려준다."""
    return {dep: c["data"]["system"] for c in collected if c.get("source") == "dependency"
            for dep in c["data"]["depends_on"]}


def dependency_reason(entry: dict, collected: list[dict]) -> str:
    """의존 대상의 모니터링 결과로 답변에 쓸 이유 문장을 만든다."""
    origin, deps = entry["data"]["system"], entry["data"]["depends_on"]
    status = {c["data"].get("system_name"): c["data"].get("status")
              for c in collected if c.get("source") == "monitoring"}
    abnormal = [d for d in deps if status.get(d) in ABNORMAL]
    healthy = [d for d in deps if status.get(d) == "healthy"]
    if abnormal:
        names = ", ".join(abnormal)
        return (f"{origin} 지표는 정상이지만 의존 대상인 {names} 지표가 저하 상태라, "
                f"증상의 원인을 {names} 쪽으로 보고 함께 조사했습니다.")
    if len(healthy) == len(deps):
        return (f"{origin} 지표는 정상이고 의존 대상인 {', '.join(healthy)} 지표도 정상이라 "
                f"원인을 좁히지 못했습니다.")
    return f"{origin} 지표는 정상이고 의존 대상인 {', '.join(deps)} 상태는 확인하지 못했습니다."
