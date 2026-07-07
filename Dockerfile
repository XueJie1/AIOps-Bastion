# AIOps-Bastion 主应用镜像
# 单进程架构 [决策#3], 跑 FastAPI + Agent + MCP Server
FROM python:3.12-slim

# 禁用 core dump (防凭证内存被转储) [评审补充#R6]
RUN ulimit -c 0

WORKDIR /app

# 系统依赖 (asyncssh 需要 libffi, cryptography 需要 build deps)
RUN apt-get update && apt-get install -y --no-install-recommends \
    libffi-dev \
    && rm -rf /var/lib/apt/lists/*

# 先装依赖 (利用 layer cache)
COPY pyproject.toml .
RUN pip install --no-cache-dir -e ".[dev]"

# 拷贝源码
COPY src/ src/
COPY tests/ tests/

# 运行用户 (非 root, 进程隔离 [评审补充#R6])
RUN useradd -m -u 1000 bastion
USER bastion

# 健康检查 + 默认入口 (M1 阶段先跑测试; M4 起改为 uvicorn)
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD python -c "import aiops_bastion" || exit 1
CMD ["python", "-m", "pytest", "tests/", "-q"]
