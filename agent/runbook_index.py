"""runbook을 줄 단위로 임베딩해 두고, 증상 문장과 의미가 가장 가까운 장애 유형을 찾습니다.

- 청크: runbook이 3~5줄로 짧아 크기 기반 분할은 의미가 없습니다. 한 줄을 청크 하나로 두고
  어느 장애의 절차인지 흐려지지 않게 제목을 앞에 붙입니다.
- 메타데이터: 파일 이름(cpu-spike 등)을 incident_type으로 둡니다. 이 값이 MCP Resource URI
  runbook://incident-response/{incident_type}에 그대로 들어가므로 research 노드는 바뀌지 않습니다.
- 집계: 청크 점수를 유형별 최댓값으로 모읍니다. 합으로 모으면 비슷한 줄이 많은 문서가 이깁니다.
- 임계값: 벡터 검색은 무엇을 물어도 가장 가까운 문서를 돌려주므로, 기준 아래면 "맞는 매뉴얼 없음"입니다.
"""
import re
import time
from pathlib import Path

RUNBOOK_DIR = Path(__file__).resolve().parent.parent / "data" / "runbooks"
DEFAULT_MODEL = "BAAI/bge-m3"
DEFAULT_THRESHOLD = 0.56


def runbook_chunks(runbook_dir: Path = RUNBOOK_DIR) -> tuple[list[str], list[str], list[dict]]:
    ids, docs, metas = [], [], []
    for path in sorted(runbook_dir.glob("*.md")):
        lines = [l.strip() for l in path.read_text().splitlines() if l.strip()]
        title = lines[0].lstrip("# ").removeprefix("Runbook: ")
        for i, line in enumerate(lines[1:], start=1):
            body = re.sub(r"^\d+\.\s+", "", line)
            ids.append(f"{path.stem}-{i}")
            docs.append(f"{title} - {body}")
            metas.append({"incident_type": path.stem, "line": i})
    return ids, docs, metas


class RunbookIndex:
    def __init__(self, runbook_dir: Path = RUNBOOK_DIR, model_name: str = DEFAULT_MODEL,
                 threshold: float = DEFAULT_THRESHOLD, top_k: int = 8):
        import chromadb
        from chromadb.config import Settings
        from chromadb.utils import embedding_functions

        started = time.perf_counter()
        embed = embedding_functions.SentenceTransformerEmbeddingFunction(
            model_name=model_name, normalize_embeddings=True)
        client = chromadb.EphemeralClient(settings=Settings(anonymized_telemetry=False))
        self.col = client.get_or_create_collection(
            "runbooks", embedding_function=embed, metadata={"hnsw:space": "cosine"})
        ids, docs, metas = runbook_chunks(runbook_dir)
        self.col.upsert(ids=ids, documents=docs, metadatas=metas)
        self.threshold = threshold
        self.top_k = min(top_k, len(ids))
        self.chunk_count = len(ids)
        self.build_s = round(time.perf_counter() - started, 2)

    def search(self, text: str) -> list[dict]:
        hits = self.col.query(query_texts=[text], n_results=self.top_k)
        best: dict[str, float] = {}
        for meta, dist in zip(hits["metadatas"][0], hits["distances"][0]):
            best.setdefault(meta["incident_type"], round(1 - dist, 3))
        return [{"incident_type": t, "score": s}
                for t, s in sorted(best.items(), key=lambda x: -x[1])]

    def classify(self, text: str) -> dict | None:
        ranked = self.search(text)
        if not ranked or ranked[0]["score"] < self.threshold:
            return None
        return ranked[0]
