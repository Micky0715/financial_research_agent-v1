"""Compare tool-router strategies on the exported trajectory data.

    python training/router_comparison.py --data exports/agent_sft --split eval
    python training/router_comparison.py --data exports/agent_sft --arms prompt_only,few_shot --live

Arms:
    heuristic     the current deterministic pipeline's routing, as a floor
    prompt_only   zero-shot LLM router given the tool catalog          (--live)
    few_shot      the same, with k examples drawn from the train split (--live)
    lora_sft      the fine-tuned Qwen3-1.7B adapter                    (--adapter)
    large_model   the full model as the routing ceiling                (--live)
    cascade       small model first, escalate to large on low confidence

Metrics: Tool Selection F1, Argument Exact Match, Invalid Tool Call Rate,
Clarification Accuracy, Stop Decision Accuracy, average latency, average cost,
and escalation rate for the cascade.

What this deliberately does *not* do: report a number for an arm it could not
actually run. An arm without its dependency (no API key, no adapter) is
reported as `skipped` with the reason, never as a zero or an estimate.
"""
from __future__ import annotations

import argparse
import json
import random
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")

from src.runtime.budget import estimate_cost, estimate_tokens  # noqa: E402
from src.tools.registry import default_registry  # noqa: E402
from src.tools.schemas import validate  # noqa: E402

DEFAULT_DATA = Path(__file__).resolve().parent.parent / "exports" / "agent_sft"
ARMS = ("heuristic", "prompt_only", "few_shot", "lora_sft", "large_model", "cascade")


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    return rows


class RouterResult:
    """One arm's predictions plus what it cost to make them."""

    def __init__(self, arm: str) -> None:
        self.arm = arm
        self.skipped_reason = ""
        self.tool_pred: list[tuple[str, str]] = []      # (gold, predicted)
        self.arg_pred: list[tuple[dict, dict, str]] = []  # (gold, predicted, tool)
        self.clarify_pred: list[tuple[str, str]] = []
        self.stop_pred: list[tuple[str, str]] = []
        self.latencies_ms: list[float] = []
        self.cost_usd: float = 0.0
        self.escalations: int = 0
        self.decisions: int = 0

    # ------------------------------------------------------------------ #
    def tool_metrics(self) -> dict[str, Any]:
        if not self.tool_pred:
            return {"tool_selection_f1": None, "tool_selection_accuracy": None}
        labels = sorted({g for g, _ in self.tool_pred} | {p for _, p in self.tool_pred})
        f1s = []
        for label in labels:
            tp = sum(1 for g, p in self.tool_pred if g == label and p == label)
            fp = sum(1 for g, p in self.tool_pred if g != label and p == label)
            fn = sum(1 for g, p in self.tool_pred if g == label and p != label)
            precision = tp / (tp + fp) if (tp + fp) else 0.0
            recall = tp / (tp + fn) if (tp + fn) else 0.0
            f1s.append(2 * precision * recall / (precision + recall) if (precision + recall) else 0.0)
        accuracy = sum(1 for g, p in self.tool_pred if g == p) / len(self.tool_pred)
        return {"tool_selection_f1": round(sum(f1s) / len(f1s), 4),
                "tool_selection_accuracy": round(accuracy, 4)}

    def argument_metrics(self) -> dict[str, Any]:
        if not self.arg_pred:
            return {"argument_exact_match": None, "invalid_tool_call_rate": None}
        registry = default_registry()
        exact = 0
        invalid = 0
        for gold, predicted, tool in self.arg_pred:
            if gold == predicted:
                exact += 1
            try:
                schema = registry.spec(tool).input_schema
                if validate(predicted, schema):
                    invalid += 1
            except Exception:  # noqa: BLE001 - unknown tool counts as invalid
                invalid += 1
        return {"argument_exact_match": round(exact / len(self.arg_pred), 4),
                "invalid_tool_call_rate": round(invalid / len(self.arg_pred), 4)}

    def _accuracy(self, pairs: list[tuple[str, str]]) -> Optional[float]:
        if not pairs:
            return None
        return round(sum(1 for g, p in pairs if g == p) / len(pairs), 4)

    def summary(self) -> dict[str, Any]:
        if self.skipped_reason:
            return {"arm": self.arm, "status": "skipped", "reason": self.skipped_reason}
        return {
            "arm": self.arm, "status": "measured", "decisions": self.decisions,
            **self.tool_metrics(), **self.argument_metrics(),
            "clarification_accuracy": self._accuracy(self.clarify_pred),
            "stop_decision_accuracy": self._accuracy(self.stop_pred),
            "avg_latency_ms": (round(sum(self.latencies_ms) / len(self.latencies_ms), 2)
                               if self.latencies_ms else 0.0),
            "avg_cost_usd": (round(self.cost_usd / self.decisions, 8) if self.decisions else 0.0),
            "escalation_rate": (round(self.escalations / self.decisions, 4)
                                if self.decisions else 0.0),
        }


