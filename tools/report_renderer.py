"""Markdown assembly, Markdown->HTML rendering, and report persistence."""
from pathlib import Path

import markdown as md_lib
from jinja2 import Template

from schemas.source import Source
from utils.file_utils import sanitize_filename, save_text

_HTML_TEMPLATE = Template(
    """<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="UTF-8">
<title>{{ title }}</title>
<style>
  body { font-family: -apple-system, "Microsoft YaHei", "PingFang SC", sans-serif;
         max-width: 860px; margin: 40px auto; padding: 0 24px; color: #1a1a1a;
         line-height: 1.7; background: #fdfdfd; }
  h1 { font-size: 28px; border-bottom: 3px solid #1a56db; padding-bottom: 12px; }
  h2 { font-size: 22px; margin-top: 36px; color: #1a56db; border-left: 4px solid #1a56db;
       padding-left: 10px; }
  h3 { font-size: 18px; margin-top: 24px; }
  table { border-collapse: collapse; width: 100%; margin: 16px 0; }
  th, td { border: 1px solid #ddd; padding: 8px 12px; text-align: left; }
  th { background: #f0f4fd; }
  code { background: #f5f5f5; padding: 2px 5px; border-radius: 3px; }
  blockquote { border-left: 4px solid #f0ad4e; margin: 12px 0; padding: 4px 16px;
               background: #fff8ec; color: #6a4a12; }
  .risk-warning { background: #fff3f3; border-left: 4px solid #d9534f; padding: 12px 16px; }
  .footer-note { color: #888; font-size: 13px; margin-top: 48px; border-top: 1px solid #eee;
                 padding-top: 12px; }
</style>
</head>
<body>
{{ body }}
<div class="footer-note">本报告由金融研报多智能体系统自动生成，仅供参考，不构成投资建议。</div>
</body>
</html>
"""
)


def render_markdown_report(
    topic: str,
    sections: dict[str, str],
    sources: list[Source],
) -> str:
    """Assemble a full Markdown report from section text pieces plus a source list.

    Used as the deterministic fallback path when the LLM-produced report is
    unavailable or fails validation. `sections` keys map directly to headings.
    """
    order = [
        ("executive_summary", "1. 执行摘要"),
        ("overview", "2. 公司/行业概况"),
        ("key_trends", "3. 核心数据与趋势"),
        ("financial_analysis", "4. 财务表现或经营情况分析"),
        ("valuation_analysis", "5. 估值分析"),
        ("risk_analysis", "6. 风险提示"),
        ("investment_view", "7. 投资观点"),
    ]

    lines = [f"# {topic} 研究报告\n"]
    for key, heading in order:
        content = sections.get(key, "").strip() or "资料不足，暂无法给出结论。"
        lines.append(f"## {heading}\n\n{content}\n")

    lines.append("## 8. 参考来源\n")
    if not sources:
        lines.append("暂无有效参考来源。\n")
    else:
        for src in sources:
            lines.append(f"- [{src.source_id}] {src.title or '未命名来源'} - {src.url}")

    return "\n".join(lines)


def render_html_report(markdown_content: str, title: str, extra_html: str = "") -> str:
    """Convert a Markdown report string into a styled standalone HTML page.

    `extra_html` (e.g. the embedded financial trend chart from
    tools/chart_renderer.py) is appended after the report body, before the
    footer note.
    """
    body_html = md_lib.markdown(
        markdown_content, extensions=["tables", "fenced_code", "nl2br"]
    )
    if extra_html:
        body_html = f"{body_html}\n{extra_html}"
    return _HTML_TEMPLATE.render(title=title, body=body_html)


def save_report(
    content: str,
    output_format: str,
    run_id: str,
    topic: str,
    output_dir: Path,
) -> Path:
    """Persist the final report content to outputs/reports and return its path."""
    ext = "html" if output_format == "html" else "md"
    filename = f"{run_id}_{sanitize_filename(topic)}_report.{ext}"
    path = Path(output_dir) / filename
    save_text(path, content)
    return path
