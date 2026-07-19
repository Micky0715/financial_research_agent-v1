"""Policy document parser -> structured PolicyImpactResult (v4 stage A).

主路径用 LLM 做结构化抽取（发布机构/日期/目标/工具/受益与受限行业/影响），
证据摘句必须来自原文；LLM 不可用时降级到规则抽取（正则机构/日期 + 句级
线索词归类）——不是关键词计数，而是抽取句子级证据。两条路径都在
parse_method 里如实标注。
"""
import json
import re
from typing import Optional

from schemas.macro_data import PolicyImpactResult
from utils.logger import logger

_ISSUER_RE = re.compile(
    r"(国务院|中国人民银行|财政部|国家发展和改革委员会|发改委|工业和信息化部|工信部|"
    r"国家金融监督管理总局|中国证监会|证监会|商务部|海关总署|国家统计局|国家能源局|"
    r"住房和城乡建设部|住建部|科技部|市场监管总局|[一-龥]{2,8}省人民政府)"
)
_DATE_RE = re.compile(r"(\d{4})年(\d{1,2})月(?:(\d{1,2})日)?")

_GOAL_CUES = ("目标", "旨在", "为了", "推动", "促进", "实现", "力争", "提升")
_TOOL_CUES = {
    "降准": "降低存款准备金率", "存款准备金": "降低存款准备金率",
    "降息": "下调政策利率", "再贷款": "结构性再贷款",
    "贴息": "财政贴息", "补贴": "财政补贴", "减税": "减税降费", "降费": "减税降费",
    "专项债": "地方政府专项债", "国债": "国债/特别国债", "准入": "市场准入调整",
    "牌照": "许可/牌照管理", "限购": "限购限售调整", "公积金": "住房公积金政策",
    "招标": "政府采购/招标", "试点": "试点示范", "标准": "标准与规范制定",
}
_SUPPORT_CUES = ("支持", "鼓励", "培育", "壮大", "加快发展", "重点发展", "扶持")
_CONSTRAIN_CUES = ("限制", "禁止", "淘汰", "压减", "整治", "规范", "严控", "遏制")
_INDUSTRY_RE = re.compile(
    r"(新能源汽车|新能源|光伏|风电|储能|半导体|集成电路|人工智能|机器人|生物医药|创新药|"
    r"低空经济|房地产|钢铁|煤炭|白酒|银行|保险|证券|消费电子|军工|化工|有色|建材|"
    r"高耗能|数字经济|平台经济|养老|医疗|教育|文旅|外贸|制造业|农业|基建|基础设施)"
)

_LLM_SYSTEM = "你是政策研究助理。只依据给定原文抽取信息，不得编造原文没有的内容。"
_LLM_PROMPT = """从下面的政策文本中抽取结构化信息，输出 JSON（无其他文字）：
{{"issuer": "发布机构", "publish_date": "YYYY-MM-DD或空", "policy_topic": "一句话主题",
"policy_goals": ["目标"], "policy_tools": ["政策工具"],
"supported_industries": ["受益行业"], "constrained_industries": ["受限行业"],
"potential_impacts": ["对经济/行业/资产的潜在影响，注明方向"],
"evidence_quotes": ["支撑上述判断的原文摘句，逐字引用，每句<=60字"]}}
要求：evidence_quotes 必须逐字来自原文；没有的信息用空列表/空串，不要猜。

政策文本：
{text}
"""


def _split_sentences(text: str) -> list[str]:
    return [s.strip() for s in re.split(r"[。；;！\n]", text) if len(s.strip()) >= 8]


def _split_clauses(text: str) -> list[str]:
    """子句级切分（含逗号）：支持/受限判断需要更细粒度，避免一句内互相抵消。"""
    return [s.strip() for s in re.split(r"[。；;！\n，,]", text) if len(s.strip()) >= 6]


def _industries_in(clause: str) -> list[str]:
    """匹配行业词，先剔除机构名，避免'中国人民银行'里的'银行'被当行业。"""
    return _INDUSTRY_RE.findall(_ISSUER_RE.sub("", clause))


