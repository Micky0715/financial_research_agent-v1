"""Source authority tier classification (v3 stage C).

启发式分级，**不等于来源绝对可靠**：tier1 也可能有错漏，tier3 也可能高质量。
分级只回答"这是什么性质的来源"，供排序加权、报告标注和评测统计使用。

- tier1: 官方/交易所/监管/公告/年报（cninfo、上交所、深交所、gov.cn、公告 PDF）
- tier2: 主流财经媒体与数据平台（东方财富、证券时报、财联社、新浪财经、同花顺…）
- tier3: 普通来源/UGC（雪球、博客、自媒体、一般财经网页）
- unknown: 域名无法解析或无法归类
- local_user_file: 用户本地上传文件（阶段G）
"""
from typing import Any

from tools.domain_rules import (
    TIER1_OFFICIAL_DOMAINS,
    TIER2_FINANCIAL_MEDIA_DOMAINS,
    get_domain,
)

# 明确的 UGC/社区平台（内容质量方差大，单独标记）
_UGC_DOMAINS = {"xueqiu.com", "guba.eastmoney.com", "tieba.baidu.com", "36kr.com"}
# 官方公告的标题信号（即使域名不在 tier1 表里，公告/年报原文也按官方内容处理）
_OFFICIAL_TITLE_HINTS = ["年度报告", "招股说明书", "年报全文", "公告全文"]


def _matches(domain: str, rule_set: set[str]) -> bool:
    return bool(domain) and any(domain == d or domain.endswith("." + d) for d in rule_set)


def classify_source(url: str, title: str = "", source_type: str = "web") -> dict[str, Any]:
    """URL（+标题/类型）-> tier 分类结果。

    Returns {authority_tier, authority_reason, domain, is_official_source,
             is_financial_media, is_user_generated_content}.
    authority_tier 取值: "tier1" | "tier2" | "tier3" | "unknown" | "local_user_file"
    """
    if source_type == "local_file":
        return {
            "authority_tier": "local_user_file",
            "authority_reason": "用户本地上传文件，可信度由使用者自行判断",
            "domain": "",
            "is_official_source": False,
            "is_financial_media": False,
            "is_user_generated_content": False,
        }

    domain = get_domain(url)
    if not domain:
        return {
            "authority_tier": "unknown",
            "authority_reason": "域名无法解析",
            "domain": "",
            "is_official_source": False,
            "is_financial_media": False,
            "is_user_generated_content": False,
        }

    is_ugc = _matches(domain, _UGC_DOMAINS)

    if _matches(domain, TIER1_OFFICIAL_DOMAINS):
        return {
            "authority_tier": "tier1",
            "authority_reason": f"官方/交易所/监管域名: {domain}",
            "domain": domain,
            "is_official_source": True,
            "is_financial_media": False,
            "is_user_generated_content": False,
        }

    if any(h in (title or "") for h in _OFFICIAL_TITLE_HINTS) and source_type == "pdf":
        return {
            "authority_tier": "tier1",
            "authority_reason": f"公告/年报原文 PDF（标题信号），域名: {domain}",
            "domain": domain,
            "is_official_source": True,
            "is_financial_media": False,
            "is_user_generated_content": False,
        }

    if _matches(domain, TIER2_FINANCIAL_MEDIA_DOMAINS) and not is_ugc:
        return {
            "authority_tier": "tier2",
            "authority_reason": f"主流财经媒体/数据平台: {domain}",
            "domain": domain,
            "is_official_source": False,
            "is_financial_media": True,
            "is_user_generated_content": False,
        }

    return {
        "authority_tier": "tier3",
        "authority_reason": ("UGC/社区平台" if is_ugc else "普通财经网页/未收录域名") + f": {domain}",
        "domain": domain,
        "is_official_source": False,
        "is_financial_media": False,
        "is_user_generated_content": is_ugc,
    }


def tier_counts(classified: list[dict[str, Any]]) -> dict[str, Any]:
    """一组分类结果 -> 各 tier 计数 + tier1_or_tier2_ratio。"""
    counts = {"tier1": 0, "tier2": 0, "tier3": 0, "unknown": 0, "local_user_file": 0}
    for c in classified:
        counts[c.get("authority_tier", "unknown")] = counts.get(c.get("authority_tier", "unknown"), 0) + 1
    total = sum(counts.values())
    high = counts["tier1"] + counts["tier2"]
    return {**counts, "total": total, "tier1_or_tier2_ratio": round(high / total, 3) if total else 0.0}
