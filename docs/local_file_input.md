# 本地文件输入（v3 阶段G）

## 支持类型

PDF（pymupdf）/ TXT / Markdown / CSV（pandas）/ Excel（pandas+openpyxl，最多前 5 个 sheet）。

## 使用

```bash
python main.py --topic "比亚迪投资价值分析" --report_type company_research \
  --local-files "D:\docs\比亚迪年报.pdf,D:\docs\财务数据.xlsx" \
  --output_format html
```

## 行为

- 本地文件转换为标准 Source：`source_id` 从 **s901** 起（与网页来源区分、兼容 [sX] 引用），
  `source_type=local_file`，`authority_tier=local_user_file`；
- **不参与相关性竞争、不占网页来源的 top_k 名额**——用户提供的资料默认全部保留；
- 报告引用本地文件时与网页来源一样用 [s901] 形式，来源列表中明确标注"本地文件"；
- 财报类文件会尽量抽取年度营收/净利润序列（复用图表抽取器的全部守卫），抽取失败留空不编造；
- 单个文件不存在/类型不支持/解析失败：跳过 + warning，主流程照常。

## 边界

- PDF 抽取的是文本层，扫描版 PDF（纯图片）抽不出内容会被跳过；
- Excel/CSV 只做结构化预览（前 50 行）进入上下文，不做完整表格语义理解；
- 本地文件内容的真实性由使用者自行负责，系统按用户资料处理。
