"""Industry concentration (CR3/CR5/CR10/HHI) on real sina sector members (v4 stage C).

口径诚实声明：免费公开渠道没有全行业营收市占率数据，本模块用"新浪行业/概念
板块内 A 股上市公司总市值份额"计算集中度，样本口径写进 sample_note——这是
上市公司市值口径的集中度，不是全行业营收口径。来源正文中出现的 CR/市占率
表述会作为补充证据摘出（带 source_id）。无法匹配板块 -> degraded，不编造。
"""
import re
from datetime import datetime

from schemas.industry_research import ConcentrationResult, EvidenceItem
from schemas.source import Source
from tools.akshare_tool import _akshare, _safe
from utils.logger import logger

# 行业主题 -> 候选板块（按顺序尝试）。(kind, 板块名)
_TOPIC_SECTOR_CANDIDATES: list[tuple[list[str], list[tuple[str, str]]]] = [
    (["白酒"], [("概念", "白酒概念"), ("行业", "酿酒行业")]),
    (["新能源汽车", "新能源车", "电动车"], [("行业", "汽车制造")]),
    (["光伏"], [("概念", "光伏"), ("概念", "光伏概念")]),
    (["锂电", "储能", "动力电池"], [("概念", "锂电池")]),
    (["半导体", "芯片", "集成电路"], [("行业", "电子器件")]),
    (["消费电子"], [("行业", "电子信息")]),
    (["创新药", "生物医药"], [("概念", "创新药"), ("行业", "生物制药")]),
    (["医药"], [("行业", "生物制药")]),
    (["机器人", "人工智能"], [("概念", "机器人概念")]),
    (["军工"], [("概念", "国防军工")]),
    (["银行", "保险", "证券", "金融"], [("行业", "金融行业")]),
    (["家电"], [("行业", "家电行业")]),
    (["房地产", "地产"], [("行业", "房地产")]),
    (["煤炭"], [("行业", "煤炭行业")]),
    (["钢铁"], [("行业", "钢铁行业")]),
    (["有色"], [("行业", "有色金属")]),
    (["化工"], [("行业", "化工行业")]),
    (["食品"], [("行业", "食品行业")]),
]

_CR_MENTION_RE = re.compile(r"(CR\d|市占率|市场份额|集中度)")
_SENT_RE = re.compile(r"[。！？；\n]")


def match_sector(topic: str) -> list[tuple[str, str]]:
    for keywords, candidates in _TOPIC_SECTOR_CANDIDATES:
        if any(k in topic for k in keywords):
            return candidates
    return []


def _fetch_members(kind: str, sector_name: str) -> list[dict]:
    ak = _akshare()
    if ak is None:
        return []
    errors: list[dict] = []
    indicator = "新浪行业" if kind == "行业" else "概念"
    spot = _safe("stock_sector_spot", lambda: ak.stock_sector_spot(indicator=indicator), errors)
    if spot is None:
        return []
    try:
        row = spot[spot["板块"] == sector_name]
        if row.empty:
            return []
        label = str(row["label"].iloc[0])
        detail = _safe("stock_sector_detail", lambda: ak.stock_sector_detail(sector=label), errors)
        if detail is None:
            return []
        members = []
        for _, s in detail.iterrows():
            cap = float(s.get("mktcap") or 0) / 1e4  # 万元 -> 亿元
            name = str(s.get("name", ""))
            if cap > 0 and not any(t in name for t in ("ST", "退市", "退")):
                members.append({"code": str(s.get("code", "")), "name": name,
                                "mktcap_yi": round(cap, 2)})
        return members
    except Exception:  # noqa: BLE001
        return []


def compute_concentration(topic: str, sources: list[Source]) -> ConcentrationResult:
    result = ConcentrationResult(industry=topic,
                                 fetched_at=datetime.now().isoformat(timespec="seconds"))

    # 来源正文中的集中度表述（证据补充，独立于结构化路径）
    for src in sources:
        for sent in _SENT_RE.split(src.content or ""):
            sent = sent.strip()
            if 15 <= len(sent) <= 200 and _CR_MENTION_RE.search(sent) and re.search(r"\d", sent):
                result.source_mentions.append(EvidenceItem(
                    text=sent[:150], source_id=src.source_id, kind="structure"))
                if len(result.source_mentions) >= 5:
                    break
        if len(result.source_mentions) >= 5:
            break

    candidates = match_sector(topic)
    members: list[dict] = []
    for kind, name in candidates:
        members = _fetch_members(kind, name)
        if len(members) >= 5:
            result.sector_kind, result.sector_name = kind, name
            break
    if len(members) < 5:
        result.degraded = True
        result.limitation = (
            "无匹配的新浪行业/概念板块或有效成分不足 5 家——免费渠道无该行业市占率结构化数据，"
            "CR/HHI 不计算（不编造）；仅保留来源正文中的集中度表述作为线索")
        return result

    members.sort(key=lambda m: -m["mktcap_yi"])
    total = sum(m["mktcap_yi"] for m in members)
    shares = [m["mktcap_yi"] / total * 100 for m in members]
    result.member_count = len(members)
    result.cr3 = round(sum(shares[:3]), 2)
    result.cr5 = round(sum(shares[:5]), 2)
    result.cr10 = round(sum(shares[:10]), 2)
    result.hhi = round(sum(s * s for s in shares), 1)
    result.top_members = [{"name": m["name"], "mktcap_yi": m["mktcap_yi"],
                           "share_pct": round(s, 2)} for m, s in zip(members[:10], shares[:10])]
    result.sample_note = (
        f"样本={result.sector_kind}板块「{result.sector_name}」{result.member_count} 家 A 股上市公司；"
        "份额按总市值计算（上市公司市值口径，非全行业营收口径），未含未上市企业与海外企业")
    result.limitation = "市值集中度与营收集中度可能显著不同；板块归类为新浪口径"
    logger.info(f"concentration {topic}: {result.sector_name} CR5={result.cr5} HHI={result.hhi} n={result.member_count}")
    return result
