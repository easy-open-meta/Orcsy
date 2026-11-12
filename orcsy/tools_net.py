"""网络请求、HTML 提取、SSRF 安全防护与镜像源测速工具集。

包含：
- web_fetch：公网文档/网页抓取与 HTML 标签清洗，逐跳校验重定向防 SSRF；
- read_skill：读取预加载技能库全文；
- speedtest_mirrors：内置与自定义镜像源延迟测速。
"""

from __future__ import annotations

import ipaddress
import socket
import sys
from html.parser import HTMLParser
from typing import TYPE_CHECKING
from urllib.parse import urljoin, urlparse

from .logging_setup import get_logger
from .mirrors import CATEGORY_BY_KEY, CATEGORY_KEYS

if TYPE_CHECKING:
    from .tools import ToolContext

log = get_logger("tools.net")

MAX_SKILL_CHARS = 24000
MAX_WEB_FETCH_CHARS = 20000
MAX_WEB_FETCH_BYTES = 4_000_000

_REDIRECT_STATUSES = frozenset((301, 302, 303, 307, 308))
_MAX_REDIRECT_HOPS = 5


def _assert_public_url(url: str) -> None:
    """只允许 http/https，且解析出的所有 IP 都必须是公网地址。"""
    # 动态检测测试对 orcsy.tools._assert_public_url 的 monkeypatch
    tools_mod = sys.modules.get("orcsy.tools")
    if tools_mod is not None:
        patched = getattr(tools_mod, "_assert_public_url", None)
        if patched is not None and patched is not _assert_public_url:
            return patched(url)

    u = urlparse(url)
    if u.scheme not in ("http", "https"):
        raise ValueError(f"只支持 http/https（当前：{u.scheme or '空'}）")
    host = u.hostname
    if not host:
        raise ValueError("URL 缺少主机名")
    port = u.port or (443 if u.scheme == "https" else 80)
    try:
        infos = socket.getaddrinfo(host, port)
    except socket.gaierror as e:
        raise ValueError(f"无法解析主机名 {host}：{e}") from e
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if (ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_reserved or ip.is_multicast or ip.is_unspecified):
            raise ValueError(f"目标 {host} 解析到非公网地址（{ip}），已拒绝访问")


class _HTMLText(HTMLParser):
    """把 HTML 剥成可读正文：丢弃 script/style，块级标签转换行，压缩空白。"""

    _SKIP = {"script", "style", "noscript", "template", "svg", "head"}
    _BREAK = {"p", "div", "br", "li", "ul", "ol", "tr", "table", "section",
              "article", "h1", "h2", "h3", "h4", "h5", "h6", "pre", "blockquote"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag, attrs):
        if tag in self._SKIP:
            self._skip_depth += 1
        elif tag in self._BREAK:
            self._parts.append("\n")

    def handle_endtag(self, tag):
        if tag in self._SKIP and self._skip_depth > 0:
            self._skip_depth -= 1
        elif tag in self._BREAK:
            self._parts.append("\n")

    def handle_data(self, data):
        if self._skip_depth == 0 and data.strip():
            self._parts.append(data)

    def text(self) -> str:
        import re
        raw = "".join(self._parts)
        raw = re.sub(r"[ \t\r\f\v]+", " ", raw)
        raw = re.sub(r" ?\n ?", "\n", raw)
        raw = re.sub(r"\n{3,}", "\n\n", raw)
        return raw.strip()


def _html_to_text(html: str) -> str:
    parser = _HTMLText()
    try:
        parser.feed(html)
        parser.close()
    except Exception:  # noqa: BLE001
        pass
    return parser.text()


class _WebFetchReject(Exception):
    """web_fetch 的业务性拒绝（HTTP 状态/内容类型），消息直接回给模型。"""


