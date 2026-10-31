# Orcsy — Agent 任务编排系统 容器镜像
#
# 构建:  docker build -t orcsy .
# 运行:  docker run -d -p 5116:5116 -v ./orcsy-data:/data -v ./projects:/projects orcsy
# 详见 README「Docker 部署」章节。

# ---------- 构建阶段:按 uv.lock 可复现安装依赖 ----------
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim AS builder

WORKDIR /app

# 先只拷依赖清单,利用层缓存:清单不变时依赖层直接命中
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project

# 再拷源码并安装 orcsy 本体;--no-editable 让包真正落进 site-packages——
# 运行阶段只拷 .venv,可编辑安装留下的源码路径引用在容器里是断的
COPY orcsy/ ./orcsy/
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-editable

# ---------- 运行阶段 ----------
FROM python:3.12-slim-bookworm

# git 为硬依赖(worktree / commit / merge / push);tini 作 PID 1 转发信号;
# gosu 供入口脚本 chown 挂载卷后降权到 orcsy 用户
RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates tini gosu \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /app/.venv /app/.venv
COPY docker/entrypoint.sh /usr/local/bin/orcsy-entrypoint
# 预建 orcsy 用户与数据目录(named volume 首次挂载会继承这里的属主);
# bind mount 由入口脚本在 root 启动时 chown——宿主目录属主不可预知
RUN chmod +x /usr/local/bin/orcsy-entrypoint \
    && useradd --uid 1000 --create-home orcsy \
    && mkdir -p /data /projects \
    && chown -R orcsy:orcsy /data /projects

# 容器默认以 root 启动:入口脚本 chown 挂载卷后经 gosu 降权,应用进程仍是非 root
WORKDIR /app

ENV PATH="/app/.venv/bin:$PATH" \
    VIRTUAL_ENV=/app/.venv \
    PYTHONUNBUFFERED=1 \
    ORCSY_DATA_DIR=/data

# /data = config.yaml + skills/ + logs/(日志相对路径锚定配置目录,自然落卷)
VOLUME /data
EXPOSE 5116

# /api/state 与 cmd_web 端口探测同款端点;slim 镜像无 curl/wget,用 venv 内置 python
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:5116/api/state', timeout=4)"]

ENTRYPOINT ["tini", "--", "orcsy-entrypoint"]
# 容器内长驻:看板 + 内置调度器。托盘/浏览器均关(容器无显示与桌面)。
# 一次性执行(供外部 cron):docker run --rm orcsy run --once
CMD ["orcsy", "web", "--host", "0.0.0.0", "--no-tray", "--no-browser"]