def _rule_parse(text: str, title: str) -> PolicyImpactResult:
    sentences = _split_sentences(text)
    issuer_m = _ISSUER_RE.search(title + " " + text[:500])
    date_m = _DATE_RE.search(title + " " + text[:500])
    publish_date = ""
    if date_m:
        y, mth, d = date_m.group(1), int(date_m.group(2)), date_m.group(3)
        publish_date = f"{y}-{mth:02d}" + (f"-{int(d):02d}" if d else "")

    goals, supported, constrained, impacts, quotes = [], [], [], [], []
    tools: list[str] = []
    for sent in sentences:
        if any(c in sent for c in _GOAL_CUES) and len(goals) < 5:
            goals.append(sent[:60])
            quotes.append(sent[:60])
        for cue, tool in _TOOL_CUES.items():
            if cue in sent and tool not in tools:
                tools.append(tool)
                quotes.append(sent[:60])
    for clause in _split_clauses(text):
        inds = _industries_in(clause)
        if not inds:
            continue
        if any(c in clause for c in _SUPPORT_CUES):
            supported.extend(inds)
            quotes.append(clause[:60])
        if any(c in clause for c in _CONSTRAIN_CUES):
            constrained.extend(inds)
            quotes.append(clause[:60])
    supported = sorted(set(supported) - set(constrained))
    constrained = sorted(set(constrained))
    for ind in supported[:3]:
        impacts.append(f"{ind}行业获得政策支持，景气度可能改善（方向判断，非量化预测）")
    for ind in constrained[:3]:
        impacts.append(f"{ind}相关活动受到约束，供给或需求可能收缩（方向判断，非量化预测）")

    return PolicyImpactResult(
        title=title,
        issuer=issuer_m.group(1) if issuer_m else "",
        publish_date=publish_date,
        policy_topic=title or (sentences[0][:40] if sentences else ""),
        policy_goals=goals,
        policy_tools=tools,
        supported_industries=supported,
        constrained_industries=constrained,
        potential_impacts=impacts,
        evidence_quotes=list(dict.fromkeys(quotes))[:10],
        parse_method="rule_fallback",
        limitation="规则抽取仅覆盖线索词命中的句子，可能遗漏隐含含义",
    )


def parse_policy_document(text: str, title: str = "", source_id: str = "",
                          use_llm: bool = True) -> PolicyImpactResult:
    """政策文本 -> PolicyImpactResult。LLM 失败自动降级规则抽取，绝不抛异常。"""
    text = (text or "").strip()
    if not text:
        return PolicyImpactResult(title=title, parse_method="rule_fallback",
                                  limitation="输入为空", source_id=source_id)
    if use_llm:
        try:
            from agents.base_agent import BaseAgent

            raw = BaseAgent.call_llm(_LLM_PROMPT.format(text=text[:6000]), system=_LLM_SYSTEM,
                                     temperature=0.1)
            data = BaseAgent.parse_json_response(raw)
            if isinstance(data, dict):
                verified_quotes = [q for q in data.get("evidence_quotes", [])
                                   if isinstance(q, str) and q[:20] in text]
                result = PolicyImpactResult(
                    title=title or str(data.get("policy_topic", ""))[:60],
                    issuer=str(data.get("issuer", ""))[:40],
                    publish_date=str(data.get("publish_date", ""))[:10],
                    policy_topic=str(data.get("policy_topic", ""))[:80],
                    policy_goals=[str(g)[:80] for g in data.get("policy_goals", [])][:6],
                    policy_tools=[str(t)[:40] for t in data.get("policy_tools", [])][:8],
                    supported_industries=[str(i)[:20] for i in data.get("supported_industries", [])][:8],
                    constrained_industries=[str(i)[:20] for i in data.get("constrained_industries", [])][:8],
                    potential_impacts=[str(i)[:100] for i in data.get("potential_impacts", [])][:6],
                    evidence_quotes=verified_quotes[:10],
                    parse_method="llm",
                    limitation="LLM 结构化抽取，摘句已与原文比对；影响为方向判断非量化预测",
                    source_id=source_id,
                )
                if not verified_quotes and data.get("evidence_quotes"):
                    result.limitation += "；模型给出的摘句未通过原文比对，已剔除"
                return result
        except Exception as exc:  # noqa: BLE001 - degrade to rules
            logger.warning(f"policy parser LLM path failed, falling back to rules: {exc!r}")
    result = _rule_parse(text, title)
    result.source_id = source_id
    return result