def _fetch_with_guard(client, url: str) -> tuple[int, str, bytearray, str, str]:
    """手动重定向循环：每一跳都重过 SSRF 校验（含第 0 跳）。"""
    import httpx

    for _hop in range(_MAX_REDIRECT_HOPS):
        _assert_public_url(url)
        with client.stream("GET", url) as resp:
            if resp.status_code in _REDIRECT_STATUSES:
                loc = (resp.headers.get("location") or "").strip()
                if not loc:
                    raise httpx.HTTPError(
                        f"重定向缺少 Location（HTTP {resp.status_code}）")
                url = urljoin(url, loc)
                scheme = urlparse(url).scheme
                if scheme not in ("http", "https"):
                    raise httpx.HTTPError(f"重定向到不支持的协议 {scheme or '空'}")
                continue
            if resp.status_code >= 400:
                raise _WebFetchReject(f"HTTP {resp.status_code}（{url}）")
            ctype = (resp.headers.get("content-type") or "").split(";")[0].strip().lower()
            ok_types = ("text/", "application/json", "application/xml",
                        "application/xhtml+xml", "application/javascript")
            if ctype and not any(ctype.startswith(t) or ctype == t for t in ok_types):
                raise _WebFetchReject(
                    f"不支持的内容类型 {ctype}（只抓取文本/HTML/JSON 类资源）")
            buf = bytearray()
            for chunk in resp.iter_bytes(65536):
                buf.extend(chunk)
                if len(buf) >= MAX_WEB_FETCH_BYTES:
                    break
            return resp.status_code, ctype, buf, (resp.encoding or "utf-8"), url
    raise httpx.HTTPError(
        f"重定向超过 {_MAX_REDIRECT_HOPS} 跳，已中止（可能的重定向循环）")


def tool_read_skill(ctx: ToolContext, args: dict) -> str:
    """读取一个适用技能的 SKILL.md 全文（内容由执行器从技能库预载）。"""
    name = str(args.get("name", "")).strip()
    if not ctx.skills:
        return "错误：当前项目没有可用的技能"
    if name not in ctx.skills:
        return f"错误：技能 {name!r} 不存在或对当前项目不生效。可用技能：{', '.join(ctx.skills)}"
    body = ctx.skills[name]
    if len(body) > MAX_SKILL_CHARS:
        body = body[:MAX_SKILL_CHARS] + f"\n\n…（技能过长，已截断至前 {MAX_SKILL_CHARS} 字符）"
    return body


def tool_web_fetch(ctx: ToolContext, args: dict) -> str:
    """抓取一个公网 URL 的正文（HTML 剥标签 / 文本类原样），供查文档、读 issue 等。"""
    url = str(args.get("url", "")).strip()
    if not url:
        return "错误：url 不能为空"
    try:
        max_chars = max(1000, min(int(args.get("max_chars") or MAX_WEB_FETCH_CHARS), 100000))
    except (TypeError, ValueError):
        return "错误：max_chars 必须是整数"
    try:
        _assert_public_url(url)
    except ValueError as e:
        return f"错误：{e}"
    import httpx  # 延迟导入：不联网的进程不为它付启动成本
    try:
        # follow_redirects=False：重定向目标同样不可信，自动跟随等于绕过上面的
        # SSRF 硬边界（恶意页面可 302 → 内网/元数据服务）。_fetch_with_guard
        # 手动循环，每一跳都重新过校验
        with httpx.Client(follow_redirects=False, timeout=20.0,
                          headers={"User-Agent": "Mozilla/5.0 (compatible; OrcsyAgent)"}) as client:
            status, ctype, buf, encoding, final_url = _fetch_with_guard(client, url)
    except httpx.HTTPError as e:
        return f"错误：请求失败：{e}"
    except (_WebFetchReject, ValueError) as e:
        return f"错误：{e}"
    text = bytes(buf).decode(encoding, errors="replace")
    if ctype in ("text/html", "application/xhtml+xml") or (
            not ctype and text.lstrip()[:1] == "<"):
        text = _html_to_text(text)
    if len(text) > max_chars:
        text = text[:max_chars] + f"\n\n…（正文过长，已截断至前 {max_chars} 字符）"
    return f"[HTTP {status} · {ctype or '未知类型'}]\n{text or '（空正文）'}"