# --------------------------------------------------------------------------- #
# Arms
# --------------------------------------------------------------------------- #
def _heuristic_tool(record: dict[str, Any]) -> str:
    """The current pipeline's deterministic routing, reproduced as a floor.

    Phase decides the tool: research searches, browse reads, analyze fetches
    structured data. That is exactly what the production system does, so this
    arm is the honest baseline any learned router must beat.
    """
    state = record.get("input", {}).get("state", {})
    phase = state.get("phase", "")
    if phase == "research":
        return "web_search"
    if phase == "browse":
        return "read_webpage"
    if phase == "analyze":
        return "fetch_financial_snapshot"
    return "web_search"


def run_heuristic(records: dict[str, list[dict[str, Any]]]) -> RouterResult:
    result = RouterResult("heuristic")
    started = time.perf_counter()
    for record in records["tool_selection"]:
        result.tool_pred.append((record["output"]["tool"], _heuristic_tool(record)))
        result.decisions += 1
    for record in records["tool_arguments"]:
        gold = record["output"]["arguments"]
        # The heuristic has no argument model; it reuses the recorded arguments,
        # which makes its exact-match trivially 1.0. Reported, but flagged.
        result.arg_pred.append((gold, gold, record["input"]["tool"]))
    for record in records["clarification"]:
        result.clarify_pred.append((record["output"]["action"], "proceed"))
    for record in records["stop_decision"]:
        result.stop_pred.append((record["output"]["stop_reason"], "completed"))
    elapsed_ms = (time.perf_counter() - started) * 1000
    result.latencies_ms = [elapsed_ms / max(1, result.decisions)] * max(1, result.decisions)
    return result


def _llm_router(arm: str, records: dict[str, list[dict[str, Any]]], *, model: str,
                few_shot: Optional[list[dict[str, Any]]] = None,
                limit: int = 50) -> RouterResult:
    """Prompt-based routing over a real model. Requires --live."""
    result = RouterResult(arm)
    try:
        import litellm  # noqa: F401

        from config import config
    except Exception as exc:  # noqa: BLE001
        result.skipped_reason = f"litellm/config unavailable: {exc}"
        return result
    if not (config.OPENAI_API_KEY or config.DEEPSEEK_API_KEY or config.ANTHROPIC_API_KEY):
        result.skipped_reason = "no API key configured; cannot measure this arm"
        return result

    from agents.base_agent import BaseAgent

    catalog = default_registry().catalog()
    examples = ""
    if few_shot:
        examples = "\n\n参考示例：\n" + "\n".join(
            f"状态：{json.dumps(e['input']['state'], ensure_ascii=False)} -> {e['output']['tool']}"
            for e in few_shot[:5])

    for record in records["tool_selection"][:limit]:
        prompt = (
            f"可用工具：{json.dumps(catalog, ensure_ascii=False)}\n\n"
            f"研究任务：{record['input'].get('query', '')}\n"
            f"当前状态：{json.dumps(record['input'].get('state', {}), ensure_ascii=False)}"
            f"{examples}\n\n只输出下一步应调用的工具名，不要输出其他内容。")
        started = time.perf_counter()
        try:
            raw = BaseAgent.call_llm(prompt, system="你是工具路由器，只输出工具名。", temperature=0.0)
        except Exception as exc:  # noqa: BLE001
            result.skipped_reason = f"model call failed: {type(exc).__name__}: {exc}"
            return result
        result.latencies_ms.append((time.perf_counter() - started) * 1000)
        predicted = (raw or "").strip().split()[0] if raw.strip() else ""
        result.tool_pred.append((record["output"]["tool"], predicted))
        result.decisions += 1
        cost, _ = estimate_cost(model, estimate_tokens(prompt), estimate_tokens(raw))
        result.cost_usd += cost
    return result


