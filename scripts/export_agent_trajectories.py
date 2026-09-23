"""Export agent-trajectory training data from verified traces.

    python scripts/export_agent_trajectories.py --results evals/results/dev_harness.jsonl
    python scripts/export_agent_trajectories.py --results ... --out-dir exports/agent_sft

Seven datasets, all derived from real runs plus deterministic grader labels:

    task_complexity      task text          -> complexity class
    tool_selection       state + catalog    -> which tool to call next
    tool_arguments       state + tool spec  -> the argument object
    clarification        task text          -> ask / proceed
    continue_search      state after round  -> search more / stop
    stop_decision        terminal state     -> why the run stopped
    failure_reason       failed run signals -> failure mode

Hard rules enforced here, not left to reviewers:

- **No hidden reasoning is ever exported.** The trace never records it
  (`observability/events.py` drops those keys), and `_assert_no_reasoning`
  re-checks every record before it is written. Only task input, visible context
  summaries, the tool catalog, the action taken, its arguments, and a
  verifiable outcome label.
- **Labels come from graders or humans, never from the model's own claim.**
  Each record carries `label_source` and `verified`, and the Data Card reports
  the split between machine-verified and human-reviewed.
- **Deduplicated and grouped-split.** Splitting is by subject group so the same
  company cannot appear in both train and eval, and every record's group is
  recorded so leakage is checkable after the fact.
- **Test-set traces are excluded** from training data by default, so the held-out
  eval split stays held out.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")

from evals.datasets.schema import EvalDataset  # noqa: E402
from src.observability.trace_store import TraceStore  # noqa: E402
from src.optimization.failure_miner import build_signal  # noqa: E402
from src.optimization.failure_taxonomy import classify  # noqa: E402
from src.tools.registry import default_registry  # noqa: E402

DEFAULT_OUT = Path(__file__).resolve().parent.parent / "exports" / "agent_sft"

DATASETS = ("task_complexity", "tool_selection", "tool_arguments", "clarification",
            "continue_search", "stop_decision", "failure_reason")

#: Keys that would indicate hidden reasoning leaked into a record.
_FORBIDDEN_KEYS = re.compile(
    r"^(reasoning|reasoning_content|thinking|thought|thoughts|chain_of_thought|cot|scratchpad)$",
    re.I)


class ExportError(RuntimeError):
    pass


def _assert_no_reasoning(record: dict[str, Any], path: str = "$") -> None:
    """Fail loudly rather than exporting a record that carries hidden reasoning."""
    for key, value in record.items():
        if _FORBIDDEN_KEYS.match(str(key)):
            raise ExportError(f"{path}.{key}: reasoning-bearing field must never be exported")
        if isinstance(value, dict):
            _assert_no_reasoning(value, f"{path}.{key}")
        elif isinstance(value, list):
            for i, item in enumerate(value):
                if isinstance(item, dict):
                    _assert_no_reasoning(item, f"{path}.{key}[{i}]")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _record_id(dataset: str, payload: dict[str, Any]) -> str:
    basis = json.dumps([dataset, payload.get("input"), payload.get("output")],
                       ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()[:16]


# --------------------------------------------------------------------------- #
def _complexity_label(row: dict[str, Any], events: list[dict[str, Any]]) -> str:
    """Complexity from what the run actually needed, not from the prompt's looks."""
    tool_calls = len([e for e in events if e.get("event_type") == "tool_call_started"])
    sources = int(row.get("num_sources", 0) or 0)
    if tool_calls <= 3 and sources <= 2:
        return "simple"
    if tool_calls <= 8 and sources <= 5:
        return "moderate"
    return "complex"


def _tool_state_summary(events: list[dict[str, Any]], upto_seq: int) -> dict[str, Any]:
    """Visible state before the call at `upto_seq`. No hidden reasoning."""
    prior = [e for e in events if int(e.get("seq", 0)) < upto_seq]
    tool_calls = [e for e in prior if e.get("event_type") == "tool_call_started"]
    completed = [e for e in prior if e.get("event_type") == "tool_call_completed"]
    failed = [e for e in prior if e.get("event_type") == "tool_call_failed"]
    phase = next((e.get("phase", "") for e in reversed(prior) if e.get("phase")), "")
    return {
        "phase": phase,
        "tool_calls_so_far": len(tool_calls),
        "tools_used": sorted({e.get("name", "") for e in tool_calls}),
        "successful_calls": len(completed),
        "failed_calls": len(failed),
        "last_error_class": next((e.get("error_class", "") for e in reversed(failed)), ""),
        "evidence_count": sum(int(e.get("payload", {}).get("total", 0) or 0)
                              for e in prior if e.get("event_type") == "evidence_added"),
    }


