"""JSONL store for historical tracking records (v4 stage D).

outputs/tracking/records.jsonl，追加写；每条是一个 TrackingRecord。
工作流成功跑完公司研报后 best-effort 追加一条（见 orchestrator）；
跟踪报告构建时读取该主体的全部历史记录做跨期对比。存储失败绝不影响主流程。
"""
import json
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from config import config
from schemas.tracking import TrackingRecord
from utils.logger import logger

_STORE_PATH = config.OUTPUT_DIR / "tracking" / "records.jsonl"


def append_record(record: TrackingRecord) -> bool:
    try:
        _STORE_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(_STORE_PATH, "a", encoding="utf-8") as f:
            f.write(record.model_dump_json() + "\n")
        return True
    except Exception as exc:  # noqa: BLE001 - store failure must not break the pipeline
        logger.warning(f"tracking store append failed: {exc!r}")
        return False


def load_records(symbol: str = "", entity: str = "") -> list[TrackingRecord]:
    records: list[TrackingRecord] = []
    if not _STORE_PATH.exists():
        return records
    try:
        for line in _STORE_PATH.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = TrackingRecord(**json.loads(line))
            except Exception:  # noqa: BLE001 - skip corrupt lines
                continue
            if (symbol and rec.symbol == symbol) or (entity and rec.entity == entity) \
                    or (not symbol and not entity):
                records.append(rec)
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"tracking store read failed: {exc!r}")
    return records


def latest_record(symbol: str, before: Optional[str] = None) -> Optional[TrackingRecord]:
    """该 symbol 最近一条记录（可选：created_at 早于 before 的最近一条）。"""
    recs = [r for r in load_records(symbol=symbol)
            if before is None or (r.created_at and r.created_at < before)]
    return max(recs, key=lambda r: r.created_at or "") if recs else None


def record_from_run(request: Any, run_id: str, report_path: str,
                    analysis_result: dict, evaluation: dict) -> Optional[TrackingRecord]:
    """从一次成功的 pipeline run 提取跟踪记录（best-effort，不抛异常）。"""
    try:
        snapshot = analysis_result.get("financial_snapshot_full") or {}
        analysis = analysis_result.get("analysis", {})
        dcf = analysis_result.get("dcf_valuation_full") or {}
        risks = []
        risk_analysis = analysis.get("risk_analysis")
        if isinstance(risk_analysis, dict):
            for v in risk_analysis.values():
                if isinstance(v, list):
                    risks += [str(x)[:100] for x in v[:2]]
                elif isinstance(v, str) and v not in ("", "资料不足"):
                    risks.append(v[:100])
        elif isinstance(risk_analysis, str):
            risks.append(risk_analysis[:200])
        assumptions = {}
        if dcf:
            base_inputs = dcf.get("scenarios", {}).get("base", {}).get("inputs", {})
            assumptions = {"valuation_range": dcf.get("valuation_range"),
                           "base_inputs": base_inputs}
        record = TrackingRecord(
            entity=getattr(request, "topic", ""),
            symbol=snapshot.get("symbol", ""),
            period=snapshot.get("period", ""),
            report_type=getattr(request, "report_type", ""),
            financial_snapshot={k: snapshot.get(k) for k in (
                "revenue", "net_profit", "gross_margin", "roe", "operating_cash_flow",
                "debt_ratio", "pe", "pb", "market_cap", "price") if snapshot.get(k) is not None},
            risks=risks[:8],
            valuation_assumptions=assumptions,
            report_path=report_path,
            created_at=datetime.now().isoformat(timespec="seconds"),
            run_id=run_id,
        )
        return record
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"record_from_run failed: {exc!r}")
        return None
