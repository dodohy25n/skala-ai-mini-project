"""실제 bge-m3로 개발용 질문을 확인합니다. 모델 로드에 수 초가 걸려 slow로 분리했습니다.

  python -m pytest -m slow
"""
import pytest

pytestmark = pytest.mark.slow


@pytest.fixture(scope="module")
def index():
    pytest.importorskip("chromadb")
    from agent.runbook_index import RunbookIndex
    return RunbookIndex()


@pytest.mark.parametrize("text, expected", [
    ("gateway-service 알림: upstream timeout to payments-service (504), network packet loss 3%", "network-timeout"),
    ("cache-service 알림: memory usage climbing steadily over 6h, heap near limit", "memory-leak"),
    ("web-01 디스크가 꽉 찼다는 알림이 왔어요", "disk-full"),
    ("web-01 CPU 급증, auth-service 커넥션 풀도 같이 확인해줘", "cpu-spike"),
])
def test_dev_symptoms_pick_expected_runbook(index, text, expected):
    assert index.classify(text)["incident_type"] == expected


@pytest.mark.parametrize("text", ["web-01 오늘 점검 일정 있어?", "web-01 배포 담당자가 누구야?", "오늘 점심 뭐 먹지"])
def test_unrelated_questions_fall_below_threshold(index, text):
    assert index.classify(text) is None


def test_scores_are_aggregated_by_max_per_type(index):
    ranked = index.search("payments-service 알림: CPU 91% (threshold 80%)")
    assert len({r["incident_type"] for r in ranked}) == len(ranked)
    assert ranked == sorted(ranked, key=lambda r: -r["score"])