def build_records(row: dict[str, Any], events: list[dict[str, Any]],
                  catalog: list[dict[str, Any]], group: str) -> dict[str, list[dict[str, Any]]]:
    """All datasets' records for one run."""
    out: dict[str, list[dict[str, Any]]] = {name: [] for name in DATASETS}
    query = row.get("query") or row.get("task_id", "")
    report_type = row.get("report_type", "")
    verified = row.get("gold_status") == "human_verified"
    label_source = "human" if verified else "deterministic_grader"
    base = {"run_id": row.get("run_id", ""), "task_id": row.get("task_id", ""),
            "group": group, "split_hint": row.get("split", ""), "verified": verified,
            "label_source": label_source, "arm": row.get("arm", "")}

    # 1. task complexity
    out["task_complexity"].append({**base,
        "input": {"query": query, "report_type": report_type},
        "output": {"complexity": _complexity_label(row, events)},
        "rationale_label": "derived from the tool-call count and source count the run actually needed"})

    # 2/3. tool selection and arguments, one record per real call
    tool_names = [t["name"] for t in catalog]
    for event in events:
        if event.get("event_type") != "tool_call_started":
            continue
        arguments = (event.get("payload", {}) or {}).get("arguments", {})
        completed = any(e.get("event_type") == "tool_call_completed"
                        and e.get("step_id") == event.get("step_id") for e in events)
        state = _tool_state_summary(events, int(event.get("seq", 0)))

        out["tool_selection"].append({**base,
            "input": {"query": query, "report_type": report_type, "state": state,
                      "tool_catalog": catalog},
            "output": {"tool": event.get("name", "")},
            "candidates": tool_names,
            "outcome_verified": completed})
        out["tool_arguments"].append({**base,
            "input": {"query": query, "tool": event.get("name", ""), "state": state,
                      "tool_schema": next((t["input_schema"] for t in catalog
                                           if t["name"] == event.get("name")), {})},
            "output": {"arguments": arguments},
            "outcome_verified": completed})

    # 4. clarification: ask, or proceed? Labelled by what the run needed.
    needed_clarification = row.get("stop_reason") in ("entity_unverified", "no_usable_sources")
    out["clarification"].append({**base,
        "input": {"query": query, "report_type": report_type},
        "output": {"action": "ask_user" if needed_clarification else "proceed"},
        "rationale_label": ("the run could not verify the subject or find any usable source, "
                            "so a clarifying question would have been the better first move"
                            if needed_clarification else
                            "the run proceeded and produced a grounded report")})

    # 5. continue searching, or stop? One record per completed search round.
    search_events = [e for e in events
                     if e.get("event_type") == "tool_call_completed" and e.get("name") == "web_search"]
    for index, event in enumerate(search_events):
        state = _tool_state_summary(events, int(event.get("seq", 0)))
        more_follow = index < len(search_events) - 1
        out["continue_search"].append({**base,
            "input": {"query": query, "round": index + 1, "state": state},
            "output": {"action": "continue" if more_follow else "stop"},
            "outcome_verified": row.get("status") in ("succeeded", "degraded")})

    # 6. stop decision - taken from the run's explicit stop_decision event
    stop_events = [e for e in events if e.get("event_type") == "stop_decision"]
    if stop_events:
        payload = stop_events[-1].get("payload", {})
        out["stop_decision"].append({**base,
            "input": {"query": query, "state": _tool_state_summary(events, 10**9)},
            "output": {"stop_reason": payload.get("reason", ""),
                       "evidence_count": payload.get("evidence_count", 0)},
            "outcome_verified": True})

    # 7. failure reason - only for runs that actually failed
    if row.get("status") not in ("succeeded",):
        classification = classify(build_signal(row, events))
        if classification is not None:
            out["failure_reason"].append({**base,
                "input": {"query": query, "status": row.get("status", ""),
                          "stop_reason": row.get("stop_reason", ""),
                          "state": _tool_state_summary(events, 10**9)},
                "output": {"failure_mode": classification.primary.value,
                           "contributing": [m.value for m in classification.contributing]},
                "outcome_verified": True})

    return out


