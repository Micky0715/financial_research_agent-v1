# DOCX / PDF 报告导出（v3 阶段F）

## 能力

- **DOCX**（python-docx）：标题、章节（##/### 映射为 Word 标题层级）、列表、Markdown 表格、
  HTML 报告内嵌的 base64 图表、来源列表（含 authority_tier 标注）、估值假设说明、免责声明。
- **PDF**：从 HTML 报告用本机 **Edge headless** 打印（Windows 自带，零额外依赖）。

## 降级策略

| 失败情形 | 行为 |
|---|---|
| python-docx 未安装 | 返回 `{success: false, degraded: true}`，不抛异常 |
| 本机没有 msedge.exe / 打印失败 | PDF 降级，只产出 DOCX/HTML，状态里写明原因 |
| 该 run 只有 Markdown 报告（无 HTML） | PDF 跳过并说明；DOCX 照常 |
| 单张图表解码失败 | 跳过该图，导出继续 |

导出是**离线后处理**（scripts/export_reports.py），不在主链路里，任何失败都不影响报告生成。

## 使用

```bash
python scripts/export_reports.py --latest          # 最新一份报告
python scripts/export_reports.py --run-id <run_id> # 指定 run
python scripts/export_reports.py --all-eval        # 评测集全部 topic 的最新 run
```

输出：`outputs/final_reports/docx/`、`outputs/final_reports/pdf/`（默认不提交入库）。

## 说明

导出的 DOCX/PDF 与 Markdown/HTML 报告内容一致，仅供阅读分发；报告内容不构成投资建议。
