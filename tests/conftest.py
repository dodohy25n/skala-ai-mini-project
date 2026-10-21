import json
import os
from pathlib import Path

os.environ["LANGCHAIN_TRACING_V2"] = "false"

import pytest

from agent.deps import SupervisorDeps
from agent.fake_llm import FastLLM, QualityLLM
from agent.runbook_select import keyword_classify

DATA = Path(__file__).resolve().parent.parent / "data"
MONITORING = json.loads((DATA / "monitoring.json").read_text())


class FakeMCP:
    """MCP 서버 프로세스 없이 같은 데이터 파일을 읽는 가짜 클라이언트. 호출 수를 센다."""

    def __init__(self, broken=()):
        self.broken = set(broken)
        self.calls = {"call_tool": 0, "read_resource": 0, "get_prompt": 0}

    async def call_tool(self, name, arguments, server="ops"):
        self.calls["call_tool"] += 1
        system = arguments["system_name"]
        if system in self.broken:
            raise ValueError("tool failed")
        if system not in MONITORING:
            return {"system_name": system, "status": "unknown", "cpu_percent": 0.0,
                    "memory_percent": 0.0, "alerts": [f"System '{system}' not found in monitoring"]}
        d = MONITORING[system]
        return {"system_name": system, "status": d["status"], "cpu_percent": d["cpu_base"],
                "memory_percent": d["memory_base"], "alerts": d.get("alerts", [])}

    async def find_resource_template(self, name):
        return "ops", "runbook://incident-response/{incident_type}"

    async def read_resource(self, uri, server):
        self.calls["read_resource"] += 1
        incident_type = uri.rsplit("/", 1)[-1]
        return (DATA / "runbooks" / f"{incident_type}.md").read_text()

    async def find_prompt(self, name):
        return "ops"

    async def get_prompt(self, name, arguments, server="ops"):
        self.calls["get_prompt"] += 1
        return f"시스템: {arguments['system_name']}\n증상: {arguments['symptom']}"


class FakeIndex:
    """임베딩 없이 키워드 규칙에 한국어 증상 몇 개를 더한 가짜 인덱스."""

    threshold = 0.56
    chunk_count = 0
    build_s = 0.0
    KOREAN = [("디스크", "disk-full"), ("느려", "high-latency"), ("db 연결", "db-down"),
              ("메모리", "memory-leak"), ("cpu", "cpu-spike")]

    def classify(self, text):
        low = text.lower()
        for keyword, incident_type in self.KOREAN:
            if keyword in low:
                return {"incident_type": incident_type, "score": 0.9}
        return keyword_classify(text)


def monitoring(*systems):
    return [{"source": "monitoring", "data": {"system_name": s, "cpu_percent": MONITORING[s]["cpu_base"],
                                              "memory_percent": MONITORING[s]["memory_base"],
                                              "status": MONITORING[s]["status"],
                                              "alerts": MONITORING[s].get("alerts", [])}}
            for s in systems]


def make_deps(mcp=None, index=None, **extra):
    return SupervisorDeps(
        fast_llm=FastLLM("fast-test", usd_per_1k_tokens=0.00015),
        quality_llm=QualityLLM("quality-test", usd_per_1k_tokens=0.005),
        mcp=mcp or FakeMCP(), session_store=None, version="v2",
        runbook_index=index, **extra)


@pytest.fixture
def fake_mcp():
    return FakeMCP()
