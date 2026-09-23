"""Multi-session memory A/B evaluation.

The question this answers is the one that decides whether the memory system
ships: **does memory improve downstream task success, or does it only add
tokens and a new way to be wrong?**

Design: a *session sequence* per subject. Session 1 runs cold and, in the
memory arms, writes what it learned. Sessions 2..N run with whatever that arm's
strategy retrieves. The arms are:

    none          - no memory at all (control)
    recent_only   - inject the previous session's summary, nothing else
    vector_only   - similarity retrieval with no policy (no floor, no decay,
                    no usefulness feedback) - the "put it in a vector DB" design
    full          - the full policy: typed memories, relevance floor, decay,
                    conflict resolution, usefulness feedback

Metrics reported per arm:
  Memory Precision / Recall / Usefulness, Stale Memory Error Rate,
  Contradiction Resolution Accuracy, Cross-user Leakage Rate,
  downstream Task Success delta, and token/cost/latency deltas.

Precision and recall are measured against *planted* memories whose relevance to
each session is known by construction - that is the only way to get a ground
truth for retrieval without hand-labelling, and it is declared as such in the
output rather than presented as a natural-traffic measurement.

    python -m evals.runners.memory_ab --sessions 4 --subjects 6
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")

from evals.fixtures import install_fixture_tools  # noqa: E402
from evals.runners.replay import build_library  # noqa: E402
from evals.runners.run_eval import install_offline_llm, restore_llm  # noqa: E402
from schemas.request import ResearchRequest  # noqa: E402
from src.memory.manager import MemoryManager  # noqa: E402
from src.memory.policies import RetrievalPolicy, WritePolicy  # noqa: E402
from src.memory.schemas import (  # noqa: E402
    EpisodicMemory,
    MemoryKind,
    Provenance,
    SemanticMemory,
)
from src.memory.stores.sqlite_store import SqliteMemoryStore  # noqa: E402
from src.observability.trace_store import TraceStore  # noqa: E402
from src.runtime.budget import BudgetLimits, estimate_tokens  # noqa: E402
from src.runtime.runner import HarnessConfig, HarnessRunner  # noqa: E402
from src.tools.registry import build_default_registry  # noqa: E402

RESULTS_DIR = Path(__file__).resolve().parents[1] / "results"

ARMS = ("none", "recent_only", "vector_only", "full")

#: Subjects used for the session sequences. Real A-share names so entity
#: validation behaves as it does in production.
SUBJECTS = ["贵州茅台", "宁德时代", "比亚迪", "隆基绿能", "中芯国际", "长江电力",
            "恒瑞医药", "科大讯飞"]

#: Sessions per subject. Session 1 is always cold.
SESSION_TEMPLATES = [
    "{subject}投资价值分析",
    "{subject}财务与估值分析",
    "{subject}投资风险分析",
    "{subject}行业地位分析",
]


def _arm_policies(arm: str) -> tuple[Optional[WritePolicy], Optional[RetrievalPolicy]]:
    if arm == "vector_only":
        # The naive design: no relevance floor, no usefulness feedback, similarity
        # is effectively the only signal.
        return WritePolicy(), RetrievalPolicy(
            weight_similarity=1.0, weight_recency=0.0, weight_importance=0.0,
            weight_usage=0.0, weight_confidence=0.0, min_score=0.0,
            max_items=6, drop_expired=False)
    if arm == "recent_only":
        return WritePolicy(), RetrievalPolicy(max_items=1, min_score=0.0)
    return None, None  # defaults


def plant_ground_truth(manager: MemoryManager, subject: str) -> dict[str, Any]:
    """Seed memories whose relevance is known by construction.

    - `relevant`: about this subject, and true.
    - `irrelevant`: about a different subject; retrieving it is a precision miss.
    - `stale`: expired and factually superseded; using it is a stale-memory error.
    - `contradicted`: an old low-confidence claim later corrected by a
      high-confidence one; the system must prefer the correction.
    """
    relevant, irrelevant, stale, contradicted = [], [], [], []

    record = SemanticMemory(
        namespace=manager.namespace, subject=subject, key=f"pref:{subject}:sections",
        content=f"研究{subject}时，用户要求优先展示现金流质量与股东结构两个章节。",
        provenance=Provenance.USER_STATED, importance=0.9)
    manager.write(record)
    relevant.append(record.ensure_id())

    record = EpisodicMemory(
        namespace=manager.namespace, subject=subject, key=f"query_strategy:{subject}",
        content=f"检索{subject}时，「{subject} 年报 营收 净利润」这一检索式产出过可用来源。",
        provenance=Provenance.VERIFIED_OUTCOME, importance=0.7,
        task_signature=f"research:{subject}", action_taken="web_search",
        outcome="usable sources", outcome_verified_by="grader")
    manager.write(record)
    relevant.append(record.ensure_id())

    other = "某无关行业" if subject != "某无关行业" else "另一无关行业"
    record = SemanticMemory(
        namespace=manager.namespace, subject=other, key=f"pref:{other}:sections",
        content=f"研究{other}时，用户只关心政策变化，不需要财务细节。",
        provenance=Provenance.RUN_OBSERVATION, importance=0.4)
    manager.write(record)
    irrelevant.append(record.ensure_id())

    # Stale: already expired at write time, and factually superseded.
    record = SemanticMemory(
        namespace=manager.namespace, subject=subject, key=f"stale:{subject}:revenue",
        content=f"{subject}最近一期营业收入为 100.0 亿元（旧口径，已过期）。",
        provenance=Provenance.RUN_OBSERVATION, importance=0.5,
        expires_at=(datetime.now(timezone.utc) - timedelta(days=1)).isoformat(timespec="seconds"))
    manager.write(record)
    stale.append(record.ensure_id())

    # Contradiction pair on one key: weak old claim, then a user correction.
    weak = SemanticMemory(
        namespace=manager.namespace, subject=subject, key=f"symbol:{subject}",
        content=f"{subject}的证券代码可能是 000000。",
        provenance=Provenance.MODEL_INFERENCE, importance=0.5, confidence=0.3)
    manager.write(weak)
    strong = SemanticMemory(
        namespace=manager.namespace, subject=subject, key=f"symbol:{subject}",
        content=f"{subject}的证券代码经用户确认为 600519。",
        provenance=Provenance.USER_STATED, importance=0.9)
    result = manager.write(strong)
    contradicted.append({"weak": weak.ensure_id(), "strong": strong.ensure_id(),
                         "write_action": result.action})

    return {"relevant": relevant, "irrelevant": irrelevant, "stale": stale,
            "contradicted": contradicted}


def run_arm(arm: str, *, subjects: list[str], sessions: int, base_dir: Path) -> dict[str, Any]:
    """Run every subject's session sequence under one memory arm."""
    write_policy, retrieval_policy = _arm_policies(arm)
    db_path = base_dir / f"memory_{arm}.db"
    # An A/B rerun must start from the same empty state. Reusing a previous
    # SQLite file silently turns planted memories into reinforced duplicates.
    db_path.unlink(missing_ok=True)
    store = SqliteMemoryStore(db_path)
    rows: list[dict[str, Any]] = []
    truth_by_subject: dict[str, dict[str, Any]] = {}

    # Cross-namespace leakage probe: another tenant's memory that must never
    # surface for our namespace.
    other_manager = MemoryManager(namespace="tenant_other", store=store, use_embeddings=False)
    other_manager.write(SemanticMemory(
        namespace="tenant_other", subject="贵州茅台", key="secret:other",
        content="另一租户的私有备注：贵州茅台目标价为 9999 元。",
        provenance=Provenance.USER_STATED, importance=0.9))

    leakage_hits = 0
    stale_uses = 0
    contradiction_correct = 0
    contradiction_total = 0

    for subject in subjects:
        namespace = f"tenant_{subject}"
        manager = MemoryManager(namespace=namespace, store=store, enabled=(arm != "none"),
                                write_policy=write_policy, retrieval_policy=retrieval_policy,
                                use_embeddings=arm in ("vector_only", "full"))
        if arm != "none":
            truth_by_subject[subject] = plant_ground_truth(manager, subject)

        for session_index in range(1, sessions + 1):
            template = SESSION_TEMPLATES[(session_index - 1) % len(SESSION_TEMPLATES)]
            topic = template.format(subject=subject)

            injected = manager.retrieve_for_run(query=topic, subject=subject) if arm != "none" else []
            injected_ids = {i["memory_id"] for i in injected}
            memory_tokens = sum(estimate_tokens(i["content"]) for i in injected)

            truth = truth_by_subject.get(subject, {})
            if truth:
                relevant_set = set(truth["relevant"])
                irrelevant_set = set(truth["irrelevant"])
                stale_set = set(truth["stale"])
                hit = len(injected_ids & relevant_set)
                precision = hit / len(injected_ids) if injected_ids else None
                recall = hit / len(relevant_set) if relevant_set else None
                false_positives = len(injected_ids & irrelevant_set)
                stale_used = len(injected_ids & stale_set)
                stale_uses += stale_used

                contradiction_total += 1
                contents = " ".join(i["content"] for i in injected)
                if "600519" in contents or "000000" not in contents:
                    contradiction_correct += 1
            else:
                precision = recall = None
                false_positives = stale_used = 0

            leaked = [i for i in injected if "另一租户" in i["content"]]
            leakage_hits += len(leaked)

            outcome = _run_session(subject, topic, base_dir / arm, injected)
            if arm != "none":
                manager.learn_from_run(
                    run_id=outcome["run_id"], subject=subject,
                    events=outcome["events"], succeeded=outcome["succeeded"])

            rows.append({
                "arm": arm, "subject": subject, "session": session_index, "topic": topic,
                "succeeded": outcome["succeeded"], "status": outcome["status"],
                "duration_s": outcome["duration_s"], "num_sources": outcome["num_sources"],
                "injected": len(injected), "memory_tokens": memory_tokens,
                "memory_precision": precision, "memory_recall": recall,
                "false_positive_memories": false_positives,
                "stale_memory_used": stale_used, "leaked_memories": len(leaked),
                "similarity_mode": manager.retriever.last_similarity_mode,
            })

    successes = [r for r in rows if r["succeeded"]]
    with_precision = [r["memory_precision"] for r in rows if r["memory_precision"] is not None]
    with_recall = [r["memory_recall"] for r in rows if r["memory_recall"] is not None]
    injected_rows = [r for r in rows if r["injected"] > 0]
    useful_rows = [r for r in injected_rows if r["succeeded"]]

    def _avg(values: list[float]) -> Optional[float]:
        return round(sum(values) / len(values), 4) if values else None

    return {
        "arm": arm,
        "sessions_run": len(rows),
        "task_success_rate": round(len(successes) / len(rows), 4) if rows else 0.0,
        "memory_precision": _avg(with_precision),
        "memory_recall": _avg(with_recall),
        "memory_usefulness": (round(len(useful_rows) / len(injected_rows), 4)
                              if injected_rows else None),
        "stale_memory_error_rate": round(stale_uses / len(rows), 4) if rows else 0.0,
        "contradiction_resolution_accuracy": (round(contradiction_correct / contradiction_total, 4)
                                              if contradiction_total else None),
        "cross_user_leakage_rate": round(leakage_hits / len(rows), 4) if rows else 0.0,
        "avg_memory_tokens": _avg([float(r["memory_tokens"]) for r in rows]) or 0.0,
        "avg_duration_s": _avg([r["duration_s"] for r in rows]) or 0.0,
        "avg_injected": _avg([float(r["injected"]) for r in rows]) or 0.0,
        "rows": rows,
    }