# --------------------------------------------------------------------------- #
def _group_for(task_id: str, dataset: Optional[EvalDataset]) -> str:
    if dataset is not None:
        for task in dataset.tasks:
            if task.task_id == task_id:
                return task.effective_group()
    return task_id


#: Salt that makes the SFT split independent of the eval-dataset split. Without
#: it both use the same hash, and since the dev split is exactly the groups that
#: hashed below 60, every one of them also lands below the 80 train cutoff -
#: producing a train-only export with no eval half at all.
_SFT_SPLIT_SALT = "agent-sft-v1"


def _split_for(group: str, train_ratio: float = 0.8) -> str:
    """Deterministic group-level split, independent of the eval-dataset split."""
    digest = hashlib.sha256(f"{_SFT_SPLIT_SALT}|{group}".encode("utf-8")).hexdigest()
    return "train" if int(digest[:8], 16) % 100 < int(train_ratio * 100) else "eval"


def export(results_path: Path, out_dir: Path, *, dataset_path: Optional[Path] = None,
           exclude_splits: tuple[str, ...] = ("test",)) -> dict[str, Any]:
    out_dir.mkdir(parents=True, exist_ok=True)
    catalog = default_registry().catalog()

    dataset: Optional[EvalDataset] = None
    if dataset_path and Path(dataset_path).exists():
        dataset = EvalDataset.load(Path(dataset_path))

    rows: list[dict[str, Any]] = []
    with Path(results_path).open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    continue

    excluded = [r for r in rows if r.get("split") in exclude_splits]
    rows = [r for r in rows if r.get("split") not in exclude_splits]

    collected: dict[str, list[dict[str, Any]]] = {name: [] for name in DATASETS}
    runs_used = 0
    for row in rows:
        trace_path = row.get("trace_path")
        events = TraceStore.read(Path(trace_path)) if trace_path and Path(trace_path).exists() else []
        if not events:
            continue
        runs_used += 1
        group = _group_for(row.get("task_id", ""), dataset)
        for name, records in build_records(row, events, catalog, group).items():
            collected[name].extend(records)

    # Dedup + split + integrity check
    stats: dict[str, Any] = {}
    for name, records in collected.items():
        seen: set[str] = set()
        unique: list[dict[str, Any]] = []
        duplicates = 0
        for record in records:
            _assert_no_reasoning(record)
            record_id = _record_id(name, record)
            if record_id in seen:
                duplicates += 1
                continue
            seen.add(record_id)
            record["record_id"] = record_id
            record["split"] = _split_for(record["group"])
            unique.append(record)

        path = out_dir / f"{name}.jsonl"
        with path.open("w", encoding="utf-8", newline="\n") as handle:
            for record in unique:
                handle.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")

        label_counts = Counter(
            json.dumps(r["output"], ensure_ascii=False, sort_keys=True) for r in unique)
        splits = Counter(r["split"] for r in unique)
        groups_by_split: dict[str, set[str]] = defaultdict(set)
        for record in unique:
            groups_by_split[record["split"]].add(record["group"])
        leakage = sorted(groups_by_split.get("train", set()) & groups_by_split.get("eval", set()))

        stats[name] = {
            "records": len(unique), "duplicates_dropped": duplicates,
            "by_split": dict(splits),
            "verified_records": sum(1 for r in unique if r.get("verified")),
            "distinct_labels": len(label_counts),
            "most_common_labels": label_counts.most_common(5),
            "group_leakage": leakage,
            "path": str(path),
        }

    return {
        "generated_at": _now(),
        "results_path": str(results_path),
        "runs_in_results": len(rows) + len(excluded),
        "runs_used": runs_used,
        "runs_excluded_by_split": len(excluded),
        "excluded_splits": list(exclude_splits),
        "datasets": stats,
        "tool_catalog_size": len(catalog),
    }


