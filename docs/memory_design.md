# 轻量长期知识库 / 向量记忆（v3 阶段I）

## 定位（先说清楚不是什么）

- **不是**生产级向量数据库（没有 Milvus/FAISS，就是 numpy 余弦 + 本地文件持久化）；
- **不是**长期用户记忆（不记用户偏好/对话历史）；
- **默认不参与主链路**——Agent 们完全不调用 memory，只有两个显式脚本会用它。

它是什么：把已有产物（报告摘要、评测结果、bad case 记录）建成可语义检索的本地索引，
方便"以前是不是研究过类似主题""这个问题以前踩过什么坑"这类查询。

## 存储对象

| kind | 来源 | metadata |
|---|---|---|
| report_summary | 每个 eval topic 最新报告的开头摘要（600 字符） | topic / report_type / run_id / quality_score / source_path / tags |
| bad_case | eval/bad_cases.md 按 "## Bad Case" 切分的每条记录 | topic（case 标题）/ source_path / tags |

## 实现与降级

- embedding：复用 tools/semantic_scorer 的 bge-small-zh-v1.5（本地 CPU）；
- 检索：numpy 余弦（记忆体量几十条，暴力检索就是最合理实现）；
- 持久化：`memory/index/records.jsonl` + `embeddings.npy`（gitignore，不入库）；
- **降级**：模型不可用时自动退回字符 bigram 重叠打分——检索照常工作只是变糙，不报错。

## 使用

```bash
python scripts/build_memory_index.py                      # 全量重建索引
python scripts/search_memory.py --query "比亚迪估值分析"
python scripts/search_memory.py --query "相关性误杀" --kind bad_case
```

实测：27 条记录（10 报告摘要 + 17 bad cases），"比亚迪估值分析"查询 top1 命中
比亚迪报告（0.823），bad case 检索能召回相关工程问题记录。
