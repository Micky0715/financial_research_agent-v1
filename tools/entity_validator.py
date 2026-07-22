"""Entity / topic credibility validation (v3 stage H, extended v4 stage H).

解决 bad_cases 16：虚构公司主题靠泛金融内容凑出 5 个"相关"来源、生成看似正常
的研报。在 browse 之后、analyze 之前做实体真实性校验。

company_research 的四态判定（v4 新增上市公司硬校验，见
tools/listed_company_registry.py）：
1. **verified**（上市）：A股代码表命中，或项目内置 COMPANY_ALIASES 命中
   （别名表条目本身就是真实证券代码）——最强证据，可生成完整公司研报；
2. **unsupported_unlisted_company**（真实但未上市）：代码表未命中，但主体名/
   别名在 >=3 个已抓取来源正文中真实出现——判断为真实存在的实体，但不在
   免费公开 A 股注册表覆盖范围内（港股/美股/新三板/未上市/私营企业），
   不生成正式上市公司研报，只能生成"公开资料摘要"；
3. **weak**：来源证据 1-2 个，证据不足以在"未上市真实企业"和"信息稀疏"
   之间下结论；
4. **failed**（insufficient_entity_evidence）：代码表未命中且没有任何来源
   正文真实提到该主体——大概率是虚构主体，终止生成。

industry_research 的验证：行业词/同义词在多个来源正文中一致出现；未命中内置
INDUSTRY_SYNONYMS 时，从 subject 里"行业/产业"前的核心词作为补充候选词
（如"创新药行业投资机会" -> 补充候选"创新药"），避免只用整段描述性短语做
逐字匹配——真实文章几乎不会逐字出现"创新药行业投资机会"这种完整短语，
但会大量出现"创新药"这个核心词（bad_cases 31 修复）。

macro_research / risk_research / valuation_research：这三类报告没有"是否
虚构的可验证实体"这个概念（GDP、CPI、利率本身不存在"是否真实存在"的问题），
套用行业式的"来源正文逐字匹配"校验只会误伤真实合法的宏观/风险/估值主题
（bad_cases 31：9/10 宏观 case 被误判 insufficient_entity_evidence 清空来源）。
这三类直接返回 `validation_status="not_applicable"`，不参与去留判定。

验证是**启发式**的，不保证 100%：未上市公司/新实体可能被误判 weak，
名称撞车可能被误判 verified。输出永远带 reason，failed/unsupported_unlisted_company
都不走完整公司研报生成路径。
"""
from typing import Any

from schemas.source import Source
from tools.listed_company_registry import check_listed_status
from utils.logger import logger
from utils.text_utils import COMPANY_ALIASES, INDUSTRY_SYNONYMS, build_relevance_profile


def _stock_list_hit(subject: str) -> tuple[bool, str]:
    """A股代码表命中检查（向后兼容包装，供既有测试 monkeypatch）。

    AkShare 不可用时返回 (False, 原因) 并降级到其他证据。"""
    try:
        status = check_listed_status(subject)
        if status["listed"]:
            return True, status["reason"]
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


_NO_ENTITY_CONCEPT_TYPES = {"macro_research", "risk_research", "valuation_research"}


def _core_industry_terms(subject: str) -> list[str]:
    """从 subject 里"行业/产业"前的部分提取核心行业词，作为逐字匹配的补充
    候选——比整段描述性短语（如"创新药行业投资机会"）更可能真实出现在来源正文里。"""
    terms = []
    for marker in ("行业", "产业"):
        idx = subject.find(marker)
        if idx >= 2:  # 至少留 2 个字符的核心词
            terms.append(subject[:idx])
    return [t for t in terms if len(t) >= 2]


def validate_entity(topic: str, report_type: str, sources: list[Source]) -> dict[str, Any]:
    """主入口。Returns:
    {entity_name, entity_type, validation_status: verified|weak|failed|not_applicable,
     evidence_sources, matched_aliases, reason}
    """
    profile = build_relevance_profile(topic, report_type)
    subject = profile["subject"]

    if report_type in _NO_ENTITY_CONCEPT_TYPES:
        # 宏观/风险/估值研究没有"虚构实体"这个概念，套用行业式逐字匹配只会
        # 误伤真实合法主题（bad_cases 31）——直接放行，不参与去留判定。
        return {
            "entity_name": subject, "entity_type": report_type, "validation_status": "not_applicable",
            "evidence_sources": [], "matched_aliases": [],
            "reason": f"{report_type} 不涉及可验证的具体实体，实体校验不适用，直接放行",
        }

    if report_type != "company_research":
        # 行业主题：行业词在多个来源正文一致出现即可；未命中内置同义词表时，
        # 用 [核心行业词, 完整subject] 做候选，核心词优先（见 _core_industry_terms）
        terms = profile["industry_terms"] or (_core_industry_terms(subject) + [subject])
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

    # 公司主题：v4 阶段H 上市公司硬校验（四态：verified / unsupported_unlisted_company
    # / weak / failed）。_stock_list_hit 保留（既有测试 monkeypatch 依赖其签名）；
    # check_listed_status 只用于补充 symbol/exchange 展示字段，失败不影响判定。
    in_alias_map = any(
        subject in c or c in subject or any(a and a in subject for a in al)
        for c, al in COMPANY_ALIASES.items()
    )
    stock_hit, stock_reason = _stock_list_hit(subject)
    try:
        registry = check_listed_status(subject)
    except Exception:  # noqa: BLE001 - registry metadata is supplementary, never blocking
        registry = {}
    symbol, exchange = registry.get("symbol"), registry.get("exchange")

    terms = [subject] + profile["aliases"]
    evidence = _source_mentions(sources, terms)
    matched = sorted({t for t in terms if t and any(t in f"{s.title}{s.content[:3000]}" for s in sources)})

    if stock_hit or in_alias_map:
        status = "verified"
        listed_status = "listed"
        reason = stock_reason if stock_hit else "项目内置公司别名表命中"
    elif len(evidence) >= 3:
        status = "unsupported_unlisted_company"
        listed_status = "unlisted"
        reason = (f"{stock_reason}；但主体名/别名在 {len(evidence)} 个来源正文中真实出现，"
                  "判断为真实存在但不在免费公开 A 股注册表覆盖范围内的实体"
                  "（可能是港股/美股/新三板/未上市或私营企业）——不生成正式上市公司研报，"
                  "仅可生成公开资料摘要")
    elif len(evidence) >= 1:
        status = "weak"
        listed_status = "unknown"
        reason = f"主体名仅在 {len(evidence)} 个来源正文中出现，且 {stock_reason}"
    else:
        status = "failed"
        listed_status = "unknown"
        reason = f"{stock_reason}；且没有任何已抓取来源的正文真实提到该主体——来源可能只是泛金融内容"

    result = {
        "entity_name": subject, "entity_type": "company", "validation_status": status,
        "evidence_sources": evidence, "matched_aliases": matched[:6], "reason": reason,
        "symbol": symbol, "exchange": exchange, "listed_status": listed_status,
        "registry_coverage_note": registry.get("coverage_note", ""),
    }
    logger.info(f"entity validation: {subject} -> {status} ({reason[:80]})")
    return result
