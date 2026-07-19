"""Grey-rhino risk monitor over real macro indicators (v4 stage A).

八类灰犀牛风险的规则监控。每条风险只在有真实指标证据时给出 low/medium/high，
指标缺失时如实输出 unknown + limitation，绝不用默认值凑判断。阈值是工程
启发式（写在规则里可审查），不是监管口径。
"""
from typing import Optional

from schemas.macro_data import GreyRhinoRisk, MacroSnapshot


def _val(s: MacroSnapshot, key: str) -> Optional[float]:
    p = s.indicators.get(key)
    return p.value if p else None


def _chg(s: MacroSnapshot, key: str) -> Optional[float]:
    p = s.indicators.get(key)
    if p is None or p.value is None or p.previous_value is None:
        return None
    return round(p.value - p.previous_value, 4)


def _ev(s: MacroSnapshot, key: str) -> str:
    p = s.indicators.get(key)
    if p is None or p.value is None:
        return f"{key}: 数据缺失"
    return f"{p.indicator_name}={p.value}{p.unit}（{p.period}，前值{p.previous_value}，来源AkShare·{p.source_name}）"


def _series_drop(s: MacroSnapshot, key: str, n: int = 6) -> Optional[float]:
    """近 n 期累计变化；序列不足返回 None。"""
    ser = s.series.get(key) or []
    if len(ser) < n:
        return None
    return round(ser[-1][1] - ser[-n][1], 4)


