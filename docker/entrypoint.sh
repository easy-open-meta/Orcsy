#!/bin/sh
# Orcsy 容器入口:修挂载卷属主 → 降权到 orcsy → 初始化容器内 git 环境 → exec 主进程。
#
# 背景:
# - git_ops.py 裸调 `git commit`(不传 -c 身份参数),容器里若无 user.name/email 提交会失败;
# - 挂载进来的宿主仓库属主 uid 与容器应用用户不一致时,git 会报 "dubious ownership" 拒绝操作;
# - 宿主 bind mount 常由 root 创建,应用用户写不了 → root 启动时先 chown 再降权。

set -eu

if [ "$(id -u)" = "0" ]; then
    # root 启动:修正挂载卷属主后,以 orcsy 用户重跑本脚本(HOME 指向其家目录,
    # 否则下面的 git config --global 会写进 /root)
    chown -R orcsy:orcsy /data /projects 2>/dev/null || true
    exec gosu orcsy env HOME=/home/orcsy "$0" "$@"
fi

# 挂载卷跨 uid:信任所有仓库目录(容器内数据盘即仓库宿主,风险可控)
git config --global --replace-all safe.directory '*' 2>/dev/null || true

# 提交身份:未配置时用环境变量或内置默认
if [ -z "$(git config --global --get user.name || true)" ]; then
    git config --global user.name "${ORCSY_GIT_NAME:-Orcsy Agent}"
fi
if [ -z "$(git config --global --get user.email || true)" ]; then
    git config --global user.email "${ORCSY_GIT_EMAIL:-orcsy@localhost}"
fi

# 命令覆盖走 CLI 子命令时(docker compose run orcsy run --once)自动补上 orcsy 前缀,
# 其余(自定义可执行文件、shell)原样 exec
case "${1:-}" in
    web|run|init|validate) set -- orcsy "$@" ;;
esac

exec "$@"