def run_lora(records: dict[str, list[dict[str, Any]]], adapter: Optional[Path]) -> RouterResult:
    result = RouterResult("lora_sft")
    if adapter is None or not Path(adapter).exists():
        result.skipped_reason = (
            "no LoRA adapter supplied. Train one with "
            "`python training/train_router_lora.py --config training/qwen3_1_7b_router_lora.yaml` "
            "(requires a GPU and a reviewed dataset), then pass --adapter <path>.")
        return result
    result.skipped_reason = (
        "adapter path exists but inference is not wired up in this build; "
        "implement load + generate here before quoting any lora_sft number")
    return result


def run_cascade(small: RouterResult, large: RouterResult, *,
                confidence_threshold: float = 0.75) -> RouterResult:
    """Small model first, escalate to the large one when it is unsure.

    Confidence proxy: the small arm is considered confident when its prediction
    is a tool that is valid for the current phase. Without token logprobs this
    is the honest available signal, and it is labelled as a proxy.
    """
    result = RouterResult("cascade")
    if small.skipped_reason or large.skipped_reason:
        result.skipped_reason = (f"requires both arms; small={small.skipped_reason or 'ok'}, "
                                 f"large={large.skipped_reason or 'ok'}")
        return result

    registry = default_registry()
    valid_tools = set(registry.names(selectable_only=True))
    for index, (gold, small_pred) in enumerate(small.tool_pred):
        confident = small_pred in valid_tools
        if confident:
            result.tool_pred.append((gold, small_pred))
            result.latencies_ms.append(small.latencies_ms[index] if index < len(small.latencies_ms) else 0.0)
        else:
            result.escalations += 1
            large_pred = large.tool_pred[index][1] if index < len(large.tool_pred) else small_pred
            result.tool_pred.append((gold, large_pred))
            result.latencies_ms.append(
                (small.latencies_ms[index] if index < len(small.latencies_ms) else 0.0)
                + (large.latencies_ms[index] if index < len(large.latencies_ms) else 0.0))
        result.decisions += 1
    denominator = max(1, small.decisions)
    result.cost_usd = small.cost_usd + large.cost_usd * (result.escalations / denominator)
    return result


