#!/usr/bin/env bash
# Orcsy 原生 Linux 卸载脚本:停止并移除服务与程序目录。
# 数据目录与受管项目默认保留(可能有真实任务数据),需要时按提示手动清理。

set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
    echo "请用 root 运行:sudo bash $0" >&2
    exit 1
fi

echo "==> 停止并移除 systemd 服务"
systemctl disable --now orcsy.service 2>/dev/null || true
rm -f /etc/systemd/system/orcsy.service
systemctl daemon-reload

echo "==> 删除程序目录 /opt/orcsy"
rm -rf /opt/orcsy

echo "==> 已保留以下内容,如需彻底清理请手动执行:"
echo "    rm -rf /var/lib/orcsy     # 数据目录(config.yaml / skills / 日志)"
echo "    rm -rf /srv/orcsy-projects # 受管项目(可能有真实任务数据)"
echo "    userdel orcsy             # 系统用户(确认上面两项已不需要后)"
