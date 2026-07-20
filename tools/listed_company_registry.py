"""Free public listed-company registry hard check (v4 stage H).

公司研报的硬校验之一：证券代码是否存在、能否映射到具体交易所、是否在
AkShare 全市场 A 股代码-名称表中。数据源与 tools/akshare_tool.py 相同
（stock_info_a_code_name，7 天缓存）——本模块把"是否上市"做成独立、显式
的检查项，不新增网络依赖，供 entity_validator 组合使用。

覆盖范围：仅沪/深/北交所 A 股。港股/美股/新三板/未上市企业不在覆盖范围内，
如实在 coverage_note 里说明——不冒充覆盖了全部交易所（比赛要求"不接 Wind、
不用付费数据"，免费公开渠道没有全球统一上市公司注册表）。
"""
from typing import Optional

from tools.akshare_tool import resolve_symbol

_COVERAGE_NOTE = "本注册表仅覆盖沪/深/北交所 A 股（AkShare 免费公开数据），不含港股/美股/新三板/未上市企业"


def exchange_of(code: str) -> str:
    if code.startswith(("60", "68")):
        return "上海证券交易所" + ("（科创板）" if code.startswith("68") else "")
    if code.startswith(("00", "30")):
        return "深圳证券交易所" + ("（创业板）" if code.startswith("30") else "")
    if code.startswith(("83", "87", "43", "92")):
        return "北京证券交易所"
    return "unknown"


def check_listed_status(name_or_code: str) -> dict[str, Optional[str]]:
    """公司名/代码 -> {listed, symbol, exchange, name, reason, coverage_note}。"""
    resolved = resolve_symbol(name_or_code)
    if resolved is None:
        return {"listed": False, "symbol": None, "exchange": None, "name": None,
                "reason": "未在 AkShare 全市场 A 股代码-名称表（或本项目别名表）中找到对应证券代码",
                "coverage_note": _COVERAGE_NOTE}
    code, name = resolved
    return {"listed": True, "symbol": code, "exchange": exchange_of(code), "name": name,
            "reason": f"A股代码表命中：{name}（{code}，{exchange_of(code)}）",
            "coverage_note": _COVERAGE_NOTE}
