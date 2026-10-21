import pytest

from agent.runbook_select import group_by_type, keyword_classify, select_runbooks
from tests.conftest import FakeIndex, monitoring

UNKNOWN = {"source": "monitoring", "data": {"system_name": "billing-service", "status": "unknown",
                                            "alerts": ["System 'billing-service' not found in monitoring"]}}


def types(picks):
    return {p["system"]: p["incident_type"] for p in picks}


def test_degraded_systems_are_judged_by_their_own_alerts():
    picks, misses = select_runbooks("gateway-service에서 payments-service로 가는 요청이 타임아웃",
                                    monitoring("gateway-service", "payments-service", "web-01"))
    assert types(picks) == {"gateway-service": "network-timeout", "payments-service": "cpu-spike"}
    assert misses == []


def test_healthy_system_not_in_question_is_skipped():
    picks, _ = select_runbooks("cache-service 메모리가 계속 올라가요", monitoring("cache-service", "web-01"))
    assert types(picks) == {"cache-service": "memory-leak"}


def test_healthy_system_in_question_is_judged_by_question():
    picks, misses = select_runbooks("web-01 디스크가 꽉 찼어요", monitoring("web-01"), FakeIndex().classify)
    assert types(picks) == {"web-01": "disk-full"}
    assert misses == []


def test_no_match_is_reported_instead_of_db_down_fallback():
    picks, misses = select_runbooks("web-01 디스크가 꽉 찼어요", monitoring("web-01"), keyword_classify)
    assert picks == []
    assert misses == [{"system": "web-01", "reason": "맞는 대응 매뉴얼이 없습니다"}]


def test_unknown_system_is_not_replaced_by_another_system():
    picks, misses = select_runbooks("billing-service 응답이 느려요", [UNKNOWN], FakeIndex().classify)
    assert picks == []
    assert misses[0]["system"] == "billing-service"


def test_same_system_twice_is_counted_once():
    picks, _ = select_runbooks("cache-service", monitoring("cache-service", "cache-service"))
    assert len(picks) == 1


@pytest.mark.parametrize("text, expected", [
    ("auth-service 알림: p95 latency 2.4s, connection pool 92% used", "high-latency"),
    ("log-service 알림: disk usage 95% on /var/log", "disk-full"),
    ("오늘 점심 뭐 먹지", None),
])
def test_keyword_classify(text, expected):
    hit = keyword_classify(text)
    assert (hit["incident_type"] if hit else None) == expected


def test_group_by_type_merges_systems_with_same_runbook():
    picks = [{"system": "a", "incident_type": "cpu-spike"}, {"system": "b", "incident_type": "cpu-spike"},
             {"system": "c", "incident_type": "disk-full"}]
    assert group_by_type(picks) == {"cpu-spike": ["a", "b"], "disk-full": ["c"]}
