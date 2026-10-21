"""개선 전후를 같은 질문 세트로 재는 스크립트.

서버를 띄우지 않고 그래프를 같은 프로세스에서 직접 실행합니다. 서버와 똑같이
`server._build_llms()`로 모델을 고르므로 LLM_BACKEND 환경 변수가 그대로 적용됩니다.

예:
  python eval/run_bench.py --label before --oracle-route --repeat 3
  python eval/run_bench.py --label after-hitl --oracle-route --repeat 3 --decision reject
  python eval/run_bench.py --label noinit --oracle-route --same-thread
"""
import argparse
import asyncio
import csv
import json
import os
import sys
import time
from pathlib import Path

os.environ.setdefault("LANGCHAIN_TRACING_V2", "false")
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agent.deps import SupervisorDeps          # noqa: E402
from agent.graph import build_graph            # noqa: E402
from agent.mcp_client import MCPClient         # noqa: E402
from agent.session_store import SqliteSessionStore  # noqa: E402
from eval.metrics import mean, median, risky_actions_in, runbooks_of, score_runbooks, unapproved_risky  # noqa: E402
from server import _build_llms                 # noqa: E402

try:
    from langgraph.types import Command
except ImportError:   # 구버전 langgraph
    Command = None


class OracleRouter:
    """라우터만 정답으로 고정한 fast_llm 래퍼.

    fake 라우터는 장애 질문 8개 중 1개만 incident로 보내 뒤 단계가 측정되지 않습니다.
    부품을 따로 재기 위해 route()만 정답을 돌려주고 나머지는 원래 모델에 넘깁니다.
    """

    def __init__(self, inner):
        self._inner = inner
        self.expected_route = "incident"
        self.name = f"oracle({inner.name})"

    def route(self, query):
        result = {"route": self.expected_route, "needs_analysis": False}
        return result, {"model": self.name, "tokens": 0, "usd": 0.0}

    def __getattr__(self, name):
        return getattr(self._inner, name)


def load_cases(path: Path, split: str | None):
    cases = [json.loads(l) for l in path.read_text().splitlines() if l.strip()]
    return [c for c in cases if split in (None, "all", c.get("split"))]


def build_input(query: str, thread_id: str, history: list) -> dict:
    try:
        from agent.state import turn_input
        return turn_input(query, thread_id, history)
    except ImportError:
        return {"query": query, "thread_id": thread_id, "history": history,
                "events": [], "cost_usd": 0.0}


async def run_case(graph, case, thread_id, decision):
    config = {"configurable": {"thread_id": thread_id}, "recursion_limit": 40}
    started = time.perf_counter()
    node_ms, last = {}, started
    final, approvals = {}, 0
    payload = build_input(case["query"], thread_id, [])
    while True:
        interrupted = False
        async for update in graph.astream(payload, config, stream_mode="updates"):
            now = time.perf_counter()
            if "__interrupt__" in update:
                interrupted = True
                approvals += 1
                last = now
                continue
            for node, delta in update.items():
                node_ms[node] = node_ms.get(node, 0) + (now - last) * 1000
                final.update(delta or {})
            last = now
        if not interrupted:
            break
        if Command is None:
            raise RuntimeError("interrupt가 났지만 Command를 불러올 수 없습니다")
        payload = Command(resume=decision)   # 즉시 재개하므로 사람 대기 시간은 지연에 들어가지 않습니다
    final["latency_ms"] = (time.perf_counter() - started) * 1000
    final["node_ms"] = node_ms
    final["approval_requests"] = approvals
    return final


