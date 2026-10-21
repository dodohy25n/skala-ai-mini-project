import pytest

from agent.safety import find_risky_actions, split_steps, strip_actions

DRAFT = ("'q' 진단 결과: 1) 모니터링: cache-service 상태=degraded, CPU 44% (알림 2건) "
         "2) cache-service: 따라 OOM 직전이면 해당 프로세스를 즉시 재시작해 중단을 막는다. "
         "힙 덤프로 쌓이는 객체를 확인한다. 3) 원인이 된 배포가 있으면 롤백한다. "
         "4) 정상화 확인 후 문서화하세요.")


@pytest.mark.parametrize("sentence, action", [
    ("1) 해당 프로세스를 즉시 재시작한다.", "프로세스 재시작"),
    ("1) 직전 배포를 롤백한다.", "배포 롤백"),
    ("1) 필요 시 failover 노드로 전환한다.", "DB failover 전환"),
    ("1) 해당 인스턴스를 로드밸런서에서 일시 제외한다.", "인스턴스 트래픽 제외"),
    ("1) 오토스케일링 그룹에서 한 대 더 띄워 부하를 분산한다.", "인스턴스 증설"),
    ("1) 안전하게 지울 수 있는 항목부터 정리한다.", "파일 삭제"),
    ("1) 볼륨을 확장하거나 스토리지를 연결한다.", "볼륨 확장"),
    ("1) 해당 구간을 우회하는 라우팅으로 임시 전환한다.", "라우팅 우회"),
    ("1) 임계값을 임시 상향해 2차 피해를 줄인다.", "타임아웃 임계값 상향"),
])
def test_each_risky_action_is_detected(sentence, action):
    assert [f["action"] for f in find_risky_actions(sentence)] == [action]


def test_safe_draft_has_no_risky_action():
    assert find_risky_actions("1) 모니터링 수치를 확인한다. 2) 정상화 후 문서화한다.") == []


def test_numbers_inside_alerts_do_not_split_steps():
    assert len(split_steps("1) upstream timeout (504) 확인 2) 정리")) == 2


def test_strip_removes_only_rejected_sentences_and_keeps_numbering():
    out = strip_actions(DRAFT, ["프로세스 재시작"])
    assert "재시작" not in out
    assert "힙 덤프" in out
    assert "3) 원인이 된 배포가 있으면 롤백한다." in out
    assert [f["action"] for f in find_risky_actions(out)] == ["배포 롤백"]


def test_strip_note_does_not_trigger_detection_again():
    out = strip_actions(DRAFT, ["프로세스 재시작", "배포 롤백"])
    assert find_risky_actions(out) == []
    assert "※" in out
