"""답변 초안에서 서비스에 영향을 주는 조치를 찾고, 거절된 조치를 지웁니다.

LLM에게 다시 묻지 않고 규칙으로 판단합니다. runbook 6개에 나오는 위험 조치는 정해져 있고,
규칙은 같은 초안에 늘 같은 결과를 내므로 interrupt() 앞에 둘 수 있습니다(재개하면 노드가
처음부터 다시 실행되기 때문입니다).
"""
import re

RISKY_ACTIONS = {
    "프로세스 재시작": r"재시작",
    "배포 롤백": r"롤백",
    "DB failover 전환": r"failover",
    "인스턴스 트래픽 제외": r"로드밸런서에서 (?:일시 )?제외",
    "인스턴스 증설": r"한 대 더 띄워|오토스케일링",
    "파일 삭제": r"지울 수 있는|삭제",
    "볼륨 확장": r"볼륨을? 확장",
    "라우팅 우회": r"우회하는 라우팅|라우팅으로 (?:임시 )?전환",
    "타임아웃 임계값 상향": r"임계값을 (?:임시 )?상향",
}

_STEP = re.compile(r"(?=(?<![\d.])\d{1,2}\)\s)")
_NUMBER = re.compile(r"^(\d{1,2}\))\s*")


def split_steps(text: str) -> list[str]:
    return [c.strip() for c in _STEP.split(text or "") if c.strip()]


def _sentences(text: str) -> list[str]:
    return [s for s in re.split(r"(?<=[.!?])\s+", text) if s.strip()]


def find_risky_actions(draft: str) -> list[dict]:
    found = []
    for step in split_steps(draft):
        for action, pattern in RISKY_ACTIONS.items():
            if re.search(pattern, step):
                found.append({"action": action, "step": step})
    return found


def strip_actions(draft: str, rejected: list[str]) -> str:
    """거절된 조치가 든 문장만 지웁니다. 번호와 같은 번호 안의 다른 문장은 남깁니다."""
    patterns = [RISKY_ACTIONS[a] for a in rejected if a in RISKY_ACTIONS]
    if not patterns:
        return draft
    out, removed = [], 0
    for step in split_steps(draft):
        m = _NUMBER.match(step)
        number, body = (m.group(1), step[m.end():]) if m else ("", step)
        sentences = _sentences(body)
        kept = [s for s in sentences if not any(re.search(p, s) for p in patterns)]
        if len(kept) == len(sentences):
            out.append(step)
            continue
        removed += len(sentences) - len(kept)
        text = " ".join(kept) if kept else "(운영자가 보류한 조치는 답변에서 뺐습니다.)"
        out.append(f"{number} {text}".strip())
    if removed:
        out.append(f"※ 운영자가 보류한 조치 문장 {removed}개를 답변에서 뺐습니다.")
    return " ".join(out)
