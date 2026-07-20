# 部署指南（v4 阶段J：FastAPI + Docker）

## 1. 本地直接运行（不用 Docker）

```bash
pip install -r requirements.txt
cp .env.example .env   # 填 API Key
uvicorn api.main:app --host 0.0.0.0 --port 8000
```

访问：
- Swagger UI：http://localhost:8000/docs
- 健康检查：http://localhost:8000/health

## 2. API 一览

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/reports` | 提交一次报告生成任务，返回 `{task_id, status: "queued"}` |
| GET | `/tasks/{task_id}` | 查询任务状态：queued/running/succeeded/failed/degraded + progress/current_stage |
| GET | `/reports/{task_id}` | 任务未完成返回 202；完成后返回报告路径/导出路径/评估/来源摘要/warnings |
| GET | `/health` | 逐项检查 api/llm_provider/mcp/akshare/embedding/export_deps/memory |

`POST /reports` 请求体（`api/schemas.py::CreateReportRequest`）：

```json
{
  "topic": "贵州茅台投资价值分析",
  "report_type": "company_research",
  "requirements": ["公司概况", "财务分析", "估值分析", "风险提示"],
  "local_files": [],
  "output_format": "html",
  "max_sources": 5,
  "export_formats": ["docx"],
  "enable_memory": false,
  "enable_revision": true
}
```

## 3. 任务模型与已知限制

- **内存任务队列**：`api/task_manager.py::TaskManager` 是单进程内存实现，不做持久化、不做分布式。
  **服务重启后所有任务状态丢失**——这是当前明确声明的限制，不是缺陷；生产化需要
  外接 Redis/数据库任务表 + 消息队列，本项目范围内不实现。
- **并发任务数限制**：`API_MAX_CONCURRENT_TASKS`（默认 2），超过并发上限的任务在
  线程池里排队（`status=queued`），不会被拒绝或丢弃。
- **单任务超时**：`API_TASK_TIMEOUT_SECONDS`（默认 900 秒），超时后任务标记 `failed`，
  后台线程仍可能在跑（daemon 线程，进程退出时终止）——已知限制，非阻塞式取消。
- **API Key 只从环境变量读取**（`config.py` 经 `.env`/`docker run --env-file`），
  代码里不写死任何密钥。
- `status` 语义：
  - `succeeded`：正常完成，来源与评估齐全；
  - `degraded`：完成但有需要关注的情况（如 `unsupported_unlisted_company`、
    评估分偏低），报告仍可用，`warnings` 字段说明原因；
  - `failed`：未生成有效报告（如 `insufficient_entity_evidence`、来源为空、超时、内部异常）。

## 4. Docker

```bash
docker build -t financial-research-agent .
docker run --env-file .env -p 8000:8000 financial-research-agent
```

或：

```bash
docker compose up --build
```

- 镜像**不打包** `.env`、`outputs/` 下的运行产物、`memory/index/`（见 `.dockerignore`）；
  首次运行时这些目录由代码在容器内重新创建。
- **PDF 导出依赖宿主机 Edge 浏览器**（`tools/report_exporter.py` 用 `msedge --headless
  --print-to-pdf`）。标准 `python:3.11-slim` 镜像内没有 Edge，因此**容器内 PDF 导出会
  自动降级**（返回 `degraded=True`），DOCX/HTML/Markdown 不受影响。如需容器内 PDF，
  需要在 Dockerfile 里额外安装 Chromium/Edge（本项目未做此扩展，超出比赛范围）。
- 健康检查：`docker inspect --format='{{.State.Health.Status}}' <container>`，
  或直接访问 `http://localhost:8000/health`。
- **本次交付未在当前工作环境验证 `docker build`/`docker run`**（沙箱环境无 Docker
  守护进程）——Dockerfile/docker-compose.yml 已按标准写法审查，且服务本体已通过
  `uvicorn api.main:app` 本地真实启动 + `/health`、`/reports`、`/tasks` 端到端验证
  （见 docs/competition_alignment.md），Docker 层面仅完成静态编写，未做镜像构建验证，
  如实标注为待验证项。

## 5. 环境变量（节选，完整见 `.env.example`）

| 变量 | 说明 |
|---|---|
| `OPENAI_API_KEY` / `MODEL_NAME` / `OPENAI_API_BASE` | LLM 接入（LiteLLM 兼容），必须设置才能启用完整分析能力 |
| `API_MAX_CONCURRENT_TASKS` | 并发任务数上限，默认 2 |
| `API_TASK_TIMEOUT_SECONDS` | 单任务超时秒数，默认 900 |

## 6. 免责声明

本服务生成的报告基于免费公开数据，不构成投资建议，仅供技术研究参考；
不接入 Wind，不使用付费金融数据 API。