def tool_speedtest_mirrors(ctx: ToolContext, args: dict) -> str:
    """测速镜像源（内置+用户添加），返回最快优先排序；结果落盘与看板共享。"""
    rt = ctx.mirror_runtime
    if rt is None:
        return ("错误：镜像源运行时不可用（配置来源未知）。"
                "请按 env-setup 技能文档的默认镜像顺序下载。")
    category = str(args.get("category") or "").strip() or None
    if category is not None and category not in CATEGORY_KEYS:
        return (f"错误：未知镜像类别 {category}"
                f"（可用：{', '.join(CATEGORY_KEYS)}；留空测全部）")
    results = rt.run_speedtest(category)
    if not results:
        return "没有可测速的镜像源（可能全部被禁用），按 env-setup 技能文档默认顺序处理。"
    ok = sum(1 for v in results.values() if v is not None)
    lines = [f"测速完成：{ok}/{len(results)} 个源可达（HTTP 直连探测，ms 越低越快）。"
             "各类别最快优先："]
    for cat in ([category] if category else CATEGORY_KEYS):
        parts = []
        for r in rt.preferred(cat):
            if r["url"] not in results:
                continue
            ms = results[r["url"]]
            parts.append(f"{r['url']} {ms:.0f}ms" if isinstance(ms, (int, float))
                         else f"{r['url']} 失败")
        if parts:
            lines.append(f"- {CATEGORY_BY_KEY[cat].label}：" + " | ".join(parts))
    lines.append("下载安装包/配置依赖代理（GOPROXY、pip、npm）时按上面顺序用最快的可达源；"
                 "结果已同步到看板设置页与 env-setup 技能的动态优选段。")
    return "\n".join(lines)


NET_TOOLS: dict[str, tuple[str, dict, object]] = {
    "read_skill": (
        "读取一个技能（Skill）的全文。系统提示词的技能索引里列出的技能适用时，"
        "先读取全文再严格按其内容执行；与任务无关的技能不要读取。",
        {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "技能名，见系统提示词的技能索引"},
            },
            "required": ["name"],
        },
        tool_read_skill,
    ),
    "web_fetch": (
        "抓取一个公网 URL 的正文文本（HTML 自动剥标签；文档/issue/参考资料等）。"
        "仅限公网 http/https，内网与本机地址会被拒绝；不要用它访问需要登录的页面。",
        {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "http/https 地址"},
                "max_chars": {"type": "integer",
                              "description": f"正文上限（默认 {MAX_WEB_FETCH_CHARS}，最大 100000）"},
            },
            "required": ["url"],
        },
        tool_web_fetch,
    ),
    "speedtest_mirrors": (
        "测速镜像源的 HTTP 延迟（内置+用户添加的语言安装包/依赖代理源），返回最快"
        "优先排序并持久化结果（与看板设置页共享）。下载安装包（Go/Node/Python/JDK）"
        "或配置依赖代理（GOPROXY/pip/npm）前调用它选最快的源；若 env-setup 技能正文"
        "已带「镜像源测速优选」段且时间较新，直接用即可、不必重测。",
        {
            "type": "object",
            "properties": {
                "category": {
                    "type": "string",
                    "enum": ["", *CATEGORY_KEYS],
                    "description": "只测某类别：go_dist=Go 安装包 / goproxy=Go 模块代理 / "
                                   "npm / pip / node_dist=Node 安装包 / jdk / "
                                   "python_dist=Python 安装包；留空 = 测全部",
                },
            },
            "required": [],
        },
        tool_speedtest_mirrors,
    ),
}

__all__ = [
    "MAX_SKILL_CHARS",
    "MAX_WEB_FETCH_BYTES",
    "MAX_WEB_FETCH_CHARS",
    "NET_TOOLS",
    "_HTMLText",
    "_MAX_REDIRECT_HOPS",
    "_REDIRECT_STATUSES",
    "_WebFetchReject",
    "_assert_public_url",
    "_fetch_with_guard",
    "_html_to_text",
    "tool_read_skill",
    "tool_speedtest_mirrors",
    "tool_web_fetch",
]
