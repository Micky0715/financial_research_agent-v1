"""Export existing reports to DOCX/PDF (v3 stage F).

Usage:
    python scripts/export_reports.py --latest          # newest report only
    python scripts/export_reports.py --run-id <id>     # one specific run
    python scripts/export_reports.py --all-eval        # every eval topic's latest run

DOCX always attempted (python-docx); PDF attempted via Edge headless from the
HTML report when one exists - failures degrade gracefully and are reported,
never raised.
"""
import argparse
import csv
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")

from config import config  # noqa: E402
from tools.report_exporter import export_docx, export_pdf  # noqa: E402
from utils.file_utils import load_json  # noqa: E402


def _report_files_for_run(run_id: str) -> dict:
    md = sorted(config.REPORTS_DIR.glob(f"{run_id}_*_report.md"))
    html = sorted(config.REPORTS_DIR.glob(f"{run_id}_*_report.html"))
    sources = sorted(config.SOURCES_DIR.glob(f"{run_id}_*_sources.json"))
    return {"md": md[0] if md else None, "html": html[0] if html else None,
            "sources": sources[0] if sources else None}


def _markdown_of(files: dict) -> str:
    if files["md"]:
        return files["md"].read_text(encoding="utf-8", errors="ignore")
    if files["html"]:
        import re
        text = files["html"].read_text(encoding="utf-8", errors="ignore")
        text = re.sub(r"<script[\s\S]*?</script>|<style[\s\S]*?</style>", "", text)
        # 保住二级标题结构，剩余标签剥掉
        text = re.sub(r"<h2[^>]*>", "\n## ", text)
        text = re.sub(r"<h3[^>]*>", "\n### ", text)
        text = re.sub(r"<li[^>]*>", "\n- ", text)
        return re.sub(r"<[^>]+>", "", text)
    return ""


def export_run(run_id: str) -> None:
    files = _report_files_for_run(run_id)
    markdown = _markdown_of(files)
    if not markdown:
        print(f"[warn] run {run_id}: no report file found, skipped")
        return
    topic_part = (files["md"] or files["html"]).stem.replace("_report", "")
    sources = []
    if files["sources"]:
        try:
            sources = load_json(files["sources"])
        except Exception as exc:  # noqa: BLE001
            print(f"[warn] run {run_id}: sources unreadable: {exc}")
    html_text = files["html"].read_text(encoding="utf-8", errors="ignore") if files["html"] else ""

    title = topic_part.split("_", 1)[-1] + " 研究报告"
    docx_result = export_docx(markdown, title, topic_part, sources, html_text)
    print(f"{run_id} DOCX: success={docx_result['success']} path={docx_result['path']} "
          f"error={docx_result['error']}")

    if files["html"]:
        pdf_result = export_pdf(str(files["html"]), topic_part)
        print(f"{run_id} PDF:  success={pdf_result['success']} degraded={pdf_result['degraded']} "
              f"path={pdf_result['path']} error={pdf_result['error']}")
    else:
        print(f"{run_id} PDF:  skipped (no HTML report for this run; markdown-only run)")


def main() -> None:
    parser = argparse.ArgumentParser(description="Export reports to DOCX/PDF")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--latest", action="store_true", help="export the most recent report")
    mode.add_argument("--run-id", type=str, help="export one run by run_id")
    mode.add_argument("--all-eval", action="store_true", help="export every eval topic's latest run")
    args = parser.parse_args()

    if args.run_id:
        export_run(args.run_id)
    elif args.latest:
        reports = sorted(config.REPORTS_DIR.glob("*_report.*"), key=lambda p: p.stat().st_mtime)
        if not reports:
            print("[error] no reports found")
            sys.exit(1)
        export_run(reports[-1].stem.split("_")[0])
    else:  # --all-eval
        csv_path = config.EVAL_DIR / "eval_summary.csv"
        if not csv_path.exists():
            print("[error] eval_summary.csv not found; run collect_eval_summary first")
            sys.exit(1)
        for row in csv.DictReader(open(csv_path, encoding="utf-8", newline="")):
            if row.get("run_id") and row["run_id"] != "N/A":
                export_run(row["run_id"])


if __name__ == "__main__":
    main()
