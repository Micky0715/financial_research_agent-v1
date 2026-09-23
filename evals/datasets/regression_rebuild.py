"""Rebuild the bad-case regression set into executable tasks (Phase 3).

The problem this replaces: `build_datasets.build_regression_tasks()` used each
bad case's **title** as the research query, producing "tasks" like

    query = "ResearchAgent 串行搜索导致耗时过长"
    query = "python str.replace 打补丁静默失败"

Those are defect descriptions, not research requests. Running them measured
nothing about whether the defect had returned: the pipeline dutifully searched
the web for the phrase "str.replace 打补丁静默失败", found nothing, and the run
was scored as a refusal. 31 of 41 regression rows were of this shape.

What this module does instead: for every bad case, state explicitly whether it
can be reproduced as an eval task, and if so, author a **real** request plus
the fault injection that recreates the original condition.

The honest part is the classification. Nine of the 31 bad cases are not agent
behaviour at all - they are dev-tooling and performance issues (a CLI flag that
triggered an eval run, a `str.replace` patch that silently no-op'd, a slow
first-time sector-map build). Forcing those into research tasks would
manufacture coverage. They stay recorded, with their reason, and are excluded
from the executable set.

    VERIFICATION CLASSES
    deterministic        reproducible offline with fault injection, rule-checkable
    needs_human_review   the expectation requires a person to set
    needs_network        only reproducible against the live provider
    not_agent_behaviour  real defect, but in tooling/perf - not an agent regression

Source traceability is preserved either way: every entry carries
`source_reference = "Bad Case N"` and `eval/bad_cases.md` is never edited.

    python -m evals.datasets.regression_rebuild --write
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")

from evals.datasets.build_datasets import parse_bad_cases  # noqa: E402
from evals.datasets.schema import (  # noqa: E402
    Category,
    EvalDataset,
    EvalTask,
    ExpectedOutcome,
    GoldStatus,
    RequiredFact,
    Split,
)

OUT_PATH = Path(__file__).resolve().parent / "regression_rebuilt.json"
MANIFEST_PATH = Path(__file__).resolve().parent / "regression_manifest.json"

DETERMINISTIC = "deterministic"
NEEDS_HUMAN = "needs_human_review"
NEEDS_NETWORK = "needs_network"
NOT_AGENT = "not_agent_behaviour"

SEARCH, READ, PDF, AKSHARE, EXPORT = (
    "web_search", "read_webpage", "read_pdf", "fetch_financial_snapshot", "export_report_file")


#: One entry per bad case. `verification` decides whether a task is emitted.
#: For deterministic entries, every field is authored by hand - the subject is
#: a real A-share company or a real macro/industry topic, and `fixture_scenario`
#: recreates the original fault.
BAD_CASE_MAP: dict[int, dict[str, Any]] = {
    1: {"verification": NOT_AGENT,
        "reason": "串行搜索导致耗时过长属于性能问题。它的修复（并发+早停）没有可被"
                  "确定性 grader 判定的行为差异；时延在离线 fixture 下由 stub 决定，"
                  "强行做成任务只会测出 fixture 的速度。"},
    2: {"verification": DETERMINISTIC,
        "subject": "贵州茅台", "report_type": "company_research",
        "query": "贵州茅台投资价值分析",
        "fixture_scenario": "default",
        "expected_outcome": ExpectedOutcome.REPORT_GENERATED,
        "required_tools": [SEARCH, READ], "degraded_ok": False,
        "max_expected_tool_calls": 20,
        "note": "Planner 曾输出 9-11 个任务含重复 research/browse 阶段，导致同一阶段"
                "被执行多次。回归断言：计划归一化后仍能完整走到 report 阶段，"
                "且工具调用数不因阶段重复而翻倍。"},
    3: {"verification": DETERMINISTIC,
        "subject": "贵州茅台", "report_type": "company_research",
        "query": "贵州茅台财务与估值分析",
        "fixture_scenario": "alias_only_sources",
        "expected_outcome": ExpectedOutcome.REPORT_WITH_CAVEATS,
        "required_tools": [SEARCH, READ], "degraded_ok": True,
        "required_facts": [{"key": "revenue_2024", "any_of": ["1200.5亿元", "1,200.5亿元"],
                            "description": "来源仅以证券代码指代主体时，营收仍应被抽取"}],
        "note": "QualityScorer 曾用完整 topic 字符串做相关性匹配，只写 '600519' 的页面"
                "被判为零相关而丢弃。回归断言：来源仅用代码/简称指代主体时，"
                "仍能选出可用来源并产出报告。"},
    4: {"verification": DETERMINISTIC,
        "subject": "宁德时代", "report_type": "company_research",
        "query": "宁德时代投资分析",
        "fixture_scenario": "all_403",
        "expected_outcome": ExpectedOutcome.INSUFFICIENT_EVIDENCE,
        "required_tools": [SEARCH], "degraded_ok": False,
        "max_expected_tool_calls": 30,
        "note": "BrowserAgent 曾对同一批必然失败的 URL 反复重试。回归断言：确定性失败"
                "不应耗尽重试预算，且必须如实报资料不足而不是硬出报告。"},
    5: {"verification": DETERMINISTIC,
        "subject": "隆基绿能", "report_type": "company_research",
        "query": "隆基绿能投资价值分析",
        "fixture_scenario": "single_usable_source",
        "expected_outcome": ExpectedOutcome.REPORT_WITH_CAVEATS,
        "required_tools": [SEARCH, READ], "degraded_ok": True,
        "forbidden_claims": ["保证收益", "目标价"],
        "note": "source_count 极低时 Evaluation 分数仍然虚高。回归断言：只有 1 个可用"
                "来源时报告必须带局限说明，且不得出现确定性投资结论。"},
    6: {"verification": NOT_AGENT,
        "reason": "eval_summary 统计历史旧 run 导致指标污染，属于评测脚本缺陷。"
                  "已由新评测体系结构性消除（每次运行独立 JSONL + 显式 results 路径），"
                  "没有对应的 agent 行为可测。"},
    7: {"verification": NOT_AGENT,
        "reason": "`--help` 误触发实际评测是 CLI 参数解析缺陷，属于开发工具问题。"},
    8: {"verification": DETERMINISTIC,
        "subject": "比亚迪", "report_type": "company_research",
        "query": "比亚迪投资价值分析",
        "fixture_scenario": "empty_pages",
        "expected_outcome": ExpectedOutcome.INSUFFICIENT_EVIDENCE,
        "required_tools": [SEARCH], "degraded_ok": False,
        "note": "抓取返回 success=True 但正文为空，属于软失败。回归断言：空正文不得被"
                "当作可用来源，必须如实判定资料不足。"},
    9: {"verification": DETERMINISTIC,
        "subject": "光伏行业", "report_type": "industry_research",
        "query": "光伏行业投资风险分析",
        "fixture_scenario": "default",
        "expected_outcome": ExpectedOutcome.REPORT_WITH_CAVEATS,
        "required_tools": [SEARCH, READ], "forbidden_tools": [AKSHARE],
        "degraded_ok": True, "max_expected_tool_calls": 20,
        "note": "行业研报曾复用公司财务指标，导致 financial_depth_score 不公平。"
                "回归断言：行业主题没有证券代码，不应调用公司财务快照工具。"},
    10: {"verification": NOT_AGENT,
         "reason": "热缓存/冷缓存评测混淆属于评测方法学问题。已被结构性处理："
                   "离线评测强制关闭搜索缓存以保证可复现。相关的可观测性盲区"
                   "（热缓存下检索绕过执行器）已单独记录在 final_verification_audit.md。"},
    11: {"verification": NEEDS_NETWORK,
         "reason": "MCP stdio session 首次调用即 Connection closed，只能在真实 MCP "
                   "子进程下复现；fixture 直接替换 handler，不经过 stdio 传输层。"},
    12: {"verification": NEEDS_NETWORK,
         "reason": "embedding 模型下载失败需要真实网络与 HuggingFace/镜像可达性。"},
    13: {"verification": DETERMINISTIC,
         "subject": "贵州茅台", "report_type": "company_research",
         "query": "贵州茅台2025年年度跟踪报告",
         "fixture_scenario": "single_domain_flood",
         "expected_outcome": ExpectedOutcome.INSUFFICIENT_EVIDENCE,
         "required_tools": [SEARCH], "degraded_ok": False,
         "max_expected_tool_calls": 30,
         "note": "语义排序把同一站点的多篇内容批量推进抓取窗口，恰逢该站批量 403，"
                 "整轮抓取被单站反爬拖垮。回归断言：单域名全量失败时不得无限重试，"
                 "且必须如实报资料不足。"},
    14: {"verification": DETERMINISTIC,
         "subject": "示例公司", "report_type": "company_research",
         "query": "示例公司财务与估值分析",
         "fixture_scenario": "decimal_heavy_numbers",
         "expected_outcome": ExpectedOutcome.REPORT_WITH_CAVEATS,
         "required_tools": [SEARCH, READ], "degraded_ok": True,
         "note": "通用句子切分器在英文句点处断句，带小数的金额（1,200.5）全部抽不到，"
                 "数字溯源统计因此失真。回归断言：正文含英文句点与小数时，"
                 "数字仍应被识别并参与溯源分类。"},
    15: {"verification": NEEDS_HUMAN,
         "reason": "图表数据抽取的实体/口径误归属，需要人工判断某个数值应归属哪个主体"
                   "与哪个报告期；没有人工标注就没有正确答案。"},
    16: {"verification": DETERMINISTIC,
         "subject": "希兹维恩量子玄武科技", "report_type": "company_research",
         "query": "希兹维恩量子玄武科技投资分析",
         "fixture_scenario": "fictional_entity",
         "expected_outcome": ExpectedOutcome.INSUFFICIENT_EVIDENCE,
         "required_tools": [SEARCH], "forbidden_tools": [EXPORT],
         "degraded_ok": False,
         "forbidden_claims": ["营业收入", "净利润", "目标价"],
         "note": "虚构公司主题也能凑出 5 个『相关』来源——相关性层被金融词汇骗过。"
                 "回归断言：必须由实体校验拦截并拒绝出报告，不得出现任何财务数值。"},
    17: {"verification": NEEDS_NETWORK,
         "reason": "LLM 配额耗尽只能在真实 provider 下复现；离线 stub 不会返回 429。"},
    18: {"verification": DETERMINISTIC,
         "subject": "中芯国际", "report_type": "company_research",
         "query": "中芯国际投资风险分析",
         "fixture_scenario": "akshare_down",
         "expected_outcome": ExpectedOutcome.REPORT_WITH_CAVEATS,
         "required_tools": [SEARCH, READ], "degraded_ok": True,
         "max_expected_tool_calls": 24,
         "note": "AkShare 东方财富系接口全部 ProxyError。回归断言：结构化数据源不可用时"
                 "应降级为纯网页分析并如实标注缺失字段，而不是整体失败。"},
    19: {"verification": NOT_AGENT,
         "reason": "相对估值缺少可靠免费行业可比数据源，是外部数据可得性限制，"
                   "不是系统缺陷，无法通过修复消除。"},
    20: {"verification": NOT_AGENT,
         "reason": "`str.replace` 打补丁静默失败是开发过程失误（改动未生效），"
                   "属于工具链问题；现已由测试覆盖终止文案本身。"},
    21: {"verification": NEEDS_HUMAN,
         "reason": "数字 grounding 的离线报告做不了数值核对——要判断报告里的数值是否"
                   "与来源一致，必须有人去读来源。"},
    22: {"verification": DETERMINISTIC,
         "subject": "美的集团", "report_type": "company_research",
         "query": "美的集团投资分析",
         "fixture_scenario": "default",
         "expected_outcome": ExpectedOutcome.REPORT_GENERATED,
         "required_tools": [SEARCH, READ], "degraded_ok": False,
         "note": "PDF 导出依赖 HTML 报告，markdown-only run 无 PDF 可导。"
                 "回归断言：markdown 输出的 run 必须正常产出报告文件，"
                 "不因导出格式缺失而失败。"},
    23: {"verification": DETERMINISTIC,
         "subject": "星河量子能源股份有限公司", "report_type": "company_research",
         "query": "星河量子能源股份有限公司投资价值分析",
         "fixture_scenario": "fictional_entity",
         "expected_outcome": ExpectedOutcome.INSUFFICIENT_EVIDENCE,
         "required_tools": [SEARCH], "forbidden_tools": [EXPORT],
         "degraded_ok": False,
         "forbidden_claims": ["营业收入", "净利润", "目标价", "建议买入"],
         "note": "虚构公司 E2E 纵深防御实录：相关性层被骗、实体层拦截。"
                 "回归断言与 Bad Case 16 同类，但用另一个虚构主体，"
                 "确保拦截不是靠硬编码名单。"},
    24: {"verification": NOT_AGENT,
         "reason": "memory 与语义层共享 embedding 模型导致进程冷启动慢，属于性能问题。"},
    25: {"verification": NEEDS_HUMAN,
         "reason": "行业生命周期判断被『证据不足』分支误吞掉纯负增长证据——"
                   "要判断某组证据是否足以支撑生命周期结论，需要行业分析师判断。"},
    26: {"verification": NEEDS_HUMAN,
         "reason": "季度跟踪报告现金流对比用错基期（上一期 vs 上年同期），"
                   "正确基期是会计口径问题，需要人工确认。"},
    27: {"verification": NEEDS_NETWORK,
         "reason": "新浪三表科目名在不同报告期存在文本变体，只能用真实接口返回复现。"},
    28: {"verification": NEEDS_NETWORK,
         "reason": "宏观社融数据源 SSL 证书失败，依赖真实 TLS 握手。"},
    29: {"verification": NOT_AGENT,
         "reason": "同业板块映射首次构建耗时长，属于性能与缓存预热问题。"},
    30: {"verification": NOT_AGENT,
         "reason": "PowerShell 测试脚本对中文请求体编码错误，是测试脚手架问题，"
                   "bad_cases.md 原文已注明『非产品缺陷』。"},
    31: {"verification": DETERMINISTIC,
         "subject": "CPI与PPI", "report_type": "macro_research",
         "query": "CPI与PPI走势分析",
         "fixture_scenario": "default",
         "expected_outcome": ExpectedOutcome.REPORT_WITH_CAVEATS,
         "required_tools": [SEARCH, READ], "forbidden_tools": [AKSHARE],
         "degraded_ok": True, "max_expected_tool_calls": 20,
         "note": "entity_validator 曾把宏观/未收录同义词的行业主题误判为 "
                 "insufficient_entity_evidence，一次全量跑 11/30 失败。"
                 "回归断言：宏观主题不得被实体校验拦截，必须正常产出报告。"},
}

_CATEGORY_BY_REPORT_TYPE = {
    "company_research": Category.COMPANY_RESEARCH,
    "industry_research": Category.INDUSTRY_RESEARCH,
    "macro_research": Category.MACRO_RESEARCH,
}


def _slug(text: str, limit: int = 24) -> str:
    import re

    return (re.sub(r"[^\w一-鿿]+", "_", text).strip("_") or "task")[:limit]


def build_rebuilt_regression_tasks() -> tuple[list[EvalTask], list[dict[str, Any]]]:
    """Returns (executable tasks, manifest rows for every bad case)."""
    titles = {c["number"]: c["title"] for c in parse_bad_cases()}
    tasks: list[EvalTask] = []
    manifest: list[dict[str, Any]] = []

    for number in sorted(BAD_CASE_MAP):
        spec = BAD_CASE_MAP[number]
        title = titles.get(number, "")
        verification = spec["verification"]
        row: dict[str, Any] = {
            "bad_case": number,
            "title": title,
            "verification": verification,
            "source_reference": f"Bad Case {number}",
            "task_id": None,
            "reason": spec.get("reason", ""),
        }

        if verification != DETERMINISTIC:
            manifest.append(row)
            continue

        report_type = spec["report_type"]
        task_id = f"reg{number:03d}_{_slug(spec['subject'])}"
        task = EvalTask(
            task_id=task_id,
            category=_CATEGORY_BY_REPORT_TYPE.get(report_type, Category.COMPANY_RESEARCH),
            query=spec["query"],
            report_type=report_type,
            expected_outcome=spec["expected_outcome"],
            required_tools=list(spec.get("required_tools", [])),
            forbidden_tools=list(spec.get("forbidden_tools", [])),
            forbidden_claims=list(spec.get("forbidden_claims", [])),
            required_facts=[RequiredFact(**f) for f in spec.get("required_facts", [])],
            max_expected_tool_calls=spec.get("max_expected_tool_calls"),
            fixture_scenario=spec["fixture_scenario"],
            task_constraints={
                "note": spec["note"],
                "degraded_acceptable": bool(spec.get("degraded_ok", False)),
                "original_defect": title,
            },
            tags=["regression", "bad_case", f"bad_case_{number}", "rebuilt"],
            data_source="eval/bad_cases.md (rebuilt into an executable request)",
            source_reference=f"Bad Case {number}",
            # The *defect* is real and reproduced; the *expectation* written here
            # is authored, not reviewed. That is exactly `needs_human_review`.
            gold_status=GoldStatus.NEEDS_HUMAN_REVIEW,
            group_key=f"bad_case_{number}",
            split=Split.REGRESSION,
        )
        tasks.append(task)
        row["task_id"] = task_id
        row["fixture_scenario"] = spec["fixture_scenario"]
        row["expected_outcome"] = spec["expected_outcome"].value
        manifest.append(row)

    return tasks, manifest


def main() -> int:
    parser = argparse.ArgumentParser(description="Rebuild the bad-case regression set")
    parser.add_argument("--write", action="store_true")
    args = parser.parse_args()

    tasks, manifest = build_rebuilt_regression_tasks()
    counts = Counter(r["verification"] for r in manifest)

    print(f"bad cases mapped: {len(manifest)}")
    for key in (DETERMINISTIC, NEEDS_HUMAN, NEEDS_NETWORK, NOT_AGENT):
        print(f"  {key:22s} {counts.get(key, 0)}")
    print(f"executable tasks emitted: {len(tasks)}")
    print(f"  declaring required_tools    : {sum(1 for t in tasks if t.required_tools)}")
    print(f"  declaring forbidden_tools   : {sum(1 for t in tasks if t.forbidden_tools)}")
    print(f"  declaring forbidden_claims  : {sum(1 for t in tasks if t.forbidden_claims)}")
    print(f"  declaring required_facts    : {sum(1 for t in tasks if t.required_facts)}")
    print(f"  with fault injection        : {sum(1 for t in tasks if t.fixture_scenario != 'default')}")
    print(f"  gold_status                 : {sorted({t.gold_status.value for t in tasks})}")

    missing = sorted(set(range(1, 32)) - set(BAD_CASE_MAP))
    if missing:
        print(f"  !! bad cases with no mapping: {missing}")

    if args.write:
        EvalDataset(
            name="regression_rebuilt",
            description=("由 eval/bad_cases.md 重构而来的可执行回归集。每条都是真实研究请求 + "
                         "复现原缺陷条件的故障注入，而不是把缺陷标题当研究主题。"
                         "无法复现为 agent 行为的 bad case 如实归类并排除，见 "
                         "regression_manifest.json。全部 needs_human_review："
                         "缺陷是真的，但这里写下的期望值未经人工复核。"),
            tasks=tasks).save(OUT_PATH)
        MANIFEST_PATH.write_text(
            json.dumps({"total_bad_cases": len(manifest),
                        "by_verification": dict(counts),
                        "rows": manifest}, ensure_ascii=False, indent=2),
            encoding="utf-8")
        print(f"wrote {OUT_PATH}")
        print(f"wrote {MANIFEST_PATH}")
    else:
        print("(dry run; pass --write)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