# --------------------------------------------------------------------------- #
def render_data_card(report: dict[str, Any]) -> str:
    total = sum(d["records"] for d in report["datasets"].values())
    verified = sum(d["verified_records"] for d in report["datasets"].values())
    leaks = {name: d["group_leakage"] for name, d in report["datasets"].items() if d["group_leakage"]}

    lines = [
        "# Data Card：Agent 轨迹训练数据", "",
        f"- 生成时间：{report['generated_at']}",
        f"- 来源结果文件：`{report['results_path']}`",
        f"- 结果中的运行数：{report['runs_in_results']}；实际使用：{report['runs_used']}；"
        f"因属于保留 split（{report['excluded_splits']}）被排除：{report['runs_excluded_by_split']}",
        f"- 工具目录规模：{report['tool_catalog_size']}（仅 Agent 可选择的工具）",
        f"- 记录总数：**{total}**，其中经人工复核：**{verified}**", "",
        "## 内容与来源", "",
        "全部记录来自真实运行的结构化 trace，加上确定性 grader 给出的标签。**不包含任何模型隐藏推理**："
        "trace 本身在写入时就会丢弃 reasoning 类字段（`src/observability/events.py`），"
        "导出前再用 `_assert_no_reasoning` 逐条复查，命中即报错终止导出。",
        "",
        "每条记录包含：任务输入、可见状态摘要、工具目录、实际动作、工具参数、可验证结果、标签来源。",
        "",
        "## 各数据集", "",
        "| 数据集 | 记录数 | train/eval | 去重丢弃 | 人工复核 | 标签种类 | 组泄漏 |",
        "|---|---|---|---|---|---|---|",
    ]
    for name, item in report["datasets"].items():
        splits = "/".join(f"{k}:{v}" for k, v in sorted(item["by_split"].items())) or "—"
        leak = "、".join(item["group_leakage"][:3]) if item["group_leakage"] else "无"
        lines.append(f"| `{name}` | {item['records']} | {splits} | {item['duplicates_dropped']} | "
                     f"{item['verified_records']} | {item['distinct_labels']} | {leak} |")

    lines += ["", "## 切分与泄漏", "",
              "按 `group`（公司/行业/主题）做 train/eval 切分，同一主体不会同时出现在两侧；"
              "切分用 sha256 分桶，跨进程可复现。"]
    lines.append("检测结果：" + ("**未发现组泄漏**。" if not leaks else
                              f"**发现泄漏**：{json.dumps(leaks, ensure_ascii=False)}，必须修复后再训练。"))

    lines += ["", "## 已知限制（必须与数据一并交付）", "",
              f"1. 人工复核记录数为 **{verified}**。" +
              ("当前为 0，因此这批数据只能作为 SFT 的**弱监督**素材，"
               "不能声称是人工标注的高质量数据集。" if verified == 0 else ""),
              "2. 标签由确定性 grader 推导，反映“这次运行实际做了什么、结果如何”，"
              "不等于“最优动作”。用它训练路由器学到的是**复现当前系统的行为**，"
              "而不是超越它。",
              "3. 离线 fixture 世界产生的轨迹在工具返回分布上比真实网络窄，"
              "直接用于线上路由前需要用 `--live` 轨迹补充。",
              "4. 未做类别平衡；`most_common_labels` 显示分布高度偏斜的数据集需要重采样。",
              "5. 本数据集**未用于训练任何已发布模型**；训练脚本与配置见 `training/`，"
              "是否值得训练要看 `training/router_comparison.py` 的对比结果。",
              ]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Export agent trajectory training data")
    parser.add_argument("--results", type=Path, required=True)
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--dataset", type=Path,
                        default=Path("evals/datasets/all_tasks.json"))
    parser.add_argument("--include-test-split", action="store_true",
                        help="DANGER: include held-out test-split runs in the training export")
    args = parser.parse_args()

    exclude = () if args.include_test_split else ("test",)
    if args.include_test_split:
        print("[warn] test-split runs are being included; the held-out split is no longer held out")

    report = export(args.results, args.out_dir, dataset_path=args.dataset,
                    exclude_splits=exclude)
    card_path = args.out_dir / "DATA_CARD.md"
    card_path.write_text(render_data_card(report), encoding="utf-8")
    (args.out_dir / "export_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    print(json.dumps({k: v for k, v in report.items() if k != "datasets"},
                     ensure_ascii=False, indent=2))
    for name, item in report["datasets"].items():
        print(f"  {name:18s} {item['records']:5d} records  splits={item['by_split']}  "
              f"leakage={item['group_leakage'] or 'none'}")
    print(f"wrote {card_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
