"""Rule-graph macro transmission chains (v4 stage A).

可解释的规则传导图：每条链是 触发条件 -> 有序传导步骤 -> 受影响行业。
触发条件由 MacroSnapshot 中的真实指标判定（指标 key 记入
evidence_indicators）；没有指标支撑的链不会被激活。这是规则推演，
不是因果模型预测——每条输出自带 confidence_note。
"""
from typing import Optional

from schemas.macro_data import MacroSnapshot, TransmissionPath, TransmissionStep


def _chg(snapshot: MacroSnapshot, key: str) -> Optional[float]:
    """最新值 - 前值；任一缺失返回 None。"""
    p = snapshot.indicators.get(key)
    if p is None or p.value is None or p.previous_value is None:
        return None
    return round(p.value - p.previous_value, 4)


def _steps(*pairs: tuple[str, str, str]) -> list[TransmissionStep]:
    return [TransmissionStep(variable=v, direction=d, rationale=r) for v, d, r in pairs]


def build_transmission_paths(snapshot: MacroSnapshot) -> list[TransmissionPath]:
    """根据快照中真实指标的变动方向，激活对应的传导链。"""
    paths: list[TransmissionPath] = []

    # 1) 利率下行链：LPR 或 10Y 国债收益率下行
    lpr_chg, bond_chg = _chg(snapshot, "lpr_1y"), _chg(snapshot, "cn_bond_10y")
    if (lpr_chg is not None and lpr_chg < 0) or (bond_chg is not None and bond_chg < 0):
        paths.append(TransmissionPath(
            trigger="利率下行",
            steps=_steps(
                ("企业与居民融资成本", "down", "LPR/无风险利率下行直接降低新增与重定价贷款利率"),
                ("信贷需求与投资意愿", "up", "融资成本下降提高投资项目可行性"),
                ("利率敏感行业景气度", "up", "地产链/基建/高杠杆制造业对利率最敏感"),
                ("权益资产估值分母", "down", "折现率下降对长久期资产估值形成支撑"),
            ),
            affected_sectors=["房地产", "基建", "银行(息差承压)", "高股息资产", "成长股估值"],
            evidence_indicators=[k for k, c in [("lpr_1y", lpr_chg), ("cn_bond_10y", bond_chg)]
                                 if c is not None and c < 0],
        ))

    # 2) 汇率变动链
    fx_chg = _chg(snapshot, "usd_cny")
    if fx_chg is not None and abs(fx_chg) > 1e-4:
        depreciating = fx_chg > 0  # 人民币贬值（元/美元 上升）
        paths.append(TransmissionPath(
            trigger="人民币" + ("贬值" if depreciating else "升值"),
            steps=_steps(
                ("出口企业人民币计价收入", "up" if depreciating else "down",
                 "汇率折算直接影响出口收入与毛利"),
                ("进口成本", "up" if depreciating else "down", "原材料/设备进口成本随汇率变动"),
                ("外币负债企业汇兑损益", "down" if depreciating else "up",
                 "美元负债企业在贬值周期确认汇兑损失"),
                ("行业利润分化", "mixed", "出口型受益/进口依赖型承压（贬值情形，升值反向）"),
            ),
            affected_sectors=["出口制造(家电/电子/纺服)", "航空(美元负债)", "造纸(进口浆)", "石化(进口原料)"],
            evidence_indicators=["usd_cny"],
        ))

    # 3) 通胀链：CPI/PPI 上行
    cpi_chg, ppi_chg = _chg(snapshot, "cpi_yoy"), _chg(snapshot, "ppi_yoy")
    if (cpi_chg is not None and cpi_chg > 0) or (ppi_chg is not None and ppi_chg > 0):
        paths.append(TransmissionPath(
            trigger="通胀回升",
            steps=_steps(
                ("上游资源品价格", "up", "PPI 回升反映工业品价格上行"),
                ("中游制造成本", "up", "原材料涨价压缩未能提价环节的毛利"),
                ("终端消费价格", "up", "成本向 CPI 的传导取决于需求强度"),
                ("货币政策宽松空间", "down", "通胀约束宽松节奏"),
            ),
            affected_sectors=["上游资源(受益)", "中游制造(承压)", "必选消费(转嫁能力分化)", "债券(承压)"],
            evidence_indicators=[k for k, c in [("cpi_yoy", cpi_chg), ("ppi_yoy", ppi_chg)]
                                 if c is not None and c > 0],
        ))

    # 4) 流动性链：M1/M2 变动
    m1_chg, m2_chg = _chg(snapshot, "m1_yoy"), _chg(snapshot, "m2_yoy")
    if m1_chg is not None or m2_chg is not None:
        easing = (m1_chg or 0) + (m2_chg or 0) > 0
        paths.append(TransmissionPath(
            trigger="货币供应" + ("扩张" if easing else "增速回落"),
            steps=_steps(
                ("市场流动性", "up" if easing else "down", "M1/M2 同比变化反映资金面松紧"),
                ("企业活期资金活性", "up" if easing else "down", "M1 增速对应企业经营活跃度"),
                ("风险资产资金面", "up" if easing else "down", "流动性环境影响估值弹性"),
            ),
            affected_sectors=["权益市场整体", "小盘/成长风格(对流动性最敏感)", "债券市场"],
            evidence_indicators=[k for k, c in [("m1_yoy", m1_chg), ("m2_yoy", m2_chg)]
                                 if c is not None],
        ))

    # 5) 外需链：出口增速变化
    exp_chg = _chg(snapshot, "exports_yoy")
    if exp_chg is not None:
        paths.append(TransmissionPath(
            trigger="外需" + ("走强" if exp_chg > 0 else "走弱"),
            steps=_steps(
                ("出口订单与开工率", "up" if exp_chg > 0 else "down", "出口同比变化直接反映外需"),
                ("制造业投资与就业", "up" if exp_chg > 0 else "down", "出口链企业扩产/收缩传导至资本开支"),
                ("经常项目与汇率支撑", "up" if exp_chg > 0 else "down", "贸易顺差影响汇率基本面"),
            ),
            affected_sectors=["出口制造", "航运港口", "跨境电商", "上游原材料(间接)"],
            evidence_indicators=["exports_yoy"],
        ))

    # 6) 地产链：国房景气指数走弱
    house_chg = _chg(snapshot, "house_prosperity_index")
    if house_chg is not None and house_chg < 0:
        paths.append(TransmissionPath(
            trigger="房地产景气下行",
            steps=_steps(
                ("新开工与土地购置", "down", "景气指数走弱对应投资端收缩"),
                ("地产链需求(建材/家居/家电)", "down", "竣工与销售下滑传导至后周期消费"),
                ("地方政府性基金收入", "down", "土地出让收入下降约束基建资金"),
                ("银行涉房资产质量", "down", "抵押品价值与开发贷风险上升"),
            ),
            affected_sectors=["房地产", "建材", "家居家电", "银行", "地方基建"],
            evidence_indicators=["house_prosperity_index"],
        ))

    return paths
