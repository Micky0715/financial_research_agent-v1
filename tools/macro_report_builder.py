"""Deterministic macro report sections from real structured data (v4 stage A).

宏观报告的硬数据部分（关键指标表/政策解读/传导路径/灰犀牛/数据来源）由本模块
从 MacroSnapshot 等结构化结果确定性渲染——不经 LLM，杜绝数字幻觉；解读性
段落（核心结论/国内外对比/资产影响）由 ReportAgent 的 LLM 撰写，失败时用
规则文本兜底。每个数字所在行都带 AkShare+原始机构标注，供 number grounding
分类为 akshare_number。
"""
from typing import Any, Optional

from schemas.macro_data import GreyRhinoRisk, MacroSnapshot, PolicyImpactResult, TransmissionPath

_LEVEL_ZH = {"low": "低", "medium": "中", "high": "高", "unknown": "未知"}


def build_indicator_table_md(snapshot: MacroSnapshot) -> str:
    if not snapshot.indicators:
        return "（本次未获取到结构化宏观指标）"
    lines = [
        "| 指标 | 最新值 | 报告期 | 前值 | 数据来源 |",
        "|---|---|---|---|---|",
    ]
    for p in snapshot.indicators.values():
        prev = p.previous_value if p.previous_value is not None else "—"
        lines.append(
            f"| {p.indicator_name} | {p.value}{p.unit} | {p.period} | {prev} "
            f"| AkShare·{p.source_name} |"
        )
    if snapshot.missing_indicators:
        lines.append("")
        lines.append(f"缺失指标（数据源不可用，如实标注）：{'、'.join(snapshot.missing_indicators)}")
    return "\n".join(lines)


def build_policy_section_md(policies: list[PolicyImpactResult]) -> str:
    if not policies:
        return "本次检索来源中未识别出可结构化解析的政策文件，政策解读依据正文来源综述。"
    blocks = []
    for pol in policies:
        lines = [f"**{pol.title or pol.policy_topic}**"
                 + (f"（{pol.issuer}，{pol.publish_date}）" if pol.issuer else "")]
        if pol.policy_tools:
            lines.append(f"- 政策工具：{'、'.join(pol.policy_tools)}")
        if pol.policy_goals:
            lines.append(f"- 政策目标：{'；'.join(pol.policy_goals[:3])}")
        if pol.supported_industries:
            lines.append(f"- 受益方向：{'、'.join(pol.supported_industries)}")
        if pol.constrained_industries:
            lines.append(f"- 受限方向：{'、'.join(pol.constrained_industries)}")
        for imp in pol.potential_impacts[:3]:
            lines.append(f"- 潜在影响：{imp}")
        if pol.source_id:
            lines.append(f"- 依据来源：[{pol.source_id}]（解析方式：{pol.parse_method}）")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def build_transmission_section_md(paths: list[TransmissionPath]) -> str:
    if not paths:
        return "当前指标变动未激活任何预置传导链（或指标缺失），不做无依据推演。"
    blocks = []
    for path in paths:
        arrow = " → ".join(
            f"{s.variable}{'↑' if s.direction == 'up' else '↓' if s.direction == 'down' else '(分化)'}"
            for s in path.steps
        )
        lines = [
            f"**{path.trigger}**：{arrow}",
            f"- 影响板块：{'、'.join(path.affected_sectors)}",
            f"- 触发依据指标：{'、'.join(path.evidence_indicators)}（见关键指标表）",
            f"- 说明：{path.confidence_note}",
        ]
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def build_grey_rhino_section_md(risks: list[GreyRhinoRisk]) -> str:
    if not risks:
        return "（灰犀牛监控未运行）"
    lines = [
        "| 风险 | 等级 | 触发指标 | 关键证据 |",
        "|---|---|---|---|",
    ]
    details = []
    for r in risks:
        ev = r.evidence[0] if r.evidence else r.limitation or "—"
        lines.append(
            f"| {r.risk_name} | {_LEVEL_ZH.get(r.risk_level, r.risk_level)} "
            f"| {'、'.join(r.triggered_indicators) or '—'} | {ev} |"
        )
        if r.risk_level in ("medium", "high") and r.possible_transmission_path:
            details.append(f"- **{r.risk_name}**（{_LEVEL_ZH[r.risk_level]}）：{r.possible_transmission_path}。"
                           f"局限：{r.limitation}")
    out = "\n".join(lines)
    if details:
        out += "\n\n中高风险传导说明：\n\n" + "\n".join(details)
    out += "\n\n注：风险等级为规则阈值判定（阈值见 tools/grey_rhino_monitor.py，工程启发式），不构成风险预测保证。"
    return out


