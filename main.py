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
from llm.backend import LLMBackend
from llm.openai_backend import OpenAIBackend
from storage.logger import RunLogger, compact, utc_now
from tools.base import BaseTool, ToolValidationError
from tools.demo import DemoLLMBackend, DemoResearchTools
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
    parser.add_argument("--output-dir", type=Path, help="本次产物目录")
    parser.add_argument("--max-rounds", type=int, help="覆盖最大轮数")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    # Keep Chinese logs readable when PowerShell's legacy code page differs.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    args = parse_args(argv)
    config = Config.from_env()
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
    tool_registry = build_default_registry(research_tools, state)
    agent = CompetitorAgent(llm, tool_registry, config, run_dir, state)
    try:
        report_path = agent.run()
    except Exception as exc:
        print(f"运行失败：{exc}", file=sys.stderr)
        return 1
    print(f"\n完成。报告：{report_path}\n状态：{agent.state_path}\n日志：{agent.logger.path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
