"""eval/results/*.csv를 읽어 라벨별 비교표(markdown)를 출력합니다.

  python eval/report.py                 # 전체 라벨
  python eval/report.py before after-rag
"""
import csv
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from eval.metrics import mean, median  # noqa: E402

RESULTS = Path(__file__).resolve().parent / "results"


def load(label):
    with (RESULTS / f"{label}.csv").open() as f:
        rows = list(csv.DictReader(f))
    for r in rows:
        for k, v in r.items():
            if k in ("label", "id", "split", "route", "expected_route", "runbooks", "expected_runbooks"):
                continue
            r[k] = float(v) if v not in ("", None) else ""
    return rows


def summarize(rows):
    inc = [r for r in rows if r["expected_route"] == "incident"]
    ran = [r for r in inc if r["route"] == "incident"]
    return {
        "runbook 정확 일치": mean([r["exact"] for r in inc]),
        "runbook 포함률": mean([r["recall"] for r in inc]),
        "매뉴얼 없음 거절": mean([r["reject_ok"] for r in inc]),
        "승인 없이 나간 위험 조치": f"{int(sum(r['risky_unapproved'] for r in inc))}/{int(sum(r['risky_total'] for r in inc))}",
        "승인 요청 수": int(sum(r["approval_requests"] for r in inc)),
        "latency p50 (ms)": median([r["latency_ms"] for r in ran]),
        "runbook 선택 p50 (ms)": median([r["select_ms"] for r in ran]),
        "retry 합계": int(sum(r["retry_count"] for r in ran)),
        "cost 평균 (usd)": mean([r["cost_usd"] for r in ran], 6),
        "기동 (s)": rows[0]["startup_s"] if rows else "",
        "행 수": len(rows),
    }


def main(labels):
    if not labels:
        labels = sorted(p.stem for p in RESULTS.glob("*.csv"))
    table = {l: summarize(load(l)) for l in labels}
    keys = list(next(iter(table.values())).keys())
    print("| 지표 | " + " | ".join(labels) + " |")
    print("| --- | " + " | ".join("---" for _ in labels) + " |")
    for k in keys:
        print(f"| {k} | " + " | ".join(str(table[l][k]) for l in labels) + " |")


if __name__ == "__main__":
    main(sys.argv[1:])
