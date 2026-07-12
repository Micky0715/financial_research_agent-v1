"""Standalone DCF (discounted cash flow) valuation tool.

v2 模块3：给 AnalyzeAgent 一个可以被**显式调用**的估值计算函数，而不是把
估值判断混在 LLM 的综合总结里。计算逻辑全部是确定性的纯 Python——给定同样
的输入永远得到同样的估值区间，可以逐行讲清楚。

诚实边界（重要）：
- 这是工程近似估算，不是严格的投行级 DCF 建模；
- base_fcf 优先取"经营性现金流净额"，取不到再退化用"净利润"做代理——两者都
  不是严格意义的自由现金流（没有扣资本开支），结果会系统性偏乐观，输出里会
  明确标注 proxy 类型；
- 折现率/永续增长率无法从新闻文本可靠抽取，使用可配置默认值（config.py）；
- 估值结果仅供研究参考，不构成投资建议。

两个公开函数：
- dcf_valuation(...)          纯计算：输入参数 -> 估值明细 + bear/base/bull 区间
- extract_dcf_inputs(...)     参数抽取：来源文本 -> 参数 + 每个参数的出处标注
"""
import re
from typing import Any, Optional

from config import config
from schemas.source import Source

# 注意：不用 utils.text_utils.split_sentences —— 它在英文句点处也切分，
# "8039.65亿元" 会被切成 "8039." / "65 亿元"，带小数的金额永远抽不到。
# 财务抽取只按中文句读和换行切分。
_ZH_SENTENCE_SPLIT_RE = re.compile(r"[。！？；\n]")


def _split_zh_sentences(text: str) -> list[str]:
    return [s.strip() for s in _ZH_SENTENCE_SPLIT_RE.split(text or "") if s.strip()]


def _cfg(name: str, default):
    """config.py 里还没有该项时用默认值（DCF 配置项随模块3 提交进 config.py）。"""
    return getattr(config, name, default)

# ---------------------------------------------------------------------- #
# Pure computation
# ---------------------------------------------------------------------- #


def dcf_valuation(
    base_fcf: float,
    growth_rates: list[float],
    discount_rate: float,
    terminal_growth: float,
) -> dict[str, Any]:
    """两阶段 DCF：显式预测期逐年折现 + Gordon 永续期。

    Args:
        base_fcf: 基期自由现金流（亿元，>0）。
        growth_rates: 显式预测期各年增长率，如 [0.15, 0.12, 0.10, 0.08, 0.06]。
        discount_rate: 折现率 r（如 0.09）。
        terminal_growth: 永续增长率 g（必须 < discount_rate）。

    Returns:
        {
          "enterprise_value": 企业价值（亿元）,
          "pv_explicit": 显式期现值合计,
          "pv_terminal": 永续期现值,
          "yearly": [{year, fcf, present_value}, ...],
          "inputs": {入参回显},
        }
    """
    if base_fcf <= 0:
        raise ValueError(f"base_fcf must be positive, got {base_fcf}")
    if terminal_growth >= discount_rate:
        raise ValueError(
            f"terminal_growth ({terminal_growth}) must be < discount_rate ({discount_rate}), "
            "otherwise the Gordon terminal value diverges"
        )
    if not growth_rates:
        raise ValueError("growth_rates must not be empty")

    yearly = []
    fcf = base_fcf
    pv_explicit = 0.0
    for year, g in enumerate(growth_rates, start=1):
        fcf = fcf * (1 + g)
        pv = fcf / (1 + discount_rate) ** year
        pv_explicit += pv
        yearly.append({"year": year, "growth": round(g, 4), "fcf": round(fcf, 2), "present_value": round(pv, 2)})

    n = len(growth_rates)
    terminal_value = fcf * (1 + terminal_growth) / (discount_rate - terminal_growth)
    pv_terminal = terminal_value / (1 + discount_rate) ** n

    return {
        "enterprise_value": round(pv_explicit + pv_terminal, 2),
        "pv_explicit": round(pv_explicit, 2),
        "pv_terminal": round(pv_terminal, 2),
        "yearly": yearly,
        "inputs": {
            "base_fcf": base_fcf,
            "growth_rates": [round(g, 4) for g in growth_rates],
            "discount_rate": discount_rate,
            "terminal_growth": terminal_growth,
        },
    }


