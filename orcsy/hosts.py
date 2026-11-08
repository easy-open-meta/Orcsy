"""本地调试网络安全：主机白名单匹配、DNS 解析后 IP 过滤与硬禁区拦截。

被测服务通常运行在回环地址（127.0.0.1/localhost），但项目级可追加局域网测试机。
本模块提供统一的主机安全审查逻辑（http_request 与浏览器顶层导航共用）：
1. 校验目标 host 是否在生效白名单内；
2. 域名解析后对所有关联 IP 进行安全判定，强制封杀链路本地/云元数据/广播/保留地址；
3. 防范 DNS 重绑定攻击。
"""

from __future__ import annotations

import ipaddress
import re
import socket
from urllib.parse import urlparse

# 硬禁区网络（任何配置均不可突破，即使人为写入白名单也强制拦截）
# - 169.254.0.0/16: IPv4 链路本地（包含各大云厂商元数据服务 169.254.169.254）
# - fe80::/10: IPv6 链路本地
# - 224.0.0.0/4, ff00::/8: 组播
# - 240.0.0.0/4: 保留地址
# - 0.0.0.0/8, ::/128: 未指定源地址
_FORBIDDEN_NETWORKS = (
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("fe80::/10"),
    ipaddress.ip_network("224.0.0.0/4"),
    ipaddress.ip_network("ff00::/8"),
    ipaddress.ip_network("240.0.0.0/4"),
    ipaddress.ip_network("0.0.0.0/8"),
)


def extract_host_and_port(url_or_target: str, default_port: int = 80) -> tuple[str, int]:
    """从 URL 或 host:port 字符串中提取小写 host 与端口号。"""
    raw = str(url_or_target or "").strip()
    if "://" in raw:
        parsed = urlparse(raw)
        host = parsed.hostname or ""
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        return host.strip().lower(), port
    # host[:port] 形态
    if raw.startswith("[") and "]" in raw:  # IPv6
        host = raw[1:raw.index("]")]
        rest = raw[raw.index("]") + 1:]
        port = int(rest[1:]) if rest.startswith(":") else default_port
        return host.strip().lower(), port
    if ":" in raw:
        parts = raw.split(":", 1)
        try:
            return parts[0].strip().lower(), int(parts[1])
        except ValueError:
            return parts[0].strip().lower(), default_port
    return raw.lower(), default_port


def is_ip_in_forbidden_ranges(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """判定单个 IP 是否属于系统绝对封杀的硬禁区。"""
    # 回环地址（127.0.0.1、::1 等）是合法的调试地址，不属于硬禁区
    if ip.is_loopback:
        return False
    if ip.is_link_local or ip.is_multicast or ip.is_reserved or ip.is_unspecified:
        return True
    for net in _FORBIDDEN_NETWORKS:
        if ip in net:
            return True
    return False


def normalize_host(h: str) -> str:
    """标准化主机名（去除两端空白与方括号，转小写）。"""
    h = h.strip().lower()
    if h.startswith("[") and h.endswith("]"):
        h = h[1:-1]
    return h


def check_host_allowed(host: str, allowed_hosts: list[str]) -> tuple[bool, str]:
    """判定 host 是否在允许列表中，并通过 IP 解析与硬禁区核验。

    返回 (True, "") 或 (False, 错误说明原因)。
    """
    clean_host = normalize_host(host)
    if not clean_host:
        return False, "主机名不能为空"

    # 标准化白名单
    allowed_set = {normalize_host(a) for a in allowed_hosts if a and a.strip()}

    if clean_host not in allowed_set:
        return False, f"目标主机 '{clean_host}' 不在允许的调试白名单中（允许：{', '.join(sorted(allowed_set))}）"

    # 若为 IP 字面量，直接做地址安全校验
    try:
        ip = ipaddress.ip_address(clean_host)
        if is_ip_in_forbidden_ranges(ip):
            return False, f"目标 IP '{clean_host}' 属于受保护的保留/元数据禁区地址，禁止访问"
        return True, ""
    except ValueError:
        pass  # 不是 IP 字面量，走 DNS 解析核验

    # 针对主机名做 DNS 解析，校验所有解析出的 IP
    try:
        # 端口仅用于 getaddrinfo 占位，设为 80
        addr_infos = socket.getaddrinfo(clean_host, 80, proto=socket.IPPROTO_TCP)
    except socket.gaierror as e:
        return False, f"无法解析主机名 '{clean_host}'：{e}"

    if not addr_infos:
        return False, f"主机名 '{clean_host}' 未解析到任何 IP 地址"

    for info in addr_infos:
        ip_str = info[4][0]
        try:
            ip_obj = ipaddress.ip_address(ip_str)
        except ValueError:
            continue

        if is_ip_in_forbidden_ranges(ip_obj):
            return False, f"主机 '{clean_host}' 解析到的 IP '{ip_str}' 属于系统硬禁区（链路本地/元数据），已拒绝"

        # 如果白名单条目本身是 localhost，则要求解析出来的全部都是回环地址，防止外部 DNS 投毒重绑定
        if clean_host == "localhost" and not ip_obj.is_loopback:
            return False, f"主机名 'localhost' 解析到非回环地址 '{ip_str}'，疑似 DNS 重绑定攻击，已拒绝"

    return True, ""


def assert_host_allowed(host: str, allowed_hosts: list[str]) -> None:
    """判定目标 host 是否放行，若拒绝则抛出 ValueError。"""
    ok, reason = check_host_allowed(host, allowed_hosts)
    if not ok:
        raise ValueError(reason)
