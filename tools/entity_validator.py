"""Entity / topic credibility validation (v3 stage H).

解决 bad_cases 16：虚构公司主题靠泛金融内容凑出 5 个"相关"来源、生成看似正常
的研报。在 browse 之后、analyze 之前做实体真实性校验：

company_research 的验证证据（任一强证据 -> verified）：
1. A股代码表命中（AkShare 全市场 code-name 映射，本地缓存）——最强证据；
2. 项目内置 COMPANY_ALIASES 命中；
3. 来源正文证据：主体名/别名在多个已抓取来源的正文中真实出现
   （>=3 个来源 -> verified；1-2 个 -> weak）。

industry_research 的验证：行业词/同义词在多个来源正文中一致出现。

验证是**启发式**的，不保证 100%：未上市公司/新实体可能被误判 weak，
名称撞车可能被误判 verified。输出永远带 reason，失败时主流程走
insufficient_entity_evidence 终止路径而不是硬生成报告。
"""
from typing import Any

from schemas.source import Source
from utils.logger import logger
from utils.text_utils import COMPANY_ALIASES, INDUSTRY_SYNONYMS, build_relevance_profile


def _stock_list_hit(subject: str) -> tuple[bool, str]:
    """A股代码表命中检查。AkShare 不可用时返回 (False, 原因) 并降级到其他证据。"""
    try:
        from tools.akshare_tool import resolve_symbol

        resolved = resolve_symbol(subject)
        if resolved:
            return True, f"A股代码表命中: {resolved[1]}({resolved[0]})"
        return False, "A股代码表未命中"
    except Exception as exc:  # noqa: BLE001 - stock list unavailable -> degrade to source evidence
        return False, f"代码表不可用（降级为来源证据校验）: {str(exc)[:80]}"


def _source_mentions(sources: list[Source], terms: list[str]) -> list[str]:
    """正文中真实提到任一 term 的 source_id 列表。"""
    hits = []
    for s in sources:
        text = f"{s.title}\n{s.content[:5000]}"
        if any(t and len(t) >= 2 and t in text for t in terms):
            hits.append(s.source_id)
    return hits


def validate_entity(topic: str, report_type: str, sources: list[Source]) -> dict[str, Any]:
    """主入口。Returns:
    {entity_name, entity_type, validation_status: verified|weak|failed,
     evidence_sources, matched_aliases, reason}
    """
    profile = build_relevance_profile(topic, report_type)
    subject = profile["subject"]

    if report_type != "company_research":
        # 行业主题：行业词在多个来源正文一致出现即可
        terms = profile["industry_terms"] or [subject]
        evidence = _source_mentions(sources, terms)
        matched = [t for t in terms if any(t in f"{s.title}{s.content[:3000]}" for s in sources)]
        if len(evidence) >= 3:
            status, reason = "verified", f"行业词在 {len(evidence)} 个来源正文中一致出现"
        elif len(evidence) >= 1:
            status, reason = "weak", f"行业词仅在 {len(evidence)} 个来源中出现"
        else:
            status, reason = "failed", "没有任何来源正文包含该行业的核心词"
        return {
            "entity_name": subject, "entity_type": "industry", "validation_status": status,
            "evidence_sources": evidence, "matched_aliases": matched[:6], "reason": reason,
        }

    # 公司主题
    in_alias_map = any(
        subject in c or c in subject or any(a and a in subject for a in al)
        for c, al in COMPANY_ALIASES.items()
    )
    stock_hit, stock_reason = _stock_list_hit(subject)
    terms = [subject] + profile["aliases"]
    evidence = _source_mentions(sources, terms)
    matched = sorted({t for t in terms if t and any(t in f"{s.title}{s.content[:3000]}" for s in sources)})

    if stock_hit or in_alias_map:
        status = "verified"
        reason = stock_reason if stock_hit else "项目内置公司别名表命中"
    elif len(evidence) >= 3:
        status = "verified"
        reason = f"主体名/别名在 {len(evidence)} 个来源正文中真实出现（{stock_reason}）"
    elif len(evidence) >= 1:
        status = "weak"
        reason = f"主体名仅在 {len(evidence)} 个来源正文中出现，且 {stock_reason}"
    else:
        status = "failed"
        reason = f"{stock_reason}；且没有任何已抓取来源的正文真实提到该主体——来源可能只是泛金融内容"

    result = {
        "entity_name": subject, "entity_type": "company", "validation_status": status,
        "evidence_sources": evidence, "matched_aliases": matched[:6], "reason": reason,
    }
    logger.info(f"entity validation: {subject} -> {status} ({reason[:80]})")
    return result
