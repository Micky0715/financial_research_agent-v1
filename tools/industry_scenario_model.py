"""Three-year (or longer) industry scenario simulation (v4 stage C).

变量来源四类，逐项标注（红线：不把假设写成事实）：
- historical_source:  来源正文抽到的当前市场规模/增速（带 source_id）
- external_forecast:  来源正文抽到的机构预测（带 source_id）
- config_assumption:  本模块内联的情景增速调节系数（bear/bull 相对 base 的折减/上浮）
- calculated_value:   由上述输入逐年复利计算的结果

抽不到真实市场规模基数时，用指数形式（基期=100，is_indexed=True）模拟——
明确标注，不虚构"XX亿元"规模。
"""
import re
from datetime import datetime

from schemas.industry_research import ScenarioModel, ScenarioVariable, ScenarioYear
from schemas.source import Source

_SENT_RE = re.compile(r"[。！？；\n]")
_SIZE_RE = re.compile(r"(?:市场规模|市场空间|产值)[^\d%（(]{0,20}?([\d.]+)\s*(万亿|千亿|亿)")
_FORECAST_RE = re.compile(r"(?:预计|预测|有望|将达)[^。；]{0,40}?(?:复合增长率|CAGR|年均增长|增速)[^\d%（(]{0,10}?([\d.]+)\s*%")
_GROWTH_RE = re.compile(r"(?:同比增长|增速|复合增长率|CAGR)[^\d%（(]{0,12}?([\d.]+)\s*%")

# 情景调节系数（config_assumption，写死可审查）
_BEAR_FACTOR = 0.4   # bear 增速 = base × 0.4
_BULL_FACTOR = 1.5   # bull 增速 = base × 1.5
_DEFAULT_BASE_GROWTH = 8.0  # % 无任何来源增速时的保守假设
_YEARS = 3


def _extract_first(sources: list[Source], regex: re.Pattern):
    for src in sources:
        for sent in _SENT_RE.split(src.content or ""):
            sent = sent.strip()
            if not (10 <= len(sent) <= 250):
                continue
            m = regex.search(sent)
            if m:
                try:
                    return float(m.group(1)), m, src.source_id, sent[:150]
                except ValueError:
                    continue
    return None, None, "", ""


def build_scenario_model(topic: str, sources: list[Source],
                         years: int = _YEARS) -> ScenarioModel:
    model = ScenarioModel(industry=topic)
    this_year = datetime.now().year

    # 1) 市场规模基数（historical_source 优先）
    size_val, size_m, size_sid, size_sent = _extract_first(sources, _SIZE_RE)
    base_size, unit, indexed = None, "亿元", False
    if size_val is not None:
        mult = {"万亿": 10000, "千亿": 1000, "亿": 1}[size_m.group(2)]
        base_size = round(size_val * mult, 1)
        model.inputs.append(ScenarioVariable(
            name="当前市场规模", base_value=base_size, unit="亿元",
            value_kind="historical_source", source_id=size_sid, note=size_sent))
    else:
        base_size, unit, indexed = 100.0, "指数(基期=100)", True
        model.inputs.append(ScenarioVariable(
            name="当前市场规模", base_value=100.0, unit=unit, value_kind="config_assumption",
            note="来源中未抽取到可靠市场规模数字，以指数形式模拟（不虚构规模）"))

    # 2) base 增速（external_forecast > historical_source > config_assumption）
    fc_val, _, fc_sid, fc_sent = _extract_first(sources, _FORECAST_RE)
    if fc_val is not None and 0 < fc_val <= 100:
        base_growth, kind, sid, note = fc_val, "external_forecast", fc_sid, fc_sent
    else:
        g_val, _, g_sid, g_sent = _extract_first(sources, _GROWTH_RE)
        if g_val is not None and 0 < g_val <= 100:
            base_growth, kind, sid, note = g_val, "historical_source", g_sid, \
                f"以历史增速延展为 base 情景：{g_sent}"
        else:
            base_growth, kind, sid, note = _DEFAULT_BASE_GROWTH, "config_assumption", "", \
                f"来源无可靠增速，取保守假设 {_DEFAULT_BASE_GROWTH}%"
    model.inputs.append(ScenarioVariable(name="base情景年增速", base_value=base_growth,
                                         unit="%", value_kind=kind, source_id=sid, note=note))
    model.inputs.append(ScenarioVariable(
        name="bear情景年增速", base_value=round(base_growth * _BEAR_FACTOR, 1), unit="%",
        value_kind="config_assumption", note=f"= base × {_BEAR_FACTOR}（情景调节系数为配置假设）"))
    model.inputs.append(ScenarioVariable(
        name="bull情景年增速", base_value=round(base_growth * _BULL_FACTOR, 1), unit="%",
        value_kind="config_assumption", note=f"= base × {_BULL_FACTOR}（情景调节系数为配置假设）"))

    # 3) 逐年复利推演（calculated_value）
    rates = {"bear": base_growth * _BEAR_FACTOR / 100, "base": base_growth / 100,
             "bull": base_growth * _BULL_FACTOR / 100}
    values = {k: base_size for k in rates}
    for i in range(1, years + 1):
        for k, r in rates.items():
            values[k] = values[k] * (1 + r)
        model.years.append(ScenarioYear(
            year=this_year + i,
            bear=round(values["bear"], 1), base=round(values["base"], 1),
            bull=round(values["bull"], 1), unit=unit))

    model.is_indexed = indexed
    model.assumptions = [
        f"模拟期：{this_year + 1}-{this_year + years}（{years}年）",
        f"bear/bull 为 base 增速 ×{_BEAR_FACTOR}/×{_BULL_FACTOR}（配置假设，非预测）",
        "未建模：政策突变、技术替代、价格战等非线性冲击",
    ]
    model.limitation = ("情景值为模型测算结果（calculated_value），输入构成见 inputs 逐项标注；"
                        "这是工程化情景演示，不构成行业预测保证"
                        + ("；因无真实规模基数，输出为指数形式" if indexed else ""))
    return model
