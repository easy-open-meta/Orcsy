#!/usr/bin/env bash
# Orcsy 原生 Linux 安装脚本(系统级 systemd 服务)。
#
# 用法:在仓库根目录执行  sudo bash deploy/linux/install.sh
#
# 布局:程序 /opt/orcsy · 数据(config/skills/logs)/var/lib/orcsy · 受管项目 /srv/orcsy-projects
# 可用环境变量覆盖 git 提交身份:ORCSY_GIT_NAME / ORCSY_GIT_EMAIL
# 重复执行安全:已有用户/目录跳过,程序文件与 unit 覆盖为当前源码。

set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
    echo "请用 root 运行:sudo bash $0" >&2
    exit 1
fi

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
APP_DIR=/opt/orcsy
DATA_DIR=/var/lib/orcsy
PROJECTS_DIR=/srv/orcsy-projects
SERVICE_USER=orcsy
SERVICE_FILE=/etc/systemd/system/orcsy.service

echo "==> 检查依赖"
command -v git >/dev/null || { echo "缺少 git:apt install git 或 dnf install git"; exit 1; }
if ! command -v uv >/dev/null; then
    echo "==> 未找到 uv,尝试安装"
    if command -v python3 >/dev/null && python3 -m pip --version >/dev/null 2>&1; then
        python3 -m pip install --quiet uv || curl -LsSf https://astral.sh/uv/install.sh | sh
    else
        curl -LsSf https://astral.sh/uv/install.sh | sh
    fi
    export PATH="$HOME/.local/bin:$PATH" # astral.sh 安装器的默认位置
fi
command -v uv >/dev/null || { echo "uv 安装失败,请手动安装后重试"; exit 1; }

echo "==> 同步程序到 $APP_DIR"
mkdir -p "$APP_DIR"
cp -r "$REPO_DIR/orcsy" "$APP_DIR/orcsy"
find "$APP_DIR/orcsy" -type d -name '__pycache__' -exec rm -rf {} + 2>/dev/null || true
cp "$REPO_DIR/pyproject.toml" "$APP_DIR/"
LOCK_ARGS="--frozen"
if [ -f "$REPO_DIR/uv.lock" ]; then
    cp "$REPO_DIR/uv.lock" "$APP_DIR/"
else
    LOCK_ARGS="" # 无锁文件(如全新 clone 未提交),退化为按 pyproject 解析
fi
( cd "$APP_DIR" && uv sync $LOCK_ARGS --no-dev )

echo "==> 创建系统用户与目录"
mkdir -p "$DATA_DIR" "$PROJECTS_DIR"
id -u "$SERVICE_USER" >/dev/null 2>&1 \
    || useradd -r -M -d "$DATA_DIR" -s /usr/sbin/nologin "$SERVICE_USER"
chown -R "$SERVICE_USER:$SERVICE_USER" "$APP_DIR" "$DATA_DIR" "$PROJECTS_DIR"

echo "==> 初始化 git 环境($SERVICE_USER 用户)"
GITCONFIG="$DATA_DIR/.gitconfig"
touch "$GITCONFIG"
git config --file "$GITCONFIG" --replace-all safe.directory '*' 2>/dev/null || true
[ -n "$(git config --file "$GITCONFIG" --get user.name || true)" ] \
    || git config --file "$GITCONFIG" user.name "${ORCSY_GIT_NAME:-Orcsy Agent}"
[ -n "$(git config --file "$GITCONFIG" --get user.email || true)" ] \
    || git config --file "$GITCONFIG" user.email "${ORCSY_GIT_EMAIL:-orcsy@localhost}"
chown "$SERVICE_USER:$SERVICE_USER" "$GITCONFIG"

echo "==> 安装并启动 systemd 服务"
install -m 644 "$REPO_DIR/deploy/linux/orcsy.service" "$SERVICE_FILE"
systemctl daemon-reload
systemctl enable --now orcsy.service
sleep 2
systemctl --no-pager --lines 0 status orcsy.service || true

IP=$(hostname -I 2>/dev/null | awk '{print $1}')
echo "==> 完成:看板 http://${IP:-127.0.0.1}:5116"
echo "    首次使用在看板「项目与设置」页填写 llm.api_key;受管项目放 $PROJECTS_DIR"
echo "    服务管理:systemctl status|restart|stop orcsy;日志:journalctl -u orcsy -f"
echo "    提醒:看板无鉴权,建议用防火墙限制 5116 来源,或套带认证的反向代理。"