def dcf_valuation_range(
    base_fcf: float,
    growth_rates: list[float],
    discount_rate: float,
    terminal_growth: float,
) -> dict[str, Any]:
    """跑 bear/base/bull 三种情景，输出估值区间。

    情景设定（固定、可解释）：
    - bear: 各年增长率 -3pp，折现率 +1pp
    - base: 入参原值
    - bull: 各年增长率 +3pp，折现率 -1pp
    """
    scenarios = {
        "bear": ([max(g - 0.03, -0.5) for g in growth_rates], discount_rate + 0.01),
        "base": (growth_rates, discount_rate),
        "bull": ([g + 0.03 for g in growth_rates], max(discount_rate - 0.01, terminal_growth + 0.005)),
    }
    results = {
        name: dcf_valuation(base_fcf, g, r, terminal_growth) for name, (g, r) in scenarios.items()
    }
    return {
        "valuation_range": {
            "bear": results["bear"]["enterprise_value"],
            "base": results["base"]["enterprise_value"],
            "bull": results["bull"]["enterprise_value"],
            "unit": "亿元",
        },
        "scenarios": results,
        "note": (
            "工程近似估算：base_fcf 来自文本抽取的现金流/净利润代理值，未扣除资本开支；"
            "折现率与永续增长率为可配置默认值。仅供研究参考，不构成投资建议。"
        ),
    }


# ---------------------------------------------------------------------- #
# Input extraction from fetched source text
# ---------------------------------------------------------------------- #

# 亿元金额: "经营性现金流净额1332亿元" / "净利润722.01亿元" / "现金流净额 591.36 亿元"
_CASHFLOW_RE = re.compile(
    r"(?:经营[性活动]*现金流(?:量)?净额|自由现金流)[约为达]*\s*([0-9,]+(?:\.\d+)?)\s*亿"
)
_NET_PROFIT_RE = re.compile(r"(?:归母)?净利润[约为达]*\s*([0-9,]+(?:\.\d+)?)\s*亿")
# 增长率: "营收同比增长17.04%" / "同比增长 3.46%"
_REVENUE_GROWTH_RE = re.compile(r"营(?:业收入|收)[^。；\n]{0,12}?同比[增长上升]+\s*([0-9]+(?:\.\d+)?)\s*%")


def _to_float(raw: str) -> float:
    return float(raw.replace(",", ""))


def _find_first(sources: list[Source], pattern: re.Pattern) -> Optional[tuple[float, str, str]]:
    """Return (value, matched_sentence, source_id) for the first sentence matching
    `pattern` across sources, in source-score order (sources are pre-sorted by
    BrowserAgent, so earlier = higher quality)."""
    for src in sources:
        for sentence in _split_zh_sentences(src.content):
            m = pattern.search(sentence)
            if m:
                return _to_float(m.group(1)), sentence.strip()[:120], src.source_id
    return None


def _decayed_growth_path(first_year_growth: float, years: int, terminal_growth: float) -> list[float]:
    """Linear decay from the extracted first-year growth toward the terminal
    rate over the explicit period - a standard simplification when only one
    observed growth data point is available."""
    if years == 1:
        return [first_year_growth]
    step = (first_year_growth - terminal_growth) / (years - 1) if years > 1 else 0.0
    return [round(first_year_growth - step * i, 4) for i in range(years)]