async def main(args):
    boot = time.perf_counter()
    _, fast, quality = _build_llms()
    oracle = OracleRouter(fast) if args.oracle_route else None
    mcp = MCPClient()
    await mcp.start()
    deps_kwargs = dict(fast_llm=oracle or fast, quality_llm=quality, mcp=mcp,
                       session_store=SqliteSessionStore(":memory:"), version="v2")
    deps = _make_deps(deps_kwargs)
    graph = build_graph(deps)
    startup_s = round(time.perf_counter() - boot, 2)

    rows = []
    cases = load_cases(Path(args.cases), args.split)
    for rep in range(args.repeat):
        for case in cases:
            if oracle is not None:
                oracle.expected_route = case["expected_route"]
            thread_id = f"bench-{args.label}" if args.same_thread else f"bench-{case['id']}-{rep}"
            r = await run_case(graph, case, thread_id, args.decision)
            got = runbooks_of(r)
            scores = score_runbooks(case.get("expected_runbooks", {}), got)
            final_text = r.get("final", "") or ""
            risky = risky_actions_in(final_text)
            unapproved = unapproved_risky(final_text, r.get("approved_actions", []) or [])
            select_ms = sum(e.get("ms", 0) for e in r.get("events", [])
                            if e.get("type") == "RunbookSelected")
            rows.append({
                "label": args.label, "id": case["id"], "split": case.get("split", ""), "rep": rep,
                "route": r.get("route"), "expected_route": case["expected_route"],
                "route_ok": int(r.get("route") == case["expected_route"]),
                "runbooks": "|".join(sorted(got)),
                "expected_runbooks": "|".join(sorted(set(case.get("expected_runbooks", {}).values()))),
                **scores,
                "risky_total": len(risky), "risky_unapproved": len(unapproved),
                "approval_requests": r.get("approval_requests", 0),
                "latency_ms": round(r["latency_ms"], 1),
                "research_ms": round(r["node_ms"].get("research", 0), 1),
                "select_ms": round(select_ms, 1),
                "replan_rounds": r.get("replan_rounds", 0),
                "retry_count": r.get("retry_count", 0),
                "validation_attempts": r.get("validation_attempts", 0),
                "cost_usd": r.get("cost_usd", 0.0),
                "startup_s": startup_s,
            })
    await mcp.stop()

    out = Path(args.out or ROOT / "eval" / "results" / f"{args.label}.csv")
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    print_summary(rows, args.label, startup_s, out)


def _make_deps(kwargs):
    """deps 필드가 늘어나도(runbook_index, checkpointer) 같은 스크립트로 재도록 server의 조립 함수를 우선 씁니다."""
    try:
        from server import _build_deps
        return _build_deps(**kwargs)
    except ImportError:
        return SupervisorDeps(**kwargs)


def print_summary(rows, label, startup_s, out):
    inc = [r for r in rows if r["expected_route"] == "incident"]
    ran = [r for r in inc if r["route"] == "incident"]
    print(f"[{label}] rows={len(rows)} incident={len(inc)} startup={startup_s}s -> {out}")
    print(f"  route_acc        {mean([r['route_ok'] for r in rows])}")
    print(f"  runbook_exact    {mean([r['exact'] for r in inc])}")
    print(f"  runbook_recall   {mean([r['recall'] for r in inc])}")
    print(f"  reject_ok        {mean([r['reject_ok'] for r in inc])}")
    print(f"  risky_unapproved {sum(r['risky_unapproved'] for r in inc)} / {sum(r['risky_total'] for r in inc)}")
    print(f"  approvals        {sum(r['approval_requests'] for r in inc)}")
    print(f"  latency_p50      {median([r['latency_ms'] for r in ran])} ms")
    print(f"  select_p50       {median([r['select_ms'] for r in ran])} ms")
    print(f"  retry_total      {sum(r['retry_count'] for r in ran)}")
    print(f"  cost_mean        {mean([r['cost_usd'] for r in ran], 6)} usd")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--cases", default=str(ROOT / "eval" / "cases.jsonl"))
    p.add_argument("--split", default="all", help="dev | test | all")
    p.add_argument("--label", default="before")
    p.add_argument("--repeat", type=int, default=1)
    p.add_argument("--oracle-route", action="store_true", help="라우터만 정답으로 고정(부품 분리 측정)")
    p.add_argument("--decision", default="approve", help="승인 요청이 오면 자동으로 보낼 결정")
    p.add_argument("--same-thread", action="store_true", help="모든 질문을 한 대화(thread)로 보냄")
    p.add_argument("--out", default=None)
    asyncio.run(main(p.parse_args()))
