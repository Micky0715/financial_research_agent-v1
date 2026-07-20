"""AkShare data lineage report (v4 stage I).

Usage:
    python scripts/build_data_lineage_report.py [--symbols 600519,002594]

统计公司财务快照（field_lineage）与宏观指标（MacroDataPoint，字段本身就是
lineage 完整的）两类数据源的：字段总数/有原始来源字段数/无直接URL字段数/
derived字段数/missing字段数，并给出典型示例。全部来自真实抓取，无网络时
如实输出 insufficient data，不编造统计数字。
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")

from config import config  # noqa: E402
from tools.akshare_tool import fetch_financial_snapshot  # noqa: E402
from tools.data_lineage import lineage_summary  # noqa: E402
from tools.macro_data_collector import fetch_macro_snapshot  # noqa: E402

_DEFAULT_SYMBOLS = ["600519", "002594"]


def _macro_lineage_from_snapshot(snapshot: dict) -> dict[str, dict]:
    """MacroDataPoint 本身字段就完整对齐 lineage 结构，直接映射即可。"""
    out = {}
    for key, p in snapshot.get("indicators", {}).items():
        out[key] = {
            "normalized_field": p["indicator_key"], "raw_field": p["indicator_name"],
            "value": p["value"], "unit": p["unit"], "period": p["period"],
            "provider": p["provider"], "original_platform": p["original_platform"],
            "endpoint_description": f"{p['endpoint']}（{p['source_name']} 发布，经 AkShare 转载）",
            "no_direct_source_url": p["no_direct_source_url"],
            "derived_from": [], "missing": p["value"] is None,
        }
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description="AkShare 字段级数据血缘报告")
    parser.add_argument("--symbols", default=",".join(_DEFAULT_SYMBOLS))
    args = parser.parse_args()
    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]

    lines = [
        "# Data Lineage Report（AkShare 字段级数据血缘）",
        "",
        "本报告统计的不是笼统的 `provider=akshare`，而是逐字段的："
        "原始字段名、取值、单位、报告期、原始发布/转载平台、接口说明（多数 AkShare "
        "接口不回传精确原始 URL，如实标注 no_direct_source_url）、是否推导计算、是否缺失。",
        "",
    ]

    all_summaries = []
    examples: list[dict] = []

    # 1) 公司财务快照 lineage
    lines += ["## 公司财务快照（FinancialDataSnapshot.field_lineage）", ""]
    for symbol in symbols:
        try:
            env = fetch_financial_snapshot(symbol)
        except Exception as exc:  # noqa: BLE001
            lines.append(f"- {symbol}: insufficient data（抓取失败: {exc!r}）")
            continue
        snapshot = env.get("snapshot")
        if not snapshot or not snapshot.get("field_lineage"):
            lines.append(f"- {symbol}: insufficient data（无快照或无 lineage）")
            continue
        lineage = snapshot["field_lineage"]
        s = lineage_summary(lineage)
        all_summaries.append(s)
        lines.append(f"- **{snapshot.get('company_name', symbol)}（{symbol}）**：{s}")
        for field in ("revenue", "debt_ratio", "ps"):
            if field in lineage and len(examples) < 6:
                examples.append(lineage[field])

    # 2) 宏观指标 lineage
    lines += ["", "## 宏观指标（MacroDataPoint，字段结构本身即完整血缘）", ""]
    try:
        macro_env = fetch_macro_snapshot(use_cache=True)
        macro_snapshot = macro_env.get("snapshot")
    except Exception as exc:  # noqa: BLE001
        macro_snapshot = None
        lines.append(f"insufficient data（宏观快照抓取失败: {exc!r}）")
    if macro_snapshot:
        macro_lineage = _macro_lineage_from_snapshot(macro_snapshot)
        s = lineage_summary(macro_lineage)
        all_summaries.append(s)
        lines.append(f"- 22 项宏观指标：{s}")
        for key in ("gdp_yoy", "usd_cny"):
            if key in macro_lineage and len(examples) < 8:
                examples.append(macro_lineage[key])

    # 3) 汇总统计
    lines += ["", "## 汇总统计", ""]
    if all_summaries:
        total = sum(s["total_fields"] for s in all_summaries)
        with_source = sum(s["fields_with_source"] for s in all_summaries)
        no_url = sum(s["fields_no_direct_url"] for s in all_summaries)
        derived = sum(s["derived_fields"] for s in all_summaries)
        missing = sum(s["missing_fields"] for s in all_summaries)
        lines += [
            f"- 字段总数: {total}",
            f"- 有原始来源（provider+endpoint 已登记）字段数: {with_source}",
            f"- 无直接来源 URL 字段数: {no_url}（AkShare 转载性质决定，如实标注，非缺陷）",
            f"- 推导（derived）字段数: {derived}",
            f"- 缺失字段数: {missing}",
        ]
    else:
        lines.append("insufficient data（本次未成功抓取任何真实数据，不编造统计）")

    lines += ["", "## 典型 lineage 示例", ""]
    for e in examples:
        lines.append(
            f"- `{e['normalized_field']}`: raw_field={e['raw_field']!r}, value={e['value']}, "
            f"unit={e['unit']}, platform={e['original_platform']}, "
            f"endpoint={e['endpoint_description']}, no_direct_source_url={e['no_direct_source_url']}, "
            f"derived_from={e['derived_from']}, missing={e['missing']}")
    if not examples:
        lines.append("insufficient data")

    out = config.EVAL_DIR / "data_lineage_report.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"Data lineage report saved to {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
