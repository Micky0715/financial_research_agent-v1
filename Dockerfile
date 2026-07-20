# Financial Research Agent - FastAPI service image (v4 stage J)
#
# 说明：不打包 .env / outputs 大文件（见 .dockerignore）；API Key 通过
# `docker run --env-file .env` 在运行时注入，不写死在镜像里。PDF 导出依赖
# 宿主机 Edge，容器内默认不可用——DOCX/HTML/Markdown 正常，PDF 自动降级
# （tools/report_exporter.py 已处理，不报错）。

FROM python:3.11-slim

WORKDIR /app

# 系统依赖：pandas/matplotlib 等的少量原生依赖 + 中文字体（图表标题不乱码）
RUN apt-get update && apt-get install -y --no-install-recommends \
    fonts-noto-cjk \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# 不打包进镜像层的内容已经由 .dockerignore 排除；这里再兜底删一次，
# 防止本地误 COPY 进来的缓存/密钥泄漏到镜像里
RUN rm -rf outputs/cache outputs/reports outputs/traces outputs/sources \
    outputs/evaluations outputs/final_reports outputs/formal_reports \
    outputs/tracking memory/index .env 2>/dev/null || true

ENV PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app

EXPOSE 8000

# 健康检查依赖 /health 而不是 LLM 真实调用，容器编排层可用它判断就绪状态
HEALTHCHECK --interval=30s --timeout=10s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health', timeout=5)" || exit 1

CMD ["uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8000"]