def _run_session(subject: str, topic: str, base_dir: Path,
                 injected: list[dict[str, Any]]) -> dict[str, Any]:
    """One offline pipeline run. Injected memories are prepended to the request
    requirements, which is where they would influence a real run."""
    library = build_library("default", subject)
    registry = install_fixture_tools(build_default_registry(), library)
    config = HarnessConfig(base_dir=base_dir, arm="memory_ab",
                           budget=BudgetLimits(max_duration_s=300))
    runner = HarnessRunner(config, registry=registry)
    previous_llm = install_offline_llm(topic)

    requirements = ["财务分析", "风险提示"]
    requirements += [f"记忆提示：{i['content'][:60]}" for i in injected[:3]]

    started = time.perf_counter()
    try:
        result = runner.run(ResearchRequest(
            topic=topic, report_type="company_research", requirements=requirements,
            output_format="markdown", max_sources=4))
    except Exception as exc:  # noqa: BLE001
        return {"run_id": "", "succeeded": False, "status": f"error: {exc}",
                "duration_s": round(time.perf_counter() - started, 3),
                "num_sources": 0, "events": []}
    finally:
        restore_llm(previous_llm)

    events = TraceStore.read(Path(result["harness_trace_path"]))
    return {
        "run_id": result["harness_run_id"],
        "succeeded": result["harness_status"] in ("succeeded", "degraded"),
        "status": result["harness_status"],
        "duration_s": round(time.perf_counter() - started, 3),
        "num_sources": int(result.get("num_sources", 0) or 0),
        "events": events,
    }


