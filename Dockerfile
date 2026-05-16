# =========================================================
# agushare — A股 K线形态扫描 Agent
# =========================================================
FROM python:3.11-slim AS base

# 时区 + 中文字体 (K线图标题/注释需要)
RUN apt-get update && apt-get install -y --no-install-recommends \
        tzdata \
        fonts-noto-cjk \
        ca-certificates \
        curl \
    && rm -rf /var/lib/apt/lists/*
ENV TZ=Asia/Shanghai \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# 先拷贝 requirements 利用 Docker 层缓存
COPY ashare_agent/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

# 再拷代码
COPY ashare_agent /app/ashare_agent

# 运行时挂载的目录 (在 compose 里 mount)
RUN mkdir -p /app/cache /app/output/reports /app/output/charts /app/logs

# 容器内健康检查
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8000/healthz >/dev/null \
    || exit 1

EXPOSE 8000

# 默认起 Web 服务 (调度容器会用 command 覆盖)
CMD ["uvicorn", "ashare_agent.server:app", \
     "--host", "0.0.0.0", "--port", "8000", "--workers", "2"]
