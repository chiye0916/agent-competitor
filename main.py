"""CLI entry point for the framework-free competitor research agent."""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

# These imports keep the original ``from main import ...`` interface compatible
# while all implementation now lives in focused modules.
from agent.context import ContextBuilder, ReportContextBuilder
from agent.core import CompetitorAgent
from agent.state import AgentState, PlanStep, Source
from config.settings import Config
from embedding.openai_embedding import OpenAIEmbedding
from knowledge.evaluator import KnowledgeEvaluator
from knowledge.ingestion import KnowledgeIngestionPipeline
from knowledge.policy import KnowledgePolicy
from knowledge.service import KnowledgeIngestionService
from knowledge.splitter import TextSplitter
from llm.backend import LLMBackend, LLMResponseError
from llm.openai_backend import OpenAIBackend
from retrieval.chroma_store import ChromaStore
from retrieval.retriever import Retriever
from storage.logger import RunLogger, compact, utc_now
from tools.base import BaseTool, ToolValidationError
from tools.demo import DemoLLMBackend, DemoResearchTools
from tools.knowledge import KnowledgeRetrievalTool
from tools.registry import ToolRegistry, build_default_registry
from tools.research import (
    FinishTool,
    ReadWebTool,
    ReplanTool,
    ResearchTools,
    SearchWebTool,
    VisibleTextParser,
)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="从零实现的单 Agent 竞品分析系统")
    parser.add_argument("--task", help="竞品分析目标")
    parser.add_argument("--demo", action="store_true", help="使用固定夹具离线运行完整链路")
    parser.add_argument("--resume", type=Path, help="从已有 state.json 恢复")
    parser.add_argument(
        "--ingest-existing",
        type=Path,
        metavar="STATE_JSON",
        help="从已完成运行的 state.json 单独重试知识入库",
    )
    parser.add_argument("--output-dir", type=Path, help="本次产物目录")
    parser.add_argument("--max-rounds", type=int, help="覆盖最大轮数")
    return parser.parse_args(argv)


def build_knowledge_services(
    config: Config,
) -> tuple[Retriever, KnowledgeIngestionService]:
    embedding_model = OpenAIEmbedding(config)
    knowledge_store = ChromaStore(
        embedding_model=embedding_model,
        collection_name=config.knowledge_collection,
        persist_directory=config.knowledge_db_path,
    )
    retriever = Retriever(knowledge_store)
    pipeline = KnowledgeIngestionPipeline(
        evaluator=KnowledgeEvaluator(
            KnowledgePolicy(min_content_chars=config.knowledge_min_content_chars)
        ),
        splitter=TextSplitter(
            chunk_size=config.knowledge_chunk_size,
            chunk_overlap=config.knowledge_chunk_overlap,
        ),
        store=knowledge_store,
    )
    return retriever, KnowledgeIngestionService(pipeline)


def ingest_existing(args: argparse.Namespace, config: Config) -> int:
    if args.demo or args.resume or args.task:
        print(
            "错误：--ingest-existing 不能与 --demo、--resume 或 --task 同时使用",
            file=sys.stderr,
        )
        return 2
    state = AgentState.load(args.ingest_existing)
    if state.status != "completed" or not state.report_path:
        print("错误：只能为已成功生成报告的运行重试知识入库", file=sys.stderr)
        return 2
    if not Path(state.report_path).exists():
        print(f"错误：报告文件不存在：{state.report_path}", file=sys.stderr)
        return 2
    run_dir = args.output_dir or args.ingest_existing.parent
    _, ingestion_service = build_knowledge_services(config)
    logger = RunLogger(run_dir / "run.jsonl", append=True)
    status_path = run_dir / "knowledge_ingestion.json"
    summary = ingestion_service.run(state.sources, status_path, logger)
    print(f"知识入库状态：{summary['status']}\n记录：{status_path}")
    return 0 if summary["status"] == "completed" else 1


def main(argv: list[str] | None = None) -> int:
    # Keep Chinese logs readable when PowerShell's legacy code page differs.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    args = parse_args(argv)
    config = Config.from_env()
    if args.ingest_existing:
        return ingest_existing(args, config)
    if args.max_rounds:
        config.max_rounds = args.max_rounds
    if args.resume:
        state = AgentState.load(args.resume)
        run_dir = args.output_dir or args.resume.parent
    else:
        task = args.task or ("比较 Alpha 与 Beta 的定位、定价和适用场景" if args.demo else None)
        if not task:
            print("错误：真实运行必须传入 --task", file=sys.stderr)
            return 2
        state = AgentState(task=task)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        run_dir = args.output_dir or Path("artifacts") / ("demo" if args.demo else stamp)
    llm: LLMBackend = DemoLLMBackend() if args.demo else OpenAIBackend(config)
    research_tools = DemoResearchTools(config) if args.demo else ResearchTools(config)
    retriever = None
    ingestion_service = None
    if not args.demo:
        retriever, ingestion_service = build_knowledge_services(config)
    tool_registry = build_default_registry(research_tools, state, retriever)
    agent = CompetitorAgent(llm, tool_registry, config, run_dir, state)
    try:
        report_path = agent.run()
    except Exception as exc:
        print(f"运行失败：{exc}", file=sys.stderr)
        return 1
    ingestion_summary = None
    if ingestion_service is not None and config.knowledge_auto_ingest:
        ingestion_summary = ingestion_service.run(
            state.sources,
            run_dir / "knowledge_ingestion.json",
            agent.logger,
        )
    print(f"\n完成。报告：{report_path}\n状态：{agent.state_path}\n日志：{agent.logger.path}")
    if ingestion_summary is not None:
        print(
            "知识入库："
            f"{ingestion_summary['status']} "
            f"(new={ingestion_summary.get('new', 0)}, "
            f"updated={ingestion_summary.get('updated', 0)}, "
            f"unchanged={ingestion_summary.get('unchanged', 0)}, "
            f"failed={ingestion_summary.get('failed', 0)})"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