def render_markdown(results: dict[str, dict[str, Any]]) -> str:
    metadata = results.get("_metadata", {})
    lines = ["# 记忆系统 A/B 评测", "",
             f"- 生成时间：{datetime.now(timezone.utc).isoformat(timespec='seconds')}",
             "- 运行模式：离线 fixture 回放（确定性）",
             f"- 记忆语义相似度：{'本地 embedding' if metadata.get('embedding_available') else '关键词降级'}；"
             f"模型预热 {metadata.get('embedding_prewarm_s_excluded_from_latency', 0)} 秒，不计入各 arm 时延",
             "- 为隔离变量，来源排序的 semantic ranking 在本实验中关闭。",
             "- Precision/Recall 针对**人工植入的、相关性已知的记忆**计算；"
             "这是构造出来的 ground truth，不是自然流量下的测量结果。", "",
             "## 各策略对比", "",
             "| 策略 | 下游成功率 | Memory Precision | Memory Recall | Memory Usefulness | "
             "过期记忆误用率 | 冲突解决准确率 | 跨用户泄漏率 | 平均注入条数 | 平均记忆Token | 平均时延(s) |",
             "|---|---|---|---|---|---|---|---|---|---|---|"]
    for arm in ARMS:
        row = results.get(arm)
        if not row:
            continue
        def fmt(value: Any) -> str:
            return "n/a" if value is None else (f"{value:.4f}".rstrip("0").rstrip(".")
                                               if isinstance(value, float) else str(value))
        lines.append(
            f"| {arm} | {fmt(row['task_success_rate'])} | {fmt(row['memory_precision'])} | "
            f"{fmt(row['memory_recall'])} | {fmt(row['memory_usefulness'])} | "
            f"{fmt(row['stale_memory_error_rate'])} | {fmt(row['contradiction_resolution_accuracy'])} | "
            f"{fmt(row['cross_user_leakage_rate'])} | {fmt(row['avg_injected'])} | "
            f"{fmt(row['avg_memory_tokens'])} | {fmt(row['avg_duration_s'])} |")

    baseline = results.get("none")
    full = results.get("full")
    lines += ["", "## 结论", ""]
    if baseline and full:
        delta = round(full["task_success_rate"] - baseline["task_success_rate"], 4)
        token_cost = round(full["avg_memory_tokens"], 1)
        if delta > 0:
            verdict = (f"完整记忆策略把下游任务成功率从 {baseline['task_success_rate']} 提升到 "
                       f"{full['task_success_rate']}（Δ={delta:+}），代价是每次运行平均多注入 "
                       f"{token_cost} 个 token。")
        elif delta == 0:
            verdict = (f"完整记忆策略**没有改变**下游任务成功率（两侧均为 "
                       f"{baseline['task_success_rate']}），但每次运行多花了 {token_cost} 个 token。"
                       "按项目既定标准，这种情况下**不得**在 README 中宣传“记忆增强”，"
                       "记忆的价值只能声称为可解释的偏好/别名复用，不能声称为效果提升。")
        else:
            verdict = (f"完整记忆策略使下游成功率**下降** {abs(delta)}，"
                       "应当默认关闭记忆注入并先修复检索策略。")
        lines.append(verdict)
    lines += ["", "## 限制", "",
              "- 离线 fixture 世界里每个 session 的可用来源相同，因此“记忆帮助找到更好来源”"
              "这一条主要收益路径在本实验中**无法体现**；结论只覆盖记忆对流程稳定性与"
              "偏好复用的影响。",
              "- Precision/Recall 依赖植入的 ground truth，属于构造实验。",
              "- 未做真实多用户、长时间跨度的线上实验。"]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Multi-session memory A/B evaluation")
    parser.add_argument("--sessions", type=int, default=4)
    parser.add_argument("--subjects", type=int, default=6)
    parser.add_argument("--out-dir", type=Path, default=RESULTS_DIR / "memory_ab")
    args = parser.parse_args()

    subjects = SUBJECTS[:max(1, args.subjects)]
    args.out_dir.mkdir(parents=True, exist_ok=True)

    # Source-ranking embeddings are unrelated to the memory experiment and
    # would make whichever arm runs first pay a large cold-start penalty.
    # Preload the memory embedder once, outside all measured sessions, then
    # disable source semantic ranking for the duration of this offline A/B.
    from config import config
    from tools.semantic_scorer import _load_model

    preload_started = time.perf_counter()
    embedding_available = _load_model() is not None
    preload_duration = round(time.perf_counter() - preload_started, 4)
    previous_semantic_ranking = config.ENABLE_SEMANTIC_RANKING
    config.ENABLE_SEMANTIC_RANKING = False

    results: dict[str, dict[str, Any]] = {}
    try:
        for arm in ARMS:
            print(f"=== arm: {arm} ===", flush=True)
            results[arm] = run_arm(arm, subjects=subjects, sessions=args.sessions,
                                   base_dir=args.out_dir)
            print(json.dumps({k: v for k, v in results[arm].items() if k != "rows"},
                             ensure_ascii=False, indent=2), flush=True)
    finally:
        config.ENABLE_SEMANTIC_RANKING = previous_semantic_ranking

    results["_metadata"] = {
        "embedding_available": embedding_available,
        "embedding_prewarm_s_excluded_from_latency": preload_duration,
        "source_semantic_ranking_disabled": True,
    }

    json_path = args.out_dir / "memory_ab_results.json"
    json_path.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    md_path = Path(__file__).resolve().parents[1] / "reports" / "memory_ab_report.md"
    md_path.parent.mkdir(parents=True, exist_ok=True)
    md_path.write_text(render_markdown(results), encoding="utf-8")
    print(f"wrote {json_path}\nwrote {md_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
