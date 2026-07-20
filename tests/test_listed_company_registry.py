"""tools/listed_company_registry.py: 上市公司硬校验（v4 阶段H）。"""
import tools.listed_company_registry as reg


def test_exchange_of_mapping():
    assert "上海" in reg.exchange_of("600519")
    assert "科创板" in reg.exchange_of("688981")
    assert "深圳" in reg.exchange_of("000001")
    assert "创业板" in reg.exchange_of("300750")
    assert "北京" in reg.exchange_of("830001")
    assert reg.exchange_of("999999") == "unknown"


def test_check_listed_status_hit(monkeypatch):
    monkeypatch.setattr(reg, "resolve_symbol", lambda s: ("600519", "贵州茅台"))
    result = reg.check_listed_status("贵州茅台")
    assert result["listed"] is True
    assert result["symbol"] == "600519"
    assert result["exchange"] == "上海证券交易所"
    assert "coverage_note" in result


def test_check_listed_status_miss(monkeypatch):
    monkeypatch.setattr(reg, "resolve_symbol", lambda s: None)
    result = reg.check_listed_status("华为技术有限公司")
    assert result["listed"] is False
    assert result["symbol"] is None
    assert "AkShare" in result["reason"]
