"""Domain-level allow/deny rules applied to candidate source URLs.

Whitelist domains get an authority boost in quality_scorer; blacklist domains
are dropped by browser_agent before any fetch happens (cheap pre-filter -
no point spending a network round-trip + parse on a site we'd never cite).

Whitelist is split into two authority tiers so downstream rankers/evaluators
(tools/source_filter.py, ReportAgent's evaluator) can weight official filings
above general financial media instead of treating "whitelisted" as binary.
"""
from urllib.parse import urlparse

# Tier 1: official filings/exchanges/regulators - the highest-trust sources.
TIER1_OFFICIAL_DOMAINS = {
    "cninfo.com.cn", "sse.com.cn", "szse.cn", "hkexnews.hk", "csrc.gov.cn",
    "gov.cn", "stats.gov.cn", "miit.gov.cn",
}

# Tier 2: mainstream financial data providers/media - reliable but secondary.
TIER2_FINANCIAL_MEDIA_DOMAINS = {
    "eastmoney.com", "10jqka.com.cn", "cls.cn", "stcn.com", "cs.com.cn",
    "cnstock.com", "jrj.com.cn", "hexun.com", "sina.com.cn", "p5w.net",
    "chinaipo.com", "cfi.net.cn", "xueqiu.com", "yicai.com",
}

DOMAIN_WHITELIST = TIER1_OFFICIAL_DOMAINS | TIER2_FINANCIAL_MEDIA_DOMAINS

# A handful of well-known company IR/official domains, used to give a subject's
# own official site top priority when ranking search results. Extend as needed
# - this is intentionally a small seed list, not an exhaustive registry.
COMPANY_OFFICIAL_DOMAINS: dict[str, set[str]] = {
    "比亚迪": {"byd.com"},
    "宁德时代": {"catl.com"},
}

DOMAIN_BLACKLIST = {
    "douyin.com", "kuaishou.com", "bilibili.com", "xiaohongshu.com",
    "tiktok.com", "facebook.com", "twitter.com", "x.com", "pinterest.com",
    "instagram.com", "toutiao.com", "sogou.com", "baike.baidu.com",
    "wenku.baidu.com", "zhidao.baidu.com", "jingyan.baidu.com", "zhuanlan.zhihu.com",
    # Added after observing repeated 401/403/empty-content fetch failures in
    # eval/bad_cases.md runs - these sites either block scraping outright or
    # reliably return unusable content, so skip the network round-trip entirely.
    "investing.com", "bdstatic.com", "wikipedia.org", "wallstreetcn.com",
    "gurufocus.cn", "lanjinger.com", "zhihu.com",
}


def get_domain(url: str) -> str:
    """Extract the bare registrable-ish domain (host without a leading www.)."""
    try:
        netloc = urlparse(url).netloc.lower()
    except ValueError:
        return ""
    netloc = netloc.split("@")[-1].split(":")[0]
    return netloc[4:] if netloc.startswith("www.") else netloc


def _matches(domain: str, rule_set: set[str]) -> bool:
    return bool(domain) and any(domain == d or domain.endswith("." + d) for d in rule_set)


def is_whitelisted(url: str) -> bool:
    return _matches(get_domain(url), DOMAIN_WHITELIST)


def is_blacklisted(url: str) -> bool:
    return _matches(get_domain(url), DOMAIN_BLACKLIST)


def authority_tier(url: str, subject: str = "") -> int:
    """Return 1 (official/exchange), 2 (financial media), or 0 (neither).

    `subject` optionally checks the small COMPANY_OFFICIAL_DOMAINS seed list
    so a company's own official site ranks as tier 1 for its own reports.
    """
    domain = get_domain(url)
    if not domain:
        return 0
    if subject and _matches(domain, COMPANY_OFFICIAL_DOMAINS.get(subject, set())):
        return 1
    if _matches(domain, TIER1_OFFICIAL_DOMAINS):
        return 1
    if _matches(domain, TIER2_FINANCIAL_MEDIA_DOMAINS):
        return 2
    return 0
