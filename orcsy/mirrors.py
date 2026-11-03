"""镜像源管理：内置 + 用户添加的各生态下载/代理源，HTTP 测速后「哪个快用哪个」。

三条消费路径：
1. 看板设置页「镜像源管理与测速选源」卡片：添加/禁用源、一键测速、
   「应用最快源」把代理类（GOPROXY/PIP_INDEX_URL/npm_config_registry）最快源
   写进 env.vars；
2. Agent 端：speedtest_mirrors 工具（模型下载前可自测）+ env-setup 技能正文
   动态追加「测速优选」段（执行器预载技能正文后装饰）；
3. 测速结果持久化在边车 JSON（配置目录/mirror_speeds.json），看板与 Agent
   共享同一份；不写进 config.yaml（高频变动数据不污染用户手编的配置）。

测速用 **HTTP 探测而非 ICMP ping**：镜像站/CDN 普遍禁 ICMP，且 ICMP 延迟
反映不了真实下载性能。探测 = Range GET 前 1KB，测 DNS+建连+首字节耗时；
一律绕过系统代理直连——测的正是「无代理时镜像的直连可达性与速度」。
部分镜像对目录页有防盗链（如清华 pip /simple/ 根 403 但包页正常），所以
内置源可配专用 test_url；用户添加的源用其 URL 本身探测（UI 提示填可直接
访问的地址）。

本模块只依赖标准库（config.py 会 import 这里的校验函数与类别表，反向不成立）。
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .logging_setup import get_logger

log = get_logger("mirrors")

SPEED_FILE = "mirror_speeds.json"     # 边车结果文件（配置目录下）
SPEED_TEST_TIMEOUT = 5.0              # 单源探测超时（秒）
SPEED_TEST_WORKERS = 8                # 并发探测数
SPEED_PROBE_BYTES = 1024              # Range GET 取前 1KB 计延迟
SPEED_DRAIN_BYTES = 256 * 1024        # 计完延迟后有界读完剩余响应（防 RST 竞态，见下）


# ---- 类别与内置源 ----

@dataclass(frozen=True)
class MirrorCategory:
    """一类镜像源。kind=proxy 的类别参与「应用最快源」（写 env.vars）。"""

    key: str
    label: str
    kind: str                 # dist=安装包下载源 / proxy=生态依赖代理
    env_var: str = ""         # proxy 类写入 env.vars 的变量名
    env_suffix: str = ""      # 应用时追加的值后缀（如 GOPROXY 的 ",direct"）


CATEGORIES: tuple[MirrorCategory, ...] = (
    MirrorCategory("go_dist", "Go 工具链安装包", "dist"),
    MirrorCategory("goproxy", "Go 模块代理", "proxy", "GOPROXY", ",direct"),
    MirrorCategory("npm", "npm registry", "proxy", "npm_config_registry"),
    MirrorCategory("pip", "pip index", "proxy", "PIP_INDEX_URL"),
    MirrorCategory("node_dist", "Node.js 安装包", "dist"),
    MirrorCategory("jdk", "JDK（Temurin）", "dist"),
    MirrorCategory("python_dist", "Python 安装包", "dist"),
)
CATEGORY_KEYS: tuple[str, ...] = tuple(c.key for c in CATEGORIES)
CATEGORY_BY_KEY: dict[str, MirrorCategory] = {c.key: c for c in CATEGORIES}


@dataclass(frozen=True)
class BuiltinMirror:
    """内置镜像源。url 是应用/文档口径；test_url 空 = 用 url 本身探测。"""

    url: str
    label: str
    test_url: str = ""


# 内置源均经无代理实测（2026-11）：清华 pip 目录页 403 属防盗链，探测走包页；
# 官方源（go.dev/pypi.org/proxy.golang.org/nodejs.org/python.org）保留为候选
# ——直连慢/不可达正是测速要识别的，海外或有代理的网络下它们可能反而最快
BUILTIN_MIRRORS: dict[str, tuple[BuiltinMirror, ...]] = {
    "go_dist": (
        BuiltinMirror("https://golang.google.cn/dl/", "Go 官方中国镜像"),
        BuiltinMirror("https://mirrors.aliyun.com/golang/", "阿里云"),
        BuiltinMirror("https://go.dev/dl/", "Go 官网"),
    ),
    "goproxy": (
        BuiltinMirror("https://goproxy.cn", "七牛云（国内首选）"),
        BuiltinMirror("https://goproxy.io", "goproxy.io"),
        BuiltinMirror("https://proxy.golang.org", "Go 官方代理"),
    ),
    "npm": (
        BuiltinMirror("https://registry.npmmirror.com", "npmmirror（阿里）",
                      "https://registry.npmmirror.com/vue"),
        BuiltinMirror("https://registry.npmjs.org", "npm 官方",
                      "https://registry.npmjs.org/vue"),
    ),
    "pip": (
        BuiltinMirror("https://pypi.tuna.tsinghua.edu.cn/simple/", "清华 TUNA",
                      "https://pypi.tuna.tsinghua.edu.cn/simple/pip/"),
        BuiltinMirror("https://mirrors.aliyun.com/pypi/simple/", "阿里云"),
        BuiltinMirror("https://pypi.org/simple/", "PyPI 官方",
                      "https://pypi.org/simple/pip/"),
    ),
    "node_dist": (
        BuiltinMirror("https://npmmirror.com/mirrors/node/", "npmmirror（阿里）"),
        BuiltinMirror("https://mirrors.tuna.tsinghua.edu.cn/nodejs-release/", "清华 TUNA"),
        BuiltinMirror("https://nodejs.org/dist/", "Node.js 官网"),
    ),
    "jdk": (
        BuiltinMirror("https://mirrors.tuna.tsinghua.edu.cn/Adoptium/", "清华 TUNA（Adoptium）"),
        BuiltinMirror("https://api.adoptium.net/v3/info/available_releases", "Adoptium 官方"),
    ),
    "python_dist": (
        BuiltinMirror("https://mirrors.huaweicloud.com/python/", "华为云"),
        BuiltinMirror("https://npmmirror.com/mirrors/python/", "npmmirror（阿里）"),
        BuiltinMirror("https://www.python.org/ftp/python/", "Python 官网"),
    ),
}


# ---- 配置校验（config.py 调用；返回纯 dict，由 config 包成 MirrorsConfig）----

_URL_RE = re.compile(r"^https?://[^\s\"'<>]{1,300}$", re.IGNORECASE)


def _validate_url(value: object, context: str) -> str:
    url = str(value or "").strip()
    if not _URL_RE.fullmatch(url):
        raise ValueError(f"{context} 含非法镜像 URL：{url or '空'}"
                         "（须以 http:// 或 https:// 开头、不含空白，≤300 字符）")
    return url


def validate_mirrors(data: object, context: str = "mirrors") -> dict:
    """校验 mirrors 配置块：{custom: {类别: [URL]}, disabled: [URL]}。

    custom 的类别必须是已知类别；URL 去重保序。与内置源重复的 custom URL
    在展示层去重（这里保留，用户显式添加不报错）。返回清洗后的 dict。
    """
    if data is None:
        return {"custom": {}, "disabled": []}
    if not isinstance(data, dict):
        raise ValueError(f"{context} 必须是包含 custom/disabled 的配置块")
    custom_raw = data.get("custom") or {}
    if not isinstance(custom_raw, dict):
        raise ValueError(f"{context}.custom 必须是「类别: [URL]」的映射")
    custom: dict[str, list[str]] = {}
    for cat, urls in custom_raw.items():
        key = str(cat).strip()
        if key not in CATEGORY_KEYS:
            raise ValueError(f"{context}.custom 含未知类别：{key}"
                             f"（可用：{', '.join(CATEGORY_KEYS)}）")
        if not isinstance(urls, list):
            raise ValueError(f"{context}.custom.{key} 必须是 URL 列表")
        seen: set[str] = set()
        out: list[str] = []
        for u in urls:
            url = _validate_url(u, context=f"{context}.custom.{key}")
            if url not in seen:
                seen.add(url)
                out.append(url)
        if out:
            custom[key] = out
    disabled_raw = data.get("disabled") or []
    if not isinstance(disabled_raw, list):
        raise ValueError(f"{context}.disabled 必须是 URL 列表")
    disabled: list[str] = []
    seen_d: set[str] = set()
    for u in disabled_raw:
        url = _validate_url(u, context=f"{context}.disabled")
        if url not in seen_d:
            seen_d.add(url)
            disabled.append(url)
    return {"custom": custom, "disabled": disabled}


# ---- 测速引擎 ----

def speedtest_one(url: str, test_url: str = "",
                  timeout: float = SPEED_TEST_TIMEOUT) -> float | None:
    """HTTP 探测单个镜像：Range GET 前 1KB 计延迟；失败返回 None。

    绕过系统代理直连（ProxyHandler({})）——测的就是无代理时的真实可达性。

    两个 Windows 网络细节（实测踩坑）：
    - **有界 drain**：计完延迟后把剩余响应读完（上限 256KB）再关连接。带未读
      数据关闭会发 RST，回环/并发下迟到的 RST 会打到复用端口的下一条连接，
      把别的源误判成不可达（WinError 10054）。遵守 Range 的 CDN（206）drain
      为零成本；不遵守的（200 全量）读到上限即止，脏关闭由重试兜底；
    - **快速失败重试**：HTTP 明确拒绝（403 防盗链/404）不重试（结果确定）；
      超时类慢失败不重试（不翻倍等待）；其余 <1.5s 的失败（瞬时重置等，
      Windows 回环/弱网常见）重试两次，间隔 80ms。
    """
    target = test_url or url
    for attempt in range(3):
        req = urllib.request.Request(target, headers={
            "User-Agent": "Orcsy-Mirror-Speedtest/1.0",
            "Range": f"bytes=0-{SPEED_PROBE_BYTES - 1}",
        })
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        t0 = time.monotonic()
        try:
            with opener.open(req, timeout=timeout) as resp:
                if getattr(resp, "status", 200) >= 400:
                    return None
                resp.read(SPEED_PROBE_BYTES)
                ms = round((time.monotonic() - t0) * 1000, 1)  # 延迟只计到首 1KB
                drained = 0
                try:
                    while drained < SPEED_DRAIN_BYTES:
                        chunk = resp.read(65536)
                        if not chunk:
                            break
                        drained += len(chunk)
                except Exception:  # noqa: BLE001 — drain 失败不影响已计好的延迟
                    pass
            return ms
        except urllib.error.HTTPError as e:
            # 服务器明确拒绝（403 防盗链/404）：结果确定，不重试。
            # 读完错误响应体再关——带未读数据关闭同样会发 RST 污染后续探测
            try:
                e.read(SPEED_PROBE_BYTES)
            except Exception:  # noqa: BLE001
                pass
            try:
                e.close()
            except Exception:  # noqa: BLE001
                pass
            return None
        except Exception:  # noqa: BLE001 — 超时/断连/拒连等
            if attempt < 2 and time.monotonic() - t0 < 1.5:
                time.sleep(0.08)
                continue  # 快速失败可能是瞬时抖动：再试
            return None
    return None


def speedtest_many(entries: list[tuple[str, str]],
                   timeout: float = SPEED_TEST_TIMEOUT) -> dict[str, float | None]:
    """并发测速 [(url, test_url), ...] → {url: ms|None}（键是应用口径 URL）。"""
    uniq: dict[str, str] = {}
    for url, test_url in entries:
        uniq.setdefault(url, test_url)
    if not uniq:
        return {}
    out: dict[str, float | None] = {}
    workers = min(SPEED_TEST_WORKERS, len(uniq))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futs = {pool.submit(speedtest_one, url, turl, timeout): url
                for url, turl in uniq.items()}
        for fut in as_completed(futs):
            url = futs[fut]
            try:
                out[url] = fut.result()
            except Exception:  # noqa: BLE001
                out[url] = None
    return out


# ---- 运行时（看板与 Agent 共享的视图 + 测速 + 结果存取）----

class MirrorRuntime:
    """按配置构造的镜像源运行时。

    custom/disabled 是纯数据（不依赖 config 类型，避免循环 import）；
    speeds_path 为边车结果文件（None = 只读内置表，测速结果不落盘）。
    """

    def __init__(self, custom: dict[str, list[str]] | None = None,
                 disabled: list[str] | None = None,
                 speeds_path: Path | None = None) -> None:
        self.custom: dict[str, list[str]] = {k: list(v) for k, v in (custom or {}).items()}
        self.disabled: list[str] = list(disabled or [])
        self.speeds_path = speeds_path

    # -- 源清单 --

    def sources(self, category: str | None = None) -> dict[str, list[dict]]:
        """{类别: [源行]}，内置在前、用户添加在后；行含 url/label/builtin/enabled/test_url。"""
        cats = (category,) if category else CATEGORY_KEYS
        out: dict[str, list[dict]] = {}
        for cat in cats:
            rows: list[dict] = []
            builtin_urls: set[str] = set()
            for m in BUILTIN_MIRRORS.get(cat, ()):
                builtin_urls.add(m.url)
                rows.append({"url": m.url, "label": m.label, "builtin": True,
                             "enabled": m.url not in self.disabled,
                             "test_url": m.test_url or m.url})
            for u in self.custom.get(cat, []):
                if u in builtin_urls:
                    continue  # 与内置重复：展示层去重
                rows.append({"url": u, "label": "用户添加", "builtin": False,
                             "enabled": u not in self.disabled, "test_url": u})
            out[cat] = rows
        return out

    # -- 测速结果存取 --

    def load_speeds(self) -> dict:
        """读边车结果：{"tested_at": str, "results": {url: ms|None}}；无/坏文件按未测速。"""
        if not self.speeds_path or not self.speeds_path.is_file():
            return {"tested_at": "", "results": {}}
        try:
            data = json.loads(self.speeds_path.read_text(encoding="utf-8"))
            results = data.get("results")
            if not isinstance(results, dict):
                return {"tested_at": "", "results": {}}
            return {"tested_at": str(data.get("tested_at") or ""),
                    "results": {str(k): v for k, v in results.items()}}
        except (OSError, ValueError):
            log.warning("测速结果文件损坏，按未测速处理：%s", self.speeds_path)
            return {"tested_at": "", "results": {}}

    def save_speeds(self, results: dict[str, float | None]) -> dict:
        """合并写入边车（单类别测速不抹掉其它类别的旧结果），原子替换。"""
        merged = dict(self.load_speeds()["results"])
        merged.update(results)  # None 也覆盖旧值：以最新一次探测状态为准
        data = {"tested_at": datetime.now().isoformat(timespec="seconds"),
                "results": merged}
        if self.speeds_path is not None:
            tmp = ""
            try:
                self.speeds_path.parent.mkdir(parents=True, exist_ok=True)
                fd, tmp = tempfile.mkstemp(dir=str(self.speeds_path.parent),
                                           prefix=".mirror-speeds-")
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False)
                os.replace(tmp, self.speeds_path)
                tmp = ""
            except OSError as e:
                log.warning("测速结果落盘失败（仅本次内存有效）：%s", e)
            finally:
                if tmp:
                    try:
                        os.unlink(tmp)
                    except OSError:
                        pass
        return data

    # -- 测速与优选 --

    def run_speedtest(self, category: str | None = None) -> dict[str, float | None]:
        """测速全部（或单类别）启用源，结果存边车并返回 {url: ms|None}。"""
        entries: list[tuple[str, str]] = []
        for rows in self.sources(category).values():
            for r in rows:
                if r["enabled"]:
                    entries.append((r["url"], r["test_url"]))
        results = speedtest_many(entries)
        if results:
            self.save_speeds(results)
        return results

    def preferred(self, category: str) -> list[dict]:
        """按测速结果排序的启用源：测得最快 → 慢 → 未测（保持默认序）→ 探测失败。"""
        rows = [r for r in self.sources(category)[category] if r["enabled"]]
        results = self.load_speeds()["results"]

        def key(row: dict) -> tuple[int, float]:
            if row["url"] not in results:
                return (1, 0.0)          # 未测速：排中间，保持默认顺序
            ms = results[row["url"]]
            if isinstance(ms, (int, float)):
                return (0, float(ms))    # 测得可达：按延迟升序
            return (2, 0.0)              # 测得失败：垫底

        return sorted(rows, key=key)

    def apply_fastest(self) -> dict[str, str]:
        """代理类（kind=proxy）最快源 → env.vars 映射；没测速成功的类别不写。

        值 = url + env_suffix（GOPROXY 追加 ",direct"：镜像缺的模块回源官方）。
        """
        results = self.load_speeds()["results"]
        out: dict[str, str] = {}
        for c in CATEGORIES:
            if c.kind != "proxy" or not c.env_var:
                continue
            for row in self.preferred(c.key):
                ms = results.get(row["url"])
                if isinstance(ms, (int, float)):
                    out[c.env_var] = row["url"] + c.env_suffix
                    break
        return out


def mirror_runtime_for(config: object) -> MirrorRuntime:
    """从 AppConfig 构造 MirrorRuntime（边车文件与 config.yaml 同目录）。"""
    m = getattr(config, "mirrors", None)
    src = getattr(config, "source_path", None)
    speeds = Path(src).parent / SPEED_FILE if src else None
    return MirrorRuntime(getattr(m, "custom", None), getattr(m, "disabled", None),
                         speeds_path=speeds)


# ---- env-setup 技能正文动态装饰 ----

def decorate_skill_body(name: str, body: str,
                        runtime: MirrorRuntime | None) -> str:
    """给 env-setup 技能正文追加「测速优选」段（有测速结果时）；其它技能原样返回。

    执行器每个任务预载技能正文后调用——模型 read_skill("env-setup") 拿到的
    就是带最新测速排序的版本，不测速也有据可依。
    """
    if name != "env-setup" or runtime is None or not body:
        return body
    speeds = runtime.load_speeds()
    results = speeds.get("results") or {}
    if not results:
        return body
    lines: list[str] = []
    for c in CATEGORIES:
        ranked: list[str] = []
        for row in runtime.preferred(c.key):
            ms = results.get(row["url"])
            if isinstance(ms, (int, float)):
                ranked.append(f"{row['url']}（{ms:.0f}ms）")
            if len(ranked) >= 3:
                break
        if ranked:
            lines.append(f"- {c.label}：" + " → ".join(ranked))
    if not lines:
        return body
    head = (f"\n\n---\n\n## 镜像源测速优选（系统动态追加，测于 "
            f"{speeds.get('tested_at') or '未知时间'}）\n\n"
            "HTTP 直连实测延迟（ms，越低越快），下载/配代理时**按此顺序优先用第一个源**；"
            "用户添加的源若在列，通常是内网/公司镜像，优先信任：\n\n")
    tail = ("\n若首选源实际下载失败，按顺序换下一个；也可调用 speedtest_mirrors 工具重测"
            "（结果会同步到看板）。\n")
    return body + head + "\n".join(lines) + tail