# --------------------------------------------------------------------------- #
def render_markdown(summaries: list[dict[str, Any]], meta: dict[str, Any]) -> str:
    lines = ["# 工具路由器方案对比", "",
             f"- 生成时间：{meta['generated_at']}",
             f"- 数据：`{meta['data_dir']}`（split=`{meta['split']}`）",
             f"- 评估样本：tool_selection {meta['counts'].get('tool_selection', 0)} 条、"
             f"tool_arguments {meta['counts'].get('tool_arguments', 0)} 条", "",
             "| 方案 | 状态 | Tool F1 | Arg Exact Match | Invalid Tool Call | "
             "澄清准确率 | 停止判断准确率 | 平均时延(ms) | 平均费用(USD) | 升级比例 |",
             "|---|---|---|---|---|---|---|---|---|---|"]

    def fmt(value: Any) -> str:
        if value is None:
            return "n/a"
        if isinstance(value, float):
            return f"{value:.4f}".rstrip("0").rstrip(".")
        return str(value)

    for item in summaries:
        if item["status"] == "skipped":
            lines.append(f"| `{item['arm']}` | **未运行** | — | — | — | — | — | — | — | — |")
            continue
        lines.append(
            f"| `{item['arm']}` | 已测量 | {fmt(item.get('tool_selection_f1'))} | "
            f"{fmt(item.get('argument_exact_match'))} | {fmt(item.get('invalid_tool_call_rate'))} | "
            f"{fmt(item.get('clarification_accuracy'))} | {fmt(item.get('stop_decision_accuracy'))} | "
            f"{fmt(item.get('avg_latency_ms'))} | {fmt(item.get('avg_cost_usd'))} | "
            f"{fmt(item.get('escalation_rate'))} |")

    skipped = [item for item in summaries if item["status"] == "skipped"]
    if skipped:
        lines += ["", "## 未运行的方案（如实列出，不用估计值填充）", ""]
        for item in skipped:
            lines.append(f"- `{item['arm']}`：{item['reason']}")

    lines += ["", "## 读法与限制", "",
              "- `heuristic` 是当前生产系统的确定性路由，是**下限**：任何学习型路由器如果打不过它，"
              "就没有引入的理由。",
              "- `heuristic` 的 Argument Exact Match 恒为 1.0 —— 它直接复用了记录中的参数，"
              "不是真的在生成参数，这一列对该行没有判别意义。",
              "- 标签来自“当前系统实际做了什么”，因此上限是**复现当前系统**，不是超越它。"
              "要突破这个上限需要人工标注的最优动作。",
              "- 未运行的方案一律标注为未运行，不用 0 或估计值填表。"]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Compare tool-router strategies")
    parser.add_argument("--data", type=Path, default=DEFAULT_DATA)
    parser.add_argument("--split", default="eval", choices=["train", "eval", "all"])
    parser.add_argument("--arms", default="heuristic,prompt_only,few_shot,lora_sft,large_model,cascade")
    parser.add_argument("--live", action="store_true", help="run the model-backed arms")
    parser.add_argument("--adapter", type=Path, default=None)
    parser.add_argument("--limit", type=int, default=50)
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args()

    records: dict[str, list[dict[str, Any]]] = {}
    for name in ("tool_selection", "tool_arguments", "clarification", "stop_decision",
                 "continue_search", "task_complexity"):
        rows = load_jsonl(args.data / f"{name}.jsonl")
        if args.split != "all":
            rows = [r for r in rows if r.get("split") == args.split]
        records[name] = rows

    if not records["tool_selection"]:
        raise SystemExit(
            f"no tool_selection records in {args.data} for split={args.split}; run "
            "`python scripts/export_agent_trajectories.py --results <eval results>` first")

    train_examples = [r for r in load_jsonl(args.data / "tool_selection.jsonl")
                      if r.get("split") == "train"]
    random.Random(42).shuffle(train_examples)

    wanted = [a.strip() for a in args.arms.split(",") if a.strip()]
    from config import config

    results: dict[str, RouterResult] = {}
    for arm in wanted:
        if arm == "heuristic":
            results[arm] = run_heuristic(records)
        elif arm in ("prompt_only", "few_shot", "large_model"):
            if not args.live:
                result = RouterResult(arm)
                result.skipped_reason = "requires --live and a configured model; not run"
                results[arm] = result
            else:
                results[arm] = _llm_router(
                    arm, records, model=config.MODEL_NAME,
                    few_shot=train_examples if arm == "few_shot" else None,
                    limit=args.limit)
        elif arm == "lora_sft":
            results[arm] = run_lora(records, args.adapter)
        elif arm == "cascade":
            small = results.get("lora_sft") or results.get("prompt_only") or RouterResult("small")
            large = results.get("large_model") or RouterResult("large")
            if not small.tool_pred:
                small = results.get("heuristic") or run_heuristic(records)
            results[arm] = run_cascade(small, large)

    summaries = [results[a].summary() for a in wanted if a in results]
    meta = {"generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "data_dir": str(args.data), "split": args.split,
            "counts": {k: len(v) for k, v in records.items()}}

    out_path = args.out or (Path(__file__).resolve().parent / "router_comparison.md")
    out_path.write_text(render_markdown(summaries, meta), encoding="utf-8")
    (out_path.with_suffix(".json")).write_text(
        json.dumps({"meta": meta, "arms": summaries}, ensure_ascii=False, indent=2),
        encoding="utf-8")

    print(json.dumps(summaries, ensure_ascii=False, indent=2))
    print(f"wrote {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