def monitor_grey_rhinos(snapshot: MacroSnapshot) -> list[GreyRhinoRisk]:
    risks: list[GreyRhinoRisk] = []

    # 1) 高杠杆/信用扩张失衡：M2-M1 剪刀差走阔
    m1, m2 = _val(snapshot, "m1_yoy"), _val(snapshot, "m2_yoy")
    if m1 is not None and m2 is not None:
        scissors = round(m2 - m1, 2)
        level = "high" if scissors >= 6 else "medium" if scissors >= 3 else "low"
        risks.append(GreyRhinoRisk(
            risk_name="高杠杆与资金空转", risk_level=level,
            triggered_indicators=["m1_yoy", "m2_yoy"],
            evidence=[_ev(snapshot, "m1_yoy"), _ev(snapshot, "m2_yoy"),
                      f"M2-M1剪刀差={scissors}pct（阈值：≥3 medium，≥6 high，工程启发式）"],
            possible_transmission_path="剪刀差走阔→资金活性弱→企业投资意愿低→信用扩张依赖债务滚动",
            limitation="缺少宏观杠杆率与社融数据（社融接口不可用），仅以货币结构近似",
        ))
    else:
        risks.append(GreyRhinoRisk(
            risk_name="高杠杆与资金空转", risk_level="unknown",
            limitation="M1/M2 数据缺失，无法判定"))

    # 2) 房地产下行
    house = _val(snapshot, "house_prosperity_index")
    if house is not None:
        drop6 = _series_drop(snapshot, "house_prosperity_index")
        level = "high" if house < 92 else "medium" if house < 95 else "low"
        risks.append(GreyRhinoRisk(
            risk_name="房地产市场下行", risk_level=level,
            triggered_indicators=["house_prosperity_index"],
            evidence=[_ev(snapshot, "house_prosperity_index"),
                      f"近6期景气指数变化={drop6 if drop6 is not None else '序列不足'}",
                      "阈值：<95 medium，<92 high（100为景气分界，工程启发式）"],
            possible_transmission_path="景气下行→投资收缩→地产链需求走弱→地方财政与银行资产质量承压",
            limitation="景气指数为综合指标，未覆盖单城市分化与房企个体信用",
        ))
    else:
        risks.append(GreyRhinoRisk(risk_name="房地产市场下行", risk_level="unknown",
                                   limitation="国房景气指数缺失，无法判定"))

    # 3) 外需下降
    exp = _val(snapshot, "exports_yoy")
    if exp is not None:
        level = "high" if exp < -5 else "medium" if exp < 0 else "low"
        risks.append(GreyRhinoRisk(
            risk_name="外需下降", risk_level=level,
            triggered_indicators=["exports_yoy"],
            evidence=[_ev(snapshot, "exports_yoy"), "阈值：<0 medium，<-5% high（工程启发式）"],
            possible_transmission_path="出口回落→制造业订单/开工下滑→就业与制造业投资承压",
            limitation="单月出口同比波动大，且最新披露期可能滞后",
        ))
    else:
        risks.append(GreyRhinoRisk(risk_name="外需下降", risk_level="unknown",
                                   limitation="出口同比数据缺失，无法判定"))

    # 4) 通胀反弹
    cpi, ppi = _val(snapshot, "cpi_yoy"), _val(snapshot, "ppi_yoy")
    if cpi is not None or ppi is not None:
        level = "high" if (cpi or 0) > 3 or (ppi or 0) > 6 else \
            "medium" if (cpi or 0) > 2 or (ppi or 0) > 4 else "low"
        risks.append(GreyRhinoRisk(
            risk_name="通胀反弹", risk_level=level,
            triggered_indicators=[k for k in ("cpi_yoy", "ppi_yoy") if _val(snapshot, k) is not None],
            evidence=[_ev(snapshot, "cpi_yoy"), _ev(snapshot, "ppi_yoy"),
                      "阈值：CPI>2%或PPI>4% medium，CPI>3%或PPI>6% high（工程启发式）"],
            possible_transmission_path="通胀上行→政策宽松受约束→利率与流动性预期收紧→高估值资产承压",
            limitation="未含核心CPI与食品能源拆分",
        ))

    # 5) 汇率快速波动
    fx_chg = _chg(snapshot, "usd_cny")
    fx = _val(snapshot, "usd_cny")
    if fx is not None and fx_chg is not None:
        pct = abs(fx_chg / fx) * 100 if fx else 0
        level = "high" if pct > 0.5 else "medium" if pct > 0.2 else "low"
        risks.append(GreyRhinoRisk(
            risk_name="汇率快速波动", risk_level=level,
            triggered_indicators=["usd_cny"],
            evidence=[_ev(snapshot, "usd_cny"),
                      f"单期中间价变动幅度={round(pct, 3)}%（阈值：>0.2% medium，>0.5% high，工程启发式）"],
            possible_transmission_path="汇率急变→外币负债/出口企业盈利波动→跨境资金流动扰动流动性",
            limitation="仅用相邻两期中间价，未含离岸价与波动率",
        ))
    else:
        risks.append(GreyRhinoRisk(risk_name="汇率快速波动", risk_level="unknown",
                                   limitation="汇率数据缺失，无法判定"))

    # 6) 流动性收紧
    bond_drop = _series_drop(snapshot, "cn_bond_10y", 20)
    bond = _val(snapshot, "cn_bond_10y")
    if bond is not None:
        rising = bond_drop is not None and bond_drop > 0.2
        level = "medium" if rising else "low"
        risks.append(GreyRhinoRisk(
            risk_name="流动性收紧", risk_level=level,
            triggered_indicators=["cn_bond_10y"],
            evidence=[_ev(snapshot, "cn_bond_10y"),
                      f"近20个交易日10Y收益率变动={bond_drop if bond_drop is not None else '序列不足'}pct",
                      "阈值：+0.2pct medium（工程启发式）"],
            possible_transmission_path="无风险利率上行→债市调整→估值分母抬升→高久期资产回撤",
            limitation="未含资金利率(DR007)与央行公开市场操作数据",
        ))

    # 7) 地缘冲突（无结构化指标，仅能给出监测口径）
    risks.append(GreyRhinoRisk(
        risk_name="地缘冲突", risk_level="unknown",
        evidence=["无可用结构化指标；美中利差与汇率为间接观察窗口",
                  _ev(snapshot, "us_bond_10y"), _ev(snapshot, "usd_cny")],
        possible_transmission_path="地缘冲突→能源/航运成本与供应链扰动→输入性通胀与出口不确定性",
        limitation="本系统无地缘事件结构化数据源，此项只能依赖新闻检索定性判断，不给量化等级",
    ))

    # 8) 产业链集中风险（依赖出口结构，结构化数据不可得）
    risks.append(GreyRhinoRisk(
        risk_name="产业链集中风险", risk_level="unknown",
        evidence=["缺少分行业出口/进口依存度结构化免费数据"],
        possible_transmission_path="关键环节对单一外部供给/需求依赖→外部冲击沿产业链放大",
        limitation="需行业级贸易结构数据；当前免费接口不可得，如实标注 unknown",
    ))

    return risks