def extract_dcf_inputs(
    sources: list[Source], subject: str = "", snapshot: Optional[dict[str, Any]] = None
) -> dict[str, Any]:
    """抽取 DCF 输入参数：结构化数据（AkShare）优先，其次来源文本，最后配置默认值。

    每个参数都带 provenance 标注：
    - "akshare":   来自结构化金融数据快照（附 akshare 字段名）
    - "derived":   由结构化序列派生（如营收 YoY）
    - "extracted": 从来源句子里正则抽到（附原句和 source_id）
    - "proxy":     抽到的是代理指标（净利润代替现金流）
    - "default":   使用 config 里的默认值
    """
    provenance: dict[str, Any] = {}
    snapshot = snapshot or {}

    # base_fcf 优先级：AkShare经营现金流 > 文本经营现金流 > AkShare净利润 > 文本净利润 > 默认值
    base_fcf = None
    if snapshot.get("operating_cash_flow"):
        base_fcf = snapshot["operating_cash_flow"]
        provenance["base_fcf"] = {"kind": "akshare", "value": base_fcf,
                                  "akshare_field": "akshare:operating_cash_flow",
                                  "basis": f"经营现金流净额（{snapshot.get('period', '')}，AkShare）"}
    if base_fcf is None:
        cashflow = _find_first(sources, _CASHFLOW_RE)
        if cashflow:
            base_fcf, sentence, sid = cashflow
            provenance["base_fcf"] = {"kind": "extracted", "value": base_fcf, "sentence": sentence,
                                      "source_id": sid, "basis": "经营性现金流净额"}
    if base_fcf is None and snapshot.get("net_profit"):
        base_fcf = snapshot["net_profit"]
        provenance["base_fcf"] = {"kind": "akshare", "value": base_fcf,
                                  "akshare_field": "akshare:net_profit",
                                  "basis": f"净利润（现金流代理，偏乐观；{snapshot.get('period', '')}，AkShare）"}
    if base_fcf is None:
        profit = _find_first(sources, _NET_PROFIT_RE)
        if profit:
            base_fcf, sentence, sid = profit
            provenance["base_fcf"] = {"kind": "proxy", "value": base_fcf, "sentence": sentence, "source_id": sid,
                                      "basis": "净利润（现金流代理，偏乐观）"}
    if base_fcf is None:
        base_fcf = _cfg('DCF_DEFAULT_BASE_FCF', 100.0)
        provenance["base_fcf"] = {"kind": "default", "value": base_fcf,
                                  "basis": "未从结构化数据/资料中获取到现金流/净利润，使用配置默认值"}

    # first-year growth 优先级：AkShare营收序列YoY（derived）> 文本增速 > 默认值
    g1 = None
    revenue_series = (snapshot.get("yearly_series") or {}).get("revenue") or []
    if len(revenue_series) >= 2:
        prev, last = revenue_series[-2][1], revenue_series[-1][1]
        if prev and prev > 0:
            g1 = max(min((last - prev) / prev, 0.60), -0.30)
            provenance["first_year_growth"] = {
                "kind": "derived", "value": round(g1, 4),
                "akshare_field": "akshare:revenue_yoy",
                "basis": f"营收 YoY（{revenue_series[-2][0]}->{revenue_series[-1][0]}，AkShare 序列派生）",
            }
    if g1 is None:
        growth = _find_first(sources, _REVENUE_GROWTH_RE)
        if growth:
            g1_raw, sentence, sid = growth
            g1 = min(g1_raw / 100.0, 0.60)  # cap: 单条新闻里的极端增速不外推
            provenance["first_year_growth"] = {"kind": "extracted", "value": g1, "sentence": sentence, "source_id": sid}
    if g1 is None:
        g1 = _cfg('DCF_DEFAULT_GROWTH', 0.10)
        provenance["first_year_growth"] = {"kind": "default", "value": g1}

    discount_rate = _cfg('DCF_DISCOUNT_RATE', 0.09)
    terminal_growth = _cfg('DCF_TERMINAL_GROWTH', 0.025)
    provenance["discount_rate"] = {"kind": "default", "value": discount_rate}
    provenance["terminal_growth"] = {"kind": "default", "value": terminal_growth}

    growth_rates = _decayed_growth_path(g1, _cfg('DCF_YEARS', 5), terminal_growth)

    return {
        "base_fcf": base_fcf,
        "growth_rates": growth_rates,
        "discount_rate": discount_rate,
        "terminal_growth": terminal_growth,
        "provenance": provenance,
    }


def run_dcf_for_sources(
    sources: list[Source], subject: str = "", snapshot: Optional[dict[str, Any]] = None
) -> Optional[dict[str, Any]]:
    """一步到位：抽参数 -> 跑三情景 DCF。抽取/计算任何一步失败返回 None（调用方跳过估值模块，不影响主流程）。

    v3 输出规范化：bear_value/base_value/bull_value + input_sources（逐参数
    来源：akshare:field / [sX] / config_assumption / derived）+ assumptions +
    warning + valuation_method。
    """
    try:
        params = extract_dcf_inputs(sources, subject, snapshot)
        result = dcf_valuation_range(
            base_fcf=params["base_fcf"],
            growth_rates=params["growth_rates"],
            discount_rate=params["discount_rate"],
            terminal_growth=params["terminal_growth"],
        )
        result["inputs_provenance"] = params["provenance"]

        prov = params["provenance"]
        input_sources: list[str] = []
        for name, p in prov.items():
            if p.get("akshare_field"):
                input_sources.append(f"{name} <- {p['akshare_field']} ({p['kind']})")
            elif p.get("source_id"):
                input_sources.append(f"{name} <- [{p['source_id']}] ({p['kind']})")
            else:
                input_sources.append(f"{name} <- config_assumption ({p['kind']})")
        result.update({
            "valuation_method": "two_stage_dcf",
            "bear_value": result["valuation_range"]["bear"],
            "base_value": result["valuation_range"]["base"],
            "bull_value": result["valuation_range"]["bull"],
            "input_sources": input_sources,
            "assumptions": [
                f"discount_rate={params['discount_rate']:.2%} (config_assumption)",
                f"terminal_growth={params['terminal_growth']:.2%} (config_assumption)",
                f"explicit_years={len(params['growth_rates'])}，增速线性衰减至永续增速",
            ],
            "warning": (
                "工程近似估算：base_fcf 为经营现金流或净利润代理（未扣资本开支，偏乐观）；"
                "折现率/永续增长率为配置假设。不构成目标价或投资建议。"
            ),
        })
        return result
    except Exception:  # noqa: BLE001 - valuation is auxiliary, never break the pipeline
        return None
