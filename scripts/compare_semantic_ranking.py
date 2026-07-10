"""Rules-only vs rules+semantic ranking comparison over the 10 eval topics.

Usage:
    python scripts/compare_semantic_ranking.py

Offline analysis - candidate pools come from the warm search cache
(outputs/cache/search_cache.json), no live searches or fetches. For each
topic, the same candidate pool is ranked twice (semantic layer off vs on)
and the top-K windows are compared on:

- relevance-term hit rate: fraction of top-K whose title+snippet mention the
  subject / a company alias / an industry synonym (cheap proxy for "will the
  post-fetch relevance scorer keep this candidate");
- authority-tier composition of top-K;
- rank overlap (how much the semantic layer actually changed the order).

Writes outputs/eval/semantic_ranking_compare.md. K = MAX_BROWSE_CANDIDATES
because that's the real read budget - what matters is which candidates make
it into the fetch window, not the ordering of candidates nobody fetches.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")

from agents.research_agent import ResearchAgent  # noqa: E402
from config import config  # noqa: E402
from tools.domain_rules import authority_tier  # noqa: E402
from tools.source_filter import rank_search_results  # noqa: E402
from utils.cache_utils import get_cached_search_result  # noqa: E402
from utils.file_utils import load_json  # noqa: E402
from utils.text_utils import build_relevance_profile  # noqa: E402


def candidate_pool_for_topic(topic: str, report_type: str) -> list[dict]:
    """Rebuild the deduped candidate pool from cached search results only."""
    agent = ResearchAgent()
    profile = build_relevance_profile(topic, report_type)
    queries = agent._build_core_queries(profile["subject"], report_type)  # noqa: SLF001 - offline analysis

    seen: set[str] = set()
    pool: list[dict] = []
    for query in queries:
        hits = get_cached_search_result(query) or []
        for hit in hits:
            url = hit.get("url", "")
            if url and url not in seen:
                seen.add(url)
                pool.append(hit)
    return pool


def relevance_hit_rate(candidates: list[dict], relevance_terms: list[str]) -> float:
    if not candidates:
        return 0.0
    hits = sum(
        1
        for c in candidates
        if any(t and t in f"{c.get('title', '')} {c.get('snippet', '')}" for t in relevance_terms)
    )
    return hits / len(candidates)


def tier_counts(candidates: list[dict], subject: str) -> dict[int, int]:
    counts = {1: 0, 2: 0, 0: 0}
    for c in candidates:
        counts[authority_tier(c.get("url", ""), subject)] += 1
    return counts


def main() -> None:
    topics = load_json(config.BASE_DIR / "eval" / "topics.json")
    k = config.MAX_BROWSE_CANDIDATES

    lines = [
        "# 语义排序召回质量对比（rules-only vs rules+semantic）",
        "",
        f"对同一候选池（来自搜索缓存，无实时请求）分别做纯规则排序和规则+语义融合排序，"
        f"比较进入抓取窗口（top-{k}，即 MAX_BROWSE_CANDIDATES）的候选质量。",
        f"融合权重 SEMANTIC_WEIGHT={config.SEMANTIC_WEIGHT}，模型 bge-small-zh-v1.5（本地 CPU）。",
        "",
        "| topic | 候选池 | 相关词命中率 rules | 相关词命中率 +semantic | tier1+2 rules | tier1+2 +semantic | top-K 重叠 |",
        "|---|---|---|---|---|---|---|",
    ]

    total_rule_hit, total_sem_hit, total_overlap, n_topics = 0.0, 0.0, 0.0, 0
    example_moves: list[str] = []

    for entry in topics:
        topic = entry["topic"]
        report_type = entry.get("report_type", "company_research")
        profile = build_relevance_profile(topic, report_type)
        pool = candidate_pool_for_topic(topic, report_type)
        if not pool:
            lines.append(f"| {topic} | 0（缓存未命中） | - | - | - | - | - |")
            continue

        original = config.ENABLE_SEMANTIC_RANKING
        try:
            config.ENABLE_SEMANTIC_RANKING = False
            rules_top = rank_search_results(pool, profile["subject"], profile["aliases"], topic)[:k]
            config.ENABLE_SEMANTIC_RANKING = True
            fused_top = rank_search_results(pool, profile["subject"], profile["aliases"], topic)[:k]
        finally:
            config.ENABLE_SEMANTIC_RANKING = original

        rule_hit = relevance_hit_rate(rules_top, profile["relevance_terms"])
        sem_hit = relevance_hit_rate(fused_top, profile["relevance_terms"])
        rule_tiers = tier_counts(rules_top, profile["subject"])
        sem_tiers = tier_counts(fused_top, profile["subject"])
        rules_urls = {c["url"] for c in rules_top}
        fused_urls = {c["url"] for c in fused_top}
        overlap = len(rules_urls & fused_urls) / max(len(rules_urls | fused_urls), 1)

        total_rule_hit += rule_hit
        total_sem_hit += sem_hit
        total_overlap += overlap
        n_topics += 1

        promoted = [c for c in fused_top if c["url"] not in rules_urls]
        if promoted and len(example_moves) < 5:
            c = promoted[0]
            example_moves.append(
                f"- **{topic}** 语义层拉入窗口: 《{c.get('title', '')[:50]}》"
                f"（rule={c.get('rule_score')}, semantic={c.get('semantic_score')}）"
            )

        lines.append(
            f"| {topic} | {len(pool)} | {rule_hit:.2f} | {sem_hit:.2f} "
            f"| {rule_tiers[1] + rule_tiers[2]}/{len(rules_top)} | {sem_tiers[1] + sem_tiers[2]}/{len(fused_top)} "
            f"| {overlap:.2f} |"
        )

    if n_topics:
        lines += [
            "",
            "## 汇总",
            "",
            f"- 平均相关词命中率：rules-only **{total_rule_hit / n_topics:.3f}** -> "
            f"rules+semantic **{total_sem_hit / n_topics:.3f}**",
            f"- 平均 top-{k} 窗口重叠度：{total_overlap / n_topics:.3f}"
            "（1.0 = 语义层没有改变进入抓取窗口的候选集合）",
            "",
            "## 语义层改变了什么（示例）",
            "",
            *(example_moves or ["-（本次对比中语义层未把新候选拉入任何 topic 的抓取窗口）"]),
            "",
            "## 解读注意",
            "",
            "- 相关词命中率是**代理指标**（标题/摘要是否提到主体词/别名/行业词），不是人工标注的",
            "  真实相关性；语义层的价值恰恰包括召回那些*不含*关键词但语义相关的候选，这部分",
            "  收益该指标体现不出来，需要看示例和最终 eval 的 source 质量。",
            "- 对比在同一候选池上进行，隔离的是**排序层**的差异；搜索召回本身（query 生成）两组相同。",
        ]

    out = config.EVAL_DIR / "semantic_ranking_compare.md"
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"Comparison saved to {out}")


if __name__ == "__main__":
    main()
