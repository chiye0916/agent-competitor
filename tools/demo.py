"""Deterministic offline backends used to validate the complete agent loop."""

from __future__ import annotations

from typing import Any

from config.settings import Config
from tools.research import ResearchTools


class DemoResearchTools(ResearchTools):
    """Deterministic fixtures for a runnable demo; never presented as live research."""

    PAGES = {
        "https://example.test/alpha": {
            "title": "Alpha official product page",
            "content": "Alpha offers collaborative editing, a free tier, and a Pro plan at $20/month. It targets small product teams.",
        },
        "https://example.test/beta": {
            "title": "Beta official product page",
            "content": "Beta emphasizes automation, enterprise controls, and usage-based pricing. Public pricing is not listed.",
        },
    }

    def search_web(self, query: str, max_results: int = 5) -> dict[str, Any]:
        key = "beta" if "Beta" in query else "alpha"
        page = self.PAGES[f"https://example.test/{key}"]
        return {
            "query": query,
            "results": [
                {
                    "title": page["title"],
                    "url": f"https://example.test/{key}",
                    "snippet": page["content"],
                }
            ],
        }

    def read_web(self, url: str) -> dict[str, Any]:
        if url not in self.PAGES:
            raise RuntimeError(f"demo fixture 不存在: {url}")
        return {"url": url, **self.PAGES[url]}


class DemoLLMBackend:
    """A scripted model substitute used only to prove the harness end-to-end."""

    def create_plan(self, task: str) -> list[str]:
        return ["收集 Alpha 的定位与价格证据", "收集 Beta 的定位与价格证据", "交叉比较并形成结论"]

    def decide(self, context: dict[str, Any], tools: list[dict[str, Any]]) -> dict[str, Any]:
        round_no = context["progress"]["round"]
        actions = {
            1: {"name": "search_web", "arguments": {"query": "Alpha official pricing", "purpose": "查找 Alpha 一手资料", "target_step_id": "P1"}},
            2: {"name": "read_web", "arguments": {"url": "https://example.test/alpha", "purpose": "核验价格和定位", "target_step_id": "P1"}},
            3: {"name": "replan", "arguments": {"reason": "Alpha 资料显示协作能力是重要比较维度", "new_steps": ["收集 Beta 的定位、价格与协作/管控证据", "按价格、定位和协作/管控维度比较"]}},
            4: {"name": "search_web", "arguments": {"query": "Beta official pricing enterprise controls", "purpose": "查找 Beta 一手资料", "target_step_id": "R1-1"}},
            5: {"name": "read_web", "arguments": {"url": "https://example.test/beta", "purpose": "核验价格和企业能力", "target_step_id": "R1-1"}},
            6: {"name": "finish", "arguments": {"reason": "两个产品的一手定位信息已收集，且已明确公开价格信息的缺口", "completed_step_ids": ["R1-2"]}},
        }
        return actions.get(round_no, actions[6])

    def assess_progress(self, context: dict[str, Any], observation: dict[str, Any]) -> dict[str, Any]:
        completed = []
        if observation.get("tool") == "read_web":
            completed = [observation["arguments"]["target_step_id"]]
        return {"completed_step_ids": completed, "reason": "离线夹具按读取结果确认步骤"}

    def revise_plan(self, context: dict[str, Any], reason: str) -> list[str]:
        pending = [step["description"] for step in context["plan"] if step["status"] != "completed"]
        return pending or ["基于现有证据形成结论"]

    def write_report(self, context: dict[str, Any]) -> str:
        return """# Alpha 与 Beta 竞品分析（离线演示）

> 本报告使用固定测试夹具，只用于验证 Agent 执行链路，不代表真实市场信息。

## 执行摘要

Alpha 面向小型产品团队，强调协作编辑并提供免费层及每月 20 美元的 Pro 方案 [S1]。Beta 强调自动化、企业控制和按量计费，但测试资料未给出公开价格 [S2]。

## 对比

| 维度 | Alpha | Beta |
|---|---|---|
| 定位 | 小型产品团队 [S1] | 企业自动化与管控 [S2] |
| 价格 | 免费层；Pro $20/月 [S1] | 按量计费；公开价格缺失 [S2] |
| 差异点 | 协作编辑 [S1] | 企业控制 [S2] |

## 结论与局限

小团队可优先核验 Alpha，重视企业控制的组织可进一步评估 Beta。当前证据仅有两份演示页面，缺少真实用户评价、性能和完整条款，不能用于采购决策。
"""