def build_data_source_section_md(snapshot: MacroSnapshot) -> str:
    seen = {}
    for p in snapshot.indicators.values():
        seen.setdefault((p.source_name, p.original_platform, p.endpoint), []).append(p.indicator_name)
    lines = []
    for (source, platform, endpoint), names in seen.items():
        lines.append(f"- **{source}**（经 AkShare `{endpoint}`，转载平台：{platform}，"
                     f"无直接原文URL）：{'、'.join(names[:6])}{'等' if len(names) > 6 else ''}")
    lines.append(f"- 数据抓取时间：{snapshot.fetched_at}；各指标报告期见关键指标表，存在发布滞后。")
    return "\n".join(lines)


def assemble_macro_report(topic: str, llm_sections: dict[str, str],
                          bundle: dict[str, Any], sources_md: str) -> str:
    """组装完整宏观研报 markdown。llm_sections 缺失的解读段落用规则文本兜底。"""
    snapshot = MacroSnapshot(**bundle["macro_snapshot"])
    policies = [PolicyImpactResult(**p) for p in bundle.get("policy_analysis", [])]
    paths = [TransmissionPath(**p) for p in bundle.get("transmission_paths", [])]
    risks = [GreyRhinoRisk(**r) for r in bundle.get("grey_rhino_risks", [])]

    core = llm_sections.get("core_conclusion") or _fallback_core(snapshot, risks)
    compare = llm_sections.get("domestic_international_comparison") or _fallback_compare(snapshot)
    impact = llm_sections.get("asset_industry_impact") or _fallback_impact(paths)

    return f"""# {topic} 宏观研究报告

## 核心结论

{core}

## 关键指标表

{build_indicator_table_md(snapshot)}

## 政策解读

{build_policy_section_md(policies)}

## 宏观变量传导路径

{build_transmission_section_md(paths)}

## 国内外对比

{compare}

## 资产与行业影响

{impact}

## 灰犀牛风险监控

{build_grey_rhino_section_md(risks)}

## 数据来源

{build_data_source_section_md(snapshot)}

{sources_md}

## 免责声明

本报告由自动化研究系统基于免费公开数据生成，宏观指标存在发布滞后与口径差异，
传导链与风险等级为规则推演而非专业预测，不构成投资建议；使用者需自行核实数据并承担决策风险。
"""


def _fallback_core(snapshot: MacroSnapshot, risks: list[GreyRhinoRisk]) -> str:
    parts = []
    for key in ("gdp_yoy", "cpi_yoy", "pmi_manufacturing", "m2_yoy"):
        p = snapshot.indicators.get(key)
        if p and p.value is not None:
            parts.append(f"{p.indicator_name}为{p.value}{p.unit}（{p.period}，AkShare·{p.source_name}）")
    high = [r.risk_name for r in risks if r.risk_level == "high"]
    text = "；".join(parts) if parts else "本次未获取到核心宏观指标"
    if high:
        text += f"。规则监控提示高风险项：{'、'.join(high)}"
    return text + "。（LLM 综合不可用，本段为规则拼接，解读深度有限。）"


def _fallback_compare(snapshot: MacroSnapshot) -> str:
    cn = snapshot.indicators.get("cn_bond_10y")
    us = snapshot.indicators.get("us_bond_10y")
    fed = snapshot.indicators.get("us_fed_rate")
    lines = []
    if cn and us and cn.value is not None and us.value is not None:
        lines.append(f"- 中美10年期国债利差为{round(cn.value - us.value, 2)}个百分点"
                     f"（中国{cn.value}% vs 美国{us.value}%，{cn.period}，AkShare·中债/美国财政部，模型测算利差）")
    if fed and fed.value is not None:
        lines.append(f"- 美联储联邦基金目标利率为{fed.value}%（{fed.period}，AkShare·美联储）")
    return "\n".join(lines) if lines else "国际对比指标缺失，本次不做国内外对比。"


def _fallback_impact(paths: list[TransmissionPath]) -> str:
    if not paths:
        return "指标变动未激活传导链，暂无行业影响推演。"
    lines = [f"- {p.trigger}：主要影响 {'、'.join(p.affected_sectors[:4])}（规则推演，方向性参考）"
             for p in paths]
    return "\n".join(lines)
