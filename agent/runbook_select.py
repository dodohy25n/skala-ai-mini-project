"""조사한 시스템마다 맞는 runbook 유형을 따로 고릅니다.

베이스는 모든 시스템의 알림을 한 문자열로 이어 붙여 유형 하나만 골랐습니다. 그래서
gateway 타임아웃 질문에 함께 조회한 payments의 CPU 알림이 섞여 cpu-spike가 나왔습니다.
여기서는 판단 근거를 시스템 단위로 나눕니다.

- 저하(degraded/down)된 시스템은 그 시스템의 알림만 봅니다.
- 정상이지만 질문에 이름이 나온 시스템은 질문 문장을 봅니다.
- 정상이고 질문에도 없는 시스템(비교용으로 함께 조회한 시스템)은 건너뜁니다.
- 의존 조사로 더한 시스템(agent/dependency.py)도 자기 모니터링 상태로 판단하고 근거를 dependency로 남깁니다.
  그 시스템에서 runbook을 고르면 증상을 말한 정상 시스템은 원인이 설명됐으므로 "없음"으로 남기지 않습니다.
- 모니터링 대상이 아닌 시스템과 아무 유형에도 맞지 않는 경우는 고르지 않고 "없음"으로 남깁니다.
"""
from typing import Callable

from .dependency import dependency_of

KEYWORD_RULES = [
    ("latency", "high-latency"),
    ("pool", "high-latency"),
    ("cpu", "cpu-spike"),
    ("memory", "memory-leak"),
    ("heap", "memory-leak"),
    ("timeout", "network-timeout"),
    ("network", "network-timeout"),
    ("disk", "disk-full"),
    ("down", "db-down"),
]

Classifier = Callable[[str], dict | None]


def keyword_classify(text: str) -> dict | None:
    low = text.lower()
    for keyword, incident_type in KEYWORD_RULES:
        if keyword in low:
            return {"incident_type": incident_type, "score": 1.0}
    return None


def select_runbooks(query: str, collected: list[dict],
                    classify: Classifier = keyword_classify) -> tuple[list[dict], list[dict]]:
    picks, misses, seen = [], [], set()
    origin_of = dependency_of(collected)
    for c in collected:
        if c.get("source") != "monitoring":
            continue
        data = c["data"]
        system = data.get("system_name", "")
        if system in seen:
            continue
        seen.add(system)
        status = data.get("status")
        if status == "unknown":
            misses.append({"system": system, "reason": "모니터링 대상이 아닙니다"})
            continue
        if status in ("degraded", "down"):
            text = f"{system} 알림: " + ", ".join(data.get("alerts", []))
            basis = "dependency" if system in origin_of else "alerts"
        elif system in query:
            text, basis = query, "query"
        else:
            continue
        hit = classify(text)
        if hit is None:
            misses.append({"system": system, "reason": "맞는 대응 매뉴얼이 없습니다"})
            continue
        picks.append({"system": system, "incident_type": hit["incident_type"],
                      "score": hit["score"], "basis": basis})
    explained = {origin_of[p["system"]] for p in picks if p["basis"] == "dependency"}
    return picks, [m for m in misses if m["system"] not in explained]


def group_by_type(picks: list[dict]) -> dict[str, list[str]]:
    by_type: dict[str, list[str]] = {}
    for p in picks:
        by_type.setdefault(p["incident_type"], []).append(p["system"])
    return by_type
