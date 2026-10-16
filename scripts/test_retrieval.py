"""Read-only CLI for manual inspection of the current Chroma Retriever."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from config.settings import Config
from embedding.openai_embedding import OpenAIEmbedding
from retrieval.chroma_store import ChromaStore
from retrieval.models import RetrievalMatch
from retrieval.retriever import Retriever


@dataclass(frozen=True)
class KnowledgeSnapshot:
    collection_count: int
    manifest_sha256: str | None


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="只读查看当前知识库的检索结果")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--query", help="执行一个 Query")
    group.add_argument("--queries-file", type=Path, help="执行 JSON 人工验证测试集")
    parser.add_argument("--case-id", action="append", help="仅运行指定测试 ID，可重复")
    parser.add_argument("--top-k", type=int, default=5, help="本次展示数量，默认 5")
    parser.add_argument("--delay-seconds", type=float, default=0.0, help="测试集 Query 间隔")
    parser.add_argument("--content-chars", type=int, default=500, help="每个 Chunk 展示字符数")
    parser.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    return parser.parse_args(argv)


def load_query_cases(path: Path) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise ValueError("RAG 测试集必须是 JSON 数组")
    cases: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for raw in payload:
        if not isinstance(raw, dict):
            raise ValueError("RAG 测试项必须是对象")
        case_id = str(raw.get("id") or "").strip()
        query = str(raw.get("query") or "").strip()
        if not case_id or case_id in seen_ids:
            raise ValueError("每个 RAG 测试项必须有唯一非空 id")
        if not query:
            raise ValueError(f"RAG 测试项 {case_id} 缺少 query")
        if not isinstance(raw.get("expected_source_urls", []), list):
            raise ValueError(f"RAG 测试项 {case_id} 的 expected_source_urls 必须是数组")
        seen_ids.add(case_id)
        cases.append(dict(raw))
    return cases


def build_read_only_retriever(config: Config) -> Retriever:
    db_path = Path(config.knowledge_db_path)
    if not db_path.exists():
        raise RuntimeError(f"知识库目录不存在: {db_path}")
    embedding = OpenAIEmbedding(config)
    store = ChromaStore(
        embedding_model=embedding,
        collection_name=config.knowledge_collection,
        persist_directory=db_path,
        create_if_missing=False,
    )
    return Retriever(
        store,
        relevance_threshold=config.rag_relevance_threshold,
    )


def knowledge_snapshot(retriever: Retriever) -> KnowledgeSnapshot:
    store = retriever.store
    digest = None
    if store.manifest_path is not None and store.manifest_path.exists():
        digest = hashlib.sha256(store.manifest_path.read_bytes()).hexdigest()
    return KnowledgeSnapshot(store.collection.count(), digest)


def match_to_record(
    match: RetrievalMatch,
    content_chars: int,
    *,
    accepted: bool,
) -> dict[str, Any]:
    metadata = match.chunk.metadata
    return {
        "rank": match.rank,
        "distance": match.distance,
        "threshold_decision": "accepted" if accepted else "rejected",
        "chunk_id": match.chunk.id,
        "document_id": match.chunk.document_id,
        "source_title": metadata.get("source_title")
        or metadata.get("document_title")
        or "",
        "source_url": metadata.get("source_url") or metadata.get("document_url") or "",
        "source_id": metadata.get("source_id"),
        "retrieved_at": metadata.get("retrieved_at"),
        "ingested_at": metadata.get("ingested_at"),
        "content": match.chunk.text[:content_chars],
    }


def execute_validation(
    retriever: Retriever,
    cases: list[dict[str, Any]],
    *,
    top_k: int,
    content_chars: int,
    delay_seconds: float = 0.0,
) -> list[dict[str, Any]]:
    if top_k <= 0:
        raise ValueError("top_k 必须大于 0")
    if content_chars <= 0:
        raise ValueError("content_chars 必须大于 0")
    if delay_seconds < 0:
        raise ValueError("delay_seconds 不能小于 0")
    records: list[dict[str, Any]] = []
    for index, case in enumerate(cases):
        if index and delay_seconds:
            time.sleep(delay_seconds)
        diagnostics = retriever.diagnose(str(case["query"]), top_k=top_k)
        accepted_keys = {
            (match.rank, match.chunk.id) for match in diagnostics.accepted_matches
        }
        records.append(
            {
                "id": case.get("id"),
                "category": case.get("category"),
                "query": case["query"],
                "expected_source_urls": list(case.get("expected_source_urls", [])),
                "expected_topics": list(case.get("expected_topics", [])),
                "expected_answer_status": case.get("expected_answer_status", "present"),
                "manual_evaluation": dict(case.get("manual_evaluation") or {}),
                "status": diagnostics.status,
                "reason": diagnostics.reason,
                "distance_metric": diagnostics.distance_metric,
                "relevance_threshold": diagnostics.threshold,
                "raw_candidate_count": len(diagnostics.raw_matches),
                "accepted_count": len(diagnostics.accepted_matches),
                "rejected_count": len(diagnostics.rejected_matches),
                "results": [
                    match_to_record(
                        match,
                        content_chars,
                        accepted=(match.rank, match.chunk.id) in accepted_keys,
                    )
                    for match in diagnostics.raw_matches
                ],
            }
        )
    return records


def format_validation(records: list[dict[str, Any]], distance_metric: str) -> str:
    sections: list[str] = []
    for record in records:
        header = f"[{record.get('id') or 'interactive'}] {record['query']}"
        lines = [header, f"类型: {record.get('category') or 'interactive'}"]
        expected = record.get("expected_source_urls") or []
        if expected:
            lines.append("期望来源（仅供人工判断）: " + ", ".join(expected))
        elif record.get("expected_answer_status") == "absent":
            lines.append("期望: 知识库无答案；检查是否仍返回貌似相关但不足以回答的 Chunk")
        threshold = record.get("relevance_threshold")
        rendered_threshold = "disabled" if threshold is None else f"{threshold:.6f}"
        lines.extend(
            [
                f"状态: {record.get('status')}",
                f"原因: {record.get('reason')}",
                f"Threshold: {rendered_threshold}",
                "Raw / Accepted / Rejected: "
                f"{record.get('raw_candidate_count', 0)} / "
                f"{record.get('accepted_count', 0)} / "
                f"{record.get('rejected_count', 0)}",
            ]
        )
        results = record["results"]
        if not results:
            lines.append("结果: 无")
        for result in results:
            distance = result["distance"]
            rendered_distance = "不可用" if distance is None else f"{distance:.6f}"
            lines.extend(
                [
                    "",
                    f"Rank {result['rank']}",
                    f"distance ({distance_metric}, 越小越近): {rendered_distance}",
                    f"threshold decision: {result['threshold_decision']}",
                    f"title: {result['source_title']}",
                    f"url: {result['source_url']}",
                    f"chunk_id: {result['chunk_id']}",
                    f"document_id: {result['document_id']}",
                    f"retrieved_at: {result['retrieved_at']}",
                    f"ingested_at: {result['ingested_at']}",
                    "content:",
                    result["content"],
                ]
            )
        sections.append("\n".join(lines))
    return "\n\n" + ("\n\n" + "=" * 80 + "\n\n").join(sections)


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    args = parse_args(argv)
    try:
        if args.queries_file:
            cases = load_query_cases(args.queries_file)
            if args.case_id:
                selected = set(args.case_id)
                cases = [case for case in cases if case["id"] in selected]
                missing = selected - {case["id"] for case in cases}
                if missing:
                    raise ValueError("未知测试 ID: " + ", ".join(sorted(missing)))
        else:
            query = args.query or input("Query: ").strip()
            if not query:
                raise ValueError("Query 不能为空")
            cases = [{"id": "interactive", "category": "interactive", "query": query}]

        config = Config.from_env()
        retriever = build_read_only_retriever(config)
        before = knowledge_snapshot(retriever)
        records = execute_validation(
            retriever,
            cases,
            top_k=args.top_k,
            content_chars=args.content_chars,
            delay_seconds=args.delay_seconds,
        )
        after = knowledge_snapshot(retriever)
        if before != after:
            raise RuntimeError("只读验证期间知识库逻辑状态发生变化")

        metric = retriever.store.distance_metric
        if args.json:
            print(
                json.dumps(
                    {
                        "configuration": {
                            "embedding_model": config.embedding_model,
                            "collection": config.knowledge_collection,
                            "top_k": args.top_k,
                            "distance_metric": metric,
                            "similarity_threshold": retriever.relevance_threshold,
                            "business_metadata_filter": None,
                            "active_version_filter": True,
                            "read_only_snapshot": asdict(after),
                        },
                        "queries": records,
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
        else:
            print(
                "当前验证配置："
                f"Embedding={config.embedding_model}, Collection={config.knowledge_collection}, "
                f"Top-K={args.top_k}, Distance={metric}, "
                f"Threshold={retriever.relevance_threshold}, "
                "Business Metadata Filter=None, Active Version Filter=True"
            )
            print(format_validation(records, metric))
        return 0
    except Exception as exc:
        print(f"检索验证失败：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
