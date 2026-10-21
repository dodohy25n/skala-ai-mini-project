"""로컬 Ollama 실모델(qwen3:0.6b / qwen3:8b)로 두 질문만 끝까지 돌려 보는 확인 스크립트. 비용 없음.

  ollama serve  # 별도 터미널
  python eval/ollama_smoke.py > eval/results/ollama_smoke.txt
"""
import asyncio
import os
import sys
import time
from pathlib import Path

os.environ["LANGCHAIN_TRACING_V2"] = "false"
os.environ["LLM_BACKEND"] = "ollama"
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from langgraph.types import Command
from server import _build_llms, _build_deps
from agent.graph import build_graph
from agent.mcp_client import MCPClient
from agent.session_store import SqliteSessionStore
from agent.state import turn_input
from agent.systems import refresh_from_mcp

async def main():
    _, fast, quality = _build_llms()
    mcp = MCPClient(); await mcp.start(); await refresh_from_mcp(mcp)
    g = build_graph(_build_deps(fast_llm=fast, quality_llm=quality, mcp=mcp,
                                session_store=SqliteSessionStore(":memory:"), version="v2"))
    for i, (q, decision) in enumerate([("cache-service 메모리 사용률이 계속 올라가고 있어요, 원인 진단해줘", "reject"),
                                      ("web-01 배포 담당자가 누구야? 장애 진단해줘", "approve")]):
        cfg = {"configurable": {"thread_id": f"o{i}"}, "recursion_limit": 40}
        t0 = time.perf_counter()
        r = await g.ainvoke(turn_input(q, f"o{i}", []), cfg)
        asked = []
        while "__interrupt__" in r:
            asked.append([a["action"] for a in r["__interrupt__"][0].value["actions"]])
            r = await g.ainvoke(Command(resume=decision), cfg)
        picks = [e for e in r["events"] if e["type"] in ("RunbookSelected", "RunbookMiss")]
        print(f"=== {q} | route={r.get('route')} | {time.perf_counter()-t0:.1f}s | asked={asked} decision={decision}")
        print("events:", picks)
        print("final:", r.get("final"))
    await mcp.stop()
asyncio.run(main())
