"""程序自更新：Gitee Release 检查 / 下载 / 手动导入 / 静默换名安装。

更新包为 ZIP（orcsy-update-*.zip，含 orcsy-web.exe / orcsy-tray.exe / README*）。
安装目录用户可写时走零 UAC 静默升级：后台线程「换名落位」——解压到安装目录
暂存区，把运行中的 exe 改名 *.exe.old（Windows 允许对运行中的 PE 改名，绕开
镜像锁），新文件 os.replace 落位，再由看护脚本（detached、非提权）等旧进程
退出后清理 .old 并拉起新托盘，全程同一用户权限、不触发 UAC。
安装目录不可写时（如默认装进 C:\\Program Files）走提权兜底：解压到用户
temp，弹一次 UAC 由管理员复制脚本完成替换（失败自动回滚），托盘仍由普通
权限看护脚本拉起（新程序绝不运行在管理员态）；用户取消 UAC 则如实报错、
程序继续运行、可重试。安装日志逐步写入，升级结束若有错误自动打开日志文档，
无错误则不打开。Inno 安装包（orcsy-setup-*.exe）仅用于首次安装（允许 UAC）。
Docker / Linux / 源码运行只支持检查，应用时给出对应指引。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import traceback
import zipfile
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

from . import USER_AGENT
from .issues import detect_repo


class UpdateError(Exception):
    """自更新失败的领域错误（Web 层直接转 400 提示）。"""


def parse_version(text: str) -> tuple[int, ...]:
    """把 'V0.13.0' / '0.13' / 'v0.14.0.1' 解析为可比较的整数元组。

    非数字段丢弃；没有任何数字段时返回空元组（调用方视为未知版本，不比较）。
    """
    parts = []
    for seg in re.split(r"[.\-+ ]+", (text or "").strip().lstrip("Vv")):
        if seg.isdigit():
            parts.append(int(seg))
    return tuple(parts)


def version_newer(candidate: str, current: str) -> bool:
    """candidate 是否严格新于 current；任一无法解析时返回 False（宁可不更）。"""
    c, cur = parse_version(candidate), parse_version(current)
    if not c or not cur:
        return False
    width = max(len(c), len(cur))
    c += (0,) * (width - len(c))
    cur += (0,) * (width - len(cur))
    return c > cur


def check_update(update_cfg: dict[str, Any], current_version: str) -> dict[str, Any]:
    """查询 Gitee Release 最新版本，与当前版本比较。

    update_cfg: {repo: 'https://gitee.com/xxx/orcsy', token?: str}
    返回 {has_update, current, latest, notes, asset_name, download_url,
          sha256_url, sha256_value}。附件只认 orcsy-update-*.zip 更新包及
    同名 .sha256 校验文件（exe 安装包仅首次安装用，不走在线更新）。
    """
    info = detect_repo(str(update_cfg.get("repo") or ""), "gitee")
    token = str(update_cfg.get("token") or "").strip()
    params = {"access_token": token} if token else None
    api_base = str(update_cfg.get("api_base") or "").rstrip("/")
    url = f"{api_base or info['origin']}/api/v1/repos/{info['owner']}/{info['repo']}/releases/latest"

    last_err: Exception | None = None
    resp = None
    trust_env = urlparse(url).hostname not in ("127.0.0.1", "localhost", "::1")
    for attempt in range(5):  # 本机回环竞态重试（RST 成串出现，加深重试覆盖突发窗口）
        try:
            resp = httpx.get(url, params=params, timeout=30, trust_env=trust_env,
                             headers={"User-Agent": USER_AGENT})
            break
        except httpx.HTTPError as e:
            last_err = e
            if attempt < 4 and isinstance(e, httpx.TransportError):
                time.sleep(0.5 * (attempt + 1))
                continue
            raise UpdateError(f"检查更新失败：{e}") from e
    if resp is None:
        raise UpdateError(f"检查更新失败：{last_err}")
    if resp.status_code == 401:
        raise UpdateError("检查更新失败（401）：更新源为私有仓库，token 缺失或无效。")
    if resp.status_code == 404:
        raise UpdateError("检查更新失败（404）：更新仓库不存在或还没有发布过任何 Release。")
    if resp.status_code != 200:
        raise UpdateError(f"检查更新失败：HTTP {resp.status_code} {resp.text[:200]}")
    try:
        data = resp.json()
    except ValueError as e:
        raise UpdateError("检查更新失败：响应不是 JSON") from e

    latest = str(data.get("tag_name") or "").strip()
    if not latest:
        raise UpdateError("检查更新失败：Release 缺少 tag_name。")
    assets = [a for a in (data.get("assets") or []) if isinstance(a, dict)]
    zips = [a for a in assets if str(a.get("name", "")).lower().endswith(".zip")]
    update_zips = [a for a in zips
                   if str(a.get("name", "")).lower().startswith("orcsy-update")]
    asset = (update_zips or zips or [None])[0]
    if asset is None:
        raise UpdateError(f"Release {latest} 里没有找到 orcsy-update-*.zip 更新包附件"
                          "（在线更新用 zip 更新包，请随 Release 一并上传；"
                          "exe 安装包只用于首次安装）。")
    download_url = str(asset.get("browser_download_url") or "")
    if not download_url:
        raise UpdateError(f"Release {latest} 的附件缺少下载地址。")
    if token:
        sep = "&" if "?" in download_url else "?"
        download_url += f"{sep}access_token={token}"

    # 完整性校验来源（安全审计 2026-11-04 M2）：优先 Release 附件里的同名
    # .sha256 文件（build.bat 发布时成对产出）；GitHub 新版 API 的 asset 带
    # digest 字段时直接采信。两者皆无 = 无哈希，在线更新将被拒绝（宁可不更，
    # 不可无校验提权执行）
    result = {
        "has_update": version_newer(latest, current_version),
        "current": current_version,
        "latest": latest,
        "notes": str(data.get("body") or ""),
        "asset_name": str(asset.get("name") or ""),
        "download_url": download_url,
        "sha256_url": "",     # .sha256 附件的下载地址（空 = 无附件）
        "sha256_value": "",   # API digest 字段给的哈希值本身（空 = 无）
    }
    zip_name = str(asset.get("name") or "")
    sha_asset = next((a for a in assets
                      if str(a.get("name", "")).lower() == f"{zip_name}.sha256"), None)
    if sha_asset is not None:
        sha_url = str(sha_asset.get("browser_download_url") or "")
        if sha_url and token:
            sep = "&" if "?" in sha_url else "?"
            sha_url += f"{sep}access_token={token}"
        result["sha256_url"] = sha_url
    digest = str(asset.get("digest") or "")
    if digest.lower().startswith("sha256:"):
        result["sha256_value"] = digest.split(":", 1)[1].strip().lower()
    return result


def download_installer(download_url: str, dest_dir: Path,
                       progress_cb: Callable[[int], None] | None = None,
                       filename: str = "orcsy-update-download.zip",
                       expected_sha256: str = "") -> Path:
    """流式下载更新包到 dest_dir，progress_cb 收到 0~100 的百分比。

    expected_sha256 非空时下载完成后校验（安全审计 2026-11-04 M2）：不符则
    删除落盘文件并抛 UpdateError——哈希不符的包绝不能留在磁盘上进安装环节。
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / filename
    # 环回地址绕过系统代理（Shadowsocks/Clash 类工具会把 127.0.0.1 的下载
    # 送进隧道导致超时）；外部地址仍尊重系统代理设置
    trust_env = urlparse(download_url).hostname not in ("127.0.0.1", "localhost", "::1")
    for attempt in range(5):  # 本机回环 RST 成串出现，退避重试覆盖突发窗口
        try:
            with httpx.stream("GET", download_url, timeout=60, follow_redirects=True,
                              headers={"User-Agent": USER_AGENT}, trust_env=trust_env) as resp:
                if resp.status_code != 200:
                    raise UpdateError(f"下载更新包失败：HTTP {resp.status_code}")
                total = int(resp.headers.get("content-length") or 0)
                done = 0
                with open(dest, "wb") as f:
                    for chunk in resp.iter_bytes(chunk_size=1024 * 1024):
                        f.write(chunk)
                        done += len(chunk)
                        if progress_cb and total:
                            progress_cb(min(100, done * 100 // total))
            break
        except httpx.HTTPError as e:
            if attempt < 4 and isinstance(e, httpx.TransportError):
                time.sleep(0.5 * (attempt + 1))
                continue
            raise UpdateError(f"下载更新包失败：{e}") from e
    if not dest.exists() or dest.stat().st_size == 0:
        raise UpdateError("下载更新包失败：文件为空。")
    if expected_sha256:
        h = hashlib.sha256()
        with open(dest, "rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                h.update(chunk)
        actual = h.hexdigest()
        if not hmac.compare_digest(actual, expected_sha256.strip().lower()):
            dest.unlink(missing_ok=True)  # 坏包不留在磁盘上
            raise UpdateError(
                f"更新包校验失败（sha256 不符）：期望 {expected_sha256.strip()[:12]}…，"
                f"实际 {actual[:12]}…。可能下载损坏或来源被篡改，请重试或从官方"
                "仓库手动下载。")
    return dest


def parse_sha256_file(path: Path) -> str:
    """解析 .sha256 校验文件：sha256sum 标准格式「<hex>  <文件名>」，取首个字段。

    返回小写十六进制哈希；内容无法识别（非 64 位十六进制）抛 UpdateError。
    """
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        raise UpdateError(f"读取校验文件失败：{e}") from e
    value = text.strip().split()[0].strip().lower() if text.strip() else ""
    if not re.fullmatch(r"[0-9a-f]{64}", value):
        raise UpdateError(f"校验文件内容无法识别（期望 sha256 十六进制哈希）：{text[:80]!r}")
    return value


def fetch_expected_sha256(sha256_url: str, dest_dir: Path) -> str:
    """下载并解析 .sha256 校验文件，返回期望的 sha256 十六进制值。

    下载或解析失败抛 UpdateError——调用方应中止在线更新（缺校验不装）。
    """
    dest = download_installer(sha256_url, dest_dir, filename="checksum.sha256")
    return parse_sha256_file(dest)


def prune_stale_download_dirs(keep_seconds: float = 86400.0) -> None:
    """清理历史在线更新下载残留的 orcsy-update-* 临时目录（保留 1 天内的）。

    更新包现落在随机名私有目录（mkdtemp），成功路径进程直接退出、无人清理；
    每次在线更新前顺手扫一遍 temp，超龄的删掉，避免 zip 体积的残留越积越多。
    只动目录（orcsy-update-result.json 等文件不受影响），失败静默跳过。
    """
    tmp = Path(tempfile.gettempdir())
    now = time.time()
    for d in tmp.glob("orcsy-update-*"):
        try:
            if d.is_dir() and now - d.stat().st_mtime > keep_seconds:
                shutil.rmtree(d, ignore_errors=True)
        except OSError:
            continue


def detect_installer_version(filename: str) -> str | None:
    """从更新包文件名解析版本号（orcsy-update-0.14.0.zip → 0.14.0）；解析失败返回 None。"""
    m = re.search(r"(\d+\.\d+(?:\.\d+){0,2})", filename or "")
    return m.group(1) if m else None


def can_apply() -> bool:
    """应用更新仅支持 Windows 安装版（frozen 的 orcsy-web/orcsy-tray exe）。"""
    exe = Path(sys.executable).name.lower()
    return (sys.platform == "win32" and getattr(sys, "frozen", False)
            and exe in ("orcsy-web.exe", "orcsy-tray.exe"))


def apply_gate_message() -> str:
    if sys.platform != "win32":
        return "当前为 Docker / Linux 部署：请拉取新镜像或按对应部署方式更新，程序内自更新仅支持 Windows 安装版。"
    if not getattr(sys, "frozen", False):
        return "当前为源码运行：请在仓库执行 git pull 并重新同步依赖。"
    return "当前运行环境不支持程序内自更新。"


# ---- 安装日志：全程留痕；仅出错时自动打开（成功路径绝不打开） ----

def install_log_path(config: Any = None) -> Path:
    """安装日志路径：<配置目录>/logs/install.log（与 orcsy.log 同目录，便于查找）。

    无配置时退回 %TEMP%\\orcsy-setup-install.log（自检/测试用兜底）。
    """
    src = getattr(config, "source_path", None) if config is not None else None
    if src:
        return Path(src).parent / "logs" / "install.log"
    return Path(tempfile.gettempdir()) / "orcsy-setup-install.log"


def append_install_log(log: Path, text: str) -> None:
    """往安装日志追加一行（带时间戳）。日志写失败不掩盖主错误——调用方仍会弹窗。

    编码约定：整份日志统一 UTF-8（新建时带 BOM，Notepad/VS Code 据此识别）。
    bat 脚本只写纯 ASCII 行（时间戳用 powershell 的 ASCII 格式，不用 %date%——
    中文系统下它带「周一」字样、按控制台 GBK 码页落盘，与 UTF-8 行混编后
    任何编辑器都只能正确显示一半）。历史混编码旧日志由 _heal_install_log
    改名归档后重开新文件。
    """
    try:
        log = Path(log)
        log.parent.mkdir(parents=True, exist_ok=True)
        _heal_install_log(log)
        fresh = not log.exists() or log.stat().st_size == 0
        with open(log, "a", encoding="utf-8-sig" if fresh else "utf-8") as f:
            f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {text}\n")
    except OSError:
        pass


# 每个路径每进程只体检一次，避免每次追加都读全文件
_HEALED_LOGS: set[str] = set()


def _heal_install_log(log: Path) -> None:
    """旧日志若是混编码（GBK 的 bat 行 + UTF-8 的 Python 行），改名归档重开。

    混编码文件不存在「正确打开方式」：Notepad 按 ANSI 读则 UTF-8 行乱码，
    按 UTF-8 读则 GBK 行乱码。归档名保留 .legacy-gbk 后缀供人工翻查。
    """
    key = str(log)
    if key in _HEALED_LOGS:
        return
    _HEALED_LOGS.add(key)
    try:
        if not log.exists() or log.stat().st_size == 0:
            return
        raw = log.read_bytes()
        if raw.startswith(b"\xef\xbb\xbf"):
            raw = raw[3:]
        raw.decode("utf-8")
    except UnicodeDecodeError:
        try:
            log.replace(log.with_name(log.name + ".legacy-gbk"))
        except OSError:
            pass
    except OSError:
        pass


def read_install_log(log: Path | None = None, max_chars: int = 4000) -> str:
    """读取安装日志的尾部（诊断静默安装失败用）。utf-8-sig 兼容带 BOM 的新日志。"""
    log = Path(log) if log else install_log_path()
    try:
        return log.read_text(encoding="utf-8-sig", errors="replace")[-max_chars:]
    except OSError:
        return ""


def open_install_log(log: Path) -> None:
    """自动打开安装日志文档——仅供错误路径调用（成功升级不打开、不打扰用户）。"""
    log = Path(log)
    if not log.exists():
        return
    try:
        if sys.platform == "win32":
            os.startfile(log)  # noqa: S606 — 本机用户查看自己的安装日志
        else:
            opener = "open" if sys.platform == "darwin" else "xdg-open"
            subprocess.Popen([opener, str(log)])
    except OSError:
        pass


# ---- 更新结果标记：看护脚本写、新进程启动消费（错误弹窗+开日志，成功静默） ----

def update_result_path() -> Path:
    return Path(tempfile.gettempdir()) / "orcsy-update-result.json"


def write_update_result(res: dict) -> None:
    try:
        update_result_path().write_text(json.dumps(res, ensure_ascii=False),
                                        encoding="utf-8")
    except OSError:
        pass


def read_update_result() -> dict | None:
    try:
        data = json.loads(update_result_path().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def clear_update_result() -> None:
    try:
        update_result_path().unlink()
    except OSError:
        pass


# 看护脚本写入 result 的 reason 码 → 中文说明（新进程转述给用户/安装日志）
RESULT_REASONS = {
    "ok": "升级成功",
    "install-interrupted": "升级在文件替换完成前被中断，已尽力回滚；请重新安装更新包",
    "interrupted": "升级在文件替换完成前被中断，已回滚残留文件；请重新安装更新包",
    "partial-swap-rolled-back": "升级中途被中断，部分文件已回滚为旧版；请重新安装更新包",
    "tray-missing": "升级后未找到 orcsy-tray.exe，无法自动重启；请检查安装目录后手动启动",
    "tray-died-on-start": "新版程序启动后立即退出；可能更新包与当前系统不兼容，详见安装日志",
    "old-process-lingering": "旧进程未在预期时间内退出，升级未完成；请关闭程序后重新安装",
    "elevated-copy-timeout": "提权复制未在预期时间内完成，已按旧版重新启动；请重试更新或查看安装日志",
    "elevated-copy-failed": "提权复制文件失败（已回滚为旧版并重新启动）；详见安装日志",
}


def result_message(reason: str) -> str:
    return RESULT_REASONS.get(str(reason or ""), f"升级未完成（{reason or '未知原因'}）")


# ---- 更新包校验与落位（零 UAC 核心：纯 Python 解压 + 运行中 PE 改名） ----

_PACKAGE_EXE_NAMES = ("orcsy-web.exe", "orcsy-tray.exe")
_STAGE_DIR_NAME = ".orcsy-staging"
_SWAP_FLAG_NAME = ".orcsy-swapped"


def validate_package(path: Path | str) -> dict:
    """校验 ZIP 更新包：存在、可解压、成员白名单、无路径穿越（zip-slip）。

    返回 {"version": str|None, "members": [文件名]}。
    同版本/不同版本（升/降级）均允许，版本仅供确认展示，不拦截。
    """
    pkg = Path(path)
    if not pkg.is_file():
        raise UpdateError(f"更新包不存在：{pkg}")
    if pkg.suffix.lower() != ".zip":
        raise UpdateError("更新包格式不对：请选择 orcsy-update-*.zip"
                          "（构建产出的 zip 更新包；exe 安装包只用于首次安装）")
    try:
        with zipfile.ZipFile(pkg) as zf:
            bad = zf.testzip()
            if bad is not None:
                raise UpdateError(f"更新包已损坏（成员校验失败：{bad}）")
            names = zf.namelist()
    except zipfile.BadZipFile as e:
        raise UpdateError(f"更新包无法解压（不是有效的 zip 文件）：{e}") from e

    members: list[str] = []
    for n in names:
        if n.endswith("/"):
            continue  # 目录项
        # zip-slip：成员名必须是纯文件名（构建产出的包是扁平结构）
        if "/" in n or "\\" in n or ":" in n or n.startswith("."):
            raise UpdateError(f"更新包包含非法路径成员：{n}")
        if n in _PACKAGE_EXE_NAMES or n.upper().startswith("README"):
            members.append(n)
        else:
            raise UpdateError(f"更新包包含不允许的文件：{n}"
                              "（只允许 orcsy-web.exe / orcsy-tray.exe / README*）")
    missing = [e for e in _PACKAGE_EXE_NAMES if e not in members]
    if missing:
        raise UpdateError(f"更新包缺少必需文件：{', '.join(missing)}")
    return {"version": detect_installer_version(pkg.name), "members": members}


def is_writable(app_dir: Path | str) -> bool:
    """探测安装目录当前用户是否可写（写探针文件即删）；不抛错。

    run_local_install 用它分流：可写 → 零 UAC 换名落位；不可写（如
    Program Files 默认权限）→ 提权兜底（一次 UAC）。
    """
    app_dir = Path(app_dir)
    probe = app_dir / f".orcsy-write-probe-{os.getpid()}"
    try:
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return True
    except OSError:
        return False


def check_writable(app_dir: Path) -> None:
    """安装目录可写性预检：换名落位需要当前用户写权限（零 UAC 方案的前提）。

    不可写（如 Program Files 默认权限）时明确报错并指引迁移目录——
    不尝试提权，也就不会出现 UAC 窗口。
    注：run_local_install 现改用 is_writable 分流（不可写走提权兜底），
    本函数保留给需要硬失败语义的调用方/测试。
    """
    app_dir = Path(app_dir)
    probe = app_dir / f".orcsy-write-probe-{os.getpid()}"
    try:
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except OSError as e:
        raise UpdateError(
            f"安装目录不可写：{app_dir}（{e}）。静默升级（零 UAC）要求当前用户可写"
            "安装目录，请把程序迁移到用户可写位置（如 %LOCALAPPDATA%\\Orcsy"
            "或其它非系统目录）后重试。") from e


def stage_package(package: Path | str, app_dir: Path | str,
                  log: Path | str) -> list[str]:
    """解压更新包到安装目录暂存区并换名落位（装回原目录）。

    运行中的 exe 先改名 *.exe.old（Windows 允许对运行中 PE 改名，绕开镜像锁），
    新文件随即 os.replace 落位；.old 由看护脚本等旧进程退出后清理（失败时回滚）。
    每步写安装日志；任何失败抛 UpdateError（日志含 traceback，绝不静默吞错）。
    """
    package, app_dir, log = Path(package), Path(app_dir), Path(log)
    info = validate_package(package)  # 防御性复检：即使调用方没校验也拦下脏包
    staging = app_dir / _STAGE_DIR_NAME
    replaced: list[str] = []
    try:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
        staging.mkdir(parents=True)
        append_install_log(log, f"解压更新包 {package.name} → 暂存区 {_STAGE_DIR_NAME}/")
        with zipfile.ZipFile(package) as zf:
            zf.extractall(staging)
        for name in sorted(info["members"]):
            src = staging / name
            if not src.is_file():
                continue
            target = app_dir / name
            old = app_dir / (name + ".old")
            if old.exists():
                old.unlink()  # 上次升级的残留
            if target.exists():
                os.replace(target, old)  # 运行中也可改名（镜像锁只挡覆盖/删除）
                append_install_log(log, f"改名 {name} → {name}.old（释放镜像锁）")
            os.replace(src, target)  # 同卷改名，原子落位
            replaced.append(name)
            append_install_log(log, f"落位 {name}")
        shutil.rmtree(staging, ignore_errors=True)
        append_install_log(log, f"文件落位完成，共 {len(replaced)} 个：{', '.join(replaced)}")
    except (OSError, zipfile.BadZipFile) as e:
        append_install_log(log, f"安装失败：{e}\n{traceback.format_exc()}")
        shutil.rmtree(staging, ignore_errors=True)
        raise UpdateError(f"安装更新包失败：{e}") from e
    return replaced


def _watchdog_bat(app_dir: Path, log: Path, result: Path, tray: Path,
                  pid: int) -> str:
    """构建看护脚本（.bat）内容：等旧进程退出 → 清理/回滚 .old → 拉起新托盘。

    全程无提权（普通用户权限即可），因此零 UAC。脚本只写 ASCII 行（避免
    控制台代码页把中文写花）；中文说明由新进程按 result 的 reason 码转述。
    结果标记必须在拉起程序之前写好——新进程启动即消费，避免竞态误报。
    启动成功的判据是「新进程消费掉了结果标记」（AppState 启动时读取并清除），
    而不是进程存活：卡死在错误对话框上的 exe 也算「存活」，不能骗过健康检查。
    出错才 `start notepad` 打开安装日志；成功路径绝不打开。
    """
    app_dir_s, log_s, result_s, tray_s = (str(p) for p in (app_dir, log, result, tray))
    lines = [
        "@echo off",
        "setlocal",
        # PyInstaller 父进程会把 bootstrap 环境变量（_PYI_*）传给子进程；
        # 不清掉的话，被拉起的新 exe 会误认自己是「已解压子进程」，直接去
        # 旧实例的 _MEI 目录加载 Python DLL（该目录正被旧实例删除）→ 起不来。
        # PYINSTALLER_RESET_ENVIRONMENT=1 再兜一层：强制任何 PyInstaller 子
        # 进程按全新 bootstrap 走（重新解压）。
        'set "_PYI_APPLICATION_HOME_DIR="',
        'set "_PYI_PARENT_PROCESS_LEVEL="',
        'set "_PYI_ARCHIVE_FILE="',
        'set "_PYI_SPLASH_IPC="',
        'set "_MEIPASS="',
        'set "_MEIPASS2="',
        'set "PYINSTALLER_RESET_ENVIRONMENT=1"',
        f'set "APPDIR={app_dir_s}"',
        f'set "LOG={log_s}"',
        f'set "RESULT={result_s}"',
        f'set "TRAY={tray_s}"',
        f"set PID={pid}",
        # 时间戳统一 ASCII：zh-CN 系统的 %date% 带「周一」字样、按控制台 GBK
        # 码页落盘，与 Python 写的 UTF-8 行混编后任何编辑器都只显示对一半
        'set "NOW="',
        'for /f "usebackq delims=" %%T in (`powershell -NoProfile -Command '
        '"(Get-Date).ToString(\'yyyy-MM-dd HH:mm:ss\')"`) do set "NOW=%%T"',
        'if not defined NOW set "NOW=%time%"',
        "set ERR=0",
        "set REASON=ok",
        "rem ---- wait for the old process to exit (max ~60s) ----",
        'powershell -NoProfile -Command "if (Get-Process -Id %PID% -ErrorAction '
        'SilentlyContinue) { try { Wait-Process -Id %PID% -Timeout 60 -ErrorAction '
        'Stop } catch { exit 1 } }"',
        "if errorlevel 1 goto timeout",
        "rem ---- clean or roll back *.old images ----",
        'for %%F in ("%APPDIR%\\*.old") do call :handle_old "%%~fF" "%%~dpnF"',
        "rem ---- swap-complete flag written by the app after files landed ----",
        f'if not exist "%APPDIR%\\{_SWAP_FLAG_NAME}" goto noswap',
        f'del /f /q "%APPDIR%\\{_SWAP_FLAG_NAME}" >nul 2>&1',
        "goto start_tray",
        ":noswap",
        "set ERR=1",
        "set REASON=interrupted",
        'echo [%NOW%] ERROR: update interrupted before files swapped >> "%LOG%"',
        ":start_tray",
        'if not exist "%TRAY%" goto notray',
        "rem result marker is written BEFORE relaunch: the new process consumes it",
        "rem on startup, so a later write would race with that read",
        'if "%ERR%"=="0" goto write_ok',
        'echo {"ok": false, "reason": "%REASON%"} > "%RESULT%"',
        "goto relaunch",
        ":write_ok",
        'echo {"ok": true} > "%RESULT%"',
        ":relaunch",
        'start "" "%TRAY%" --tray',
        "rem health = the new app consumed the result marker (proves it booted);",
        "rem a process stuck on an error dialog would fool a liveness probe",
        "set /a H=0",
        ":health",
        "ping -n 2 127.0.0.1 >nul",
        'if not exist "%RESULT%" goto running',
        "set /a H+=1",
        "if %H% GEQ 30 goto boot_failed",
        "goto health",
        ":boot_failed",
        "set ERR=1",
        "set REASON=tray-died-on-start",
        'echo {"ok": false, "reason": "tray-died-on-start"} > "%RESULT%"',
        'echo [%NOW%] ERROR: new version never finished starting >> "%LOG%"',
        "goto finish",
        ":running",
        "goto finish",
        ":notray",
        "set ERR=1",
        "set REASON=tray-missing",
        'echo {"ok": false, "reason": "tray-missing"} > "%RESULT%"',
        'echo [%NOW%] ERROR: "%TRAY%" not found, cannot restart >> "%LOG%"',
        "goto finish",
        ":timeout",
        "set ERR=1",
        "set REASON=old-process-lingering",
        'echo {"ok": false, "reason": "old-process-lingering"} > "%RESULT%"',
        'echo [%NOW%] ERROR: old process did not exit within 60s >> "%LOG%"',
        "goto finish",
        ":handle_old",
        'if exist "%~2" goto old_del',
        'move /y "%~1" "%~2" >nul',
        "set ERR=1",
        'if "%REASON%"=="ok" set REASON=partial-swap-rolled-back',
        'echo [%NOW%] rolled back %~nx1 >> "%LOG%"',
        "goto :eof",
        ":old_del",
        'del /f /q "%~1" >nul 2>&1',
        "goto :eof",
        ":finish",
        'if "%ERR%"=="0" goto done',
        'echo [%NOW%] ERROR: finished with errors, reason=%REASON% >> "%LOG%"',
        'start "" notepad "%LOG%"',
        ":done",
        "exit /b 0",
    ]
    return "\r\n".join(lines)


def write_watchdog(app_dir: Path, log: Path, pid: int | None = None) -> Path:
    """生成看护脚本到安装目录并返回其路径（调用方负责启动）。"""
    app_dir, log = Path(app_dir), Path(log)
    bat = app_dir / "orcsy-update-watchdog.bat"
    content = _watchdog_bat(app_dir, log, update_result_path(),
                            app_dir / "orcsy-tray.exe",
                            pid if pid is not None else os.getpid())
    bat.write_text(content, encoding="gbk")
    return bat


def _clean_env() -> dict[str, str]:
    """为子进程构建干净环境：剥离 PyInstaller bootstrap 变量。

    PyInstaller 6.x 的 onefile 父进程会把 _PYI_APPLICATION_HOME_DIR /
    _PYI_PARENT_PROCESS_LEVEL / _PYI_ARCHIVE_FILE 传给子进程；换名落位后新
    exe 与旧 exe 路径相同，_PYI_ARCHIVE_FILE 匹配会让 bootloader 跳过环境
    重置、误认自己是「已解压子进程」，直接去旧实例的 _MEI 目录加载 Python
    DLL（该目录正被旧实例退出清理）→ Failed to load Python DLL。_MEIPASS* 是
    老版变量名，一并剥离兜底；PYINSTALLER_RESET_ENVIRONMENT=1 强制全新 bootstrap。
    """
    drop = ("_PYI_APPLICATION_HOME_DIR", "_PYI_PARENT_PROCESS_LEVEL",
            "_PYI_ARCHIVE_FILE", "_PYI_SPLASH_IPC", "_MEIPASS", "_MEIPASS2")
    env = {k: v for k, v in os.environ.items() if k.upper() not in drop}
    env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
    return env


# 看护脚本进程创建标志：只用 CREATE_NO_WINDOW（隐藏控制台）——MSDN 明确
# CREATE_NO_WINDOW 在与 DETACHED_PROCESS 同用时会被忽略；而 DETACHED 让 cmd
# 没有控制台，powershell/find 等控制台子进程会各自弹出新窗口（曾弹出黑框）。
# 进程一旦创建就独立于本进程存活，不需要 DETACHED 来「防父退出被带走」。
_WATCHDOG_FLAGS = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP


def start_watchdog(bat: Path) -> subprocess.Popen | None:
    """分离启动看护脚本（无提权、无窗口）；环境经 _clean_env 剥离脏 bootstrap 变量。"""
    return subprocess.Popen(["cmd", "/c", str(bat)],
                            cwd=str(Path(bat).parent), env=_clean_env(),
                            creationflags=_WATCHDOG_FLAGS, close_fds=True)


# ---- 手动重启：看护脚本等旧进程退出后拉起当前 exe 本身（同更新重启的简化版） ----

def write_restart_watchdog(app_dir: Path | str, pid: int) -> Path:
    """生成手动重启看护脚本并返回其路径（调用方负责 start_watchdog 启动）。

    与升级看护（write_watchdog）同思路但职责单一：不碰文件、不写结果标记，
    只等当前进程退出（最多 60s）→ cd 到安装目录 → 拉起当前 exe 本身。
    托盘版补 --tray 参数（与升级看护的 start "" "%TRAY%" --tray 一致）。
    脚本写进 %TEMP% 下的一次性目录而不是安装目录：装在 Program Files 时
    安装目录不可写（写入会 PermissionError），而重启看护本不需要落在那里。
    环境清洗依赖 start_watchdog 的 _clean_env，脚本里无需再剥 _PYI_*。
    """
    app_dir = Path(app_dir)
    exe = Path(sys.executable)
    args = f'"{exe}"' + (" --tray" if exe.name.lower() == "orcsy-tray.exe" else "")
    lines = [
        "@echo off",
        "setlocal",
        f"set PID={pid}",
        # 等旧进程退出（最多 ~60s）：失败即放弃（旧进程可能卡在退出清理上，
        # 此时贸然拉新会撞端口/双实例；用户可手动再启动）
        'powershell -NoProfile -Command "if (Get-Process -Id %PID% -ErrorAction '
        'SilentlyContinue) { try { Wait-Process -Id %PID% -Timeout 60 -ErrorAction '
        'Stop } catch { exit 1 } }"',
        "if errorlevel 1 exit /b 1",
        # 新进程工作目录与升级看护拉起时一致（安装目录），不留在 %TEMP%
        f'cd /d "{app_dir}"',
        f'start "" {args}',
        "exit /b 0",
    ]
    # 一次性子目录 + 独占创建：不可预测路径 + 不覆盖既有文件（同更新下载的
    # 本地 TOCTOU 防护思路；脚本内容只是拉起本机自己的 exe，无提权面）
    holder = Path(tempfile.mkdtemp(prefix="orcsy-restart-"))
    bat = holder / "orcsy-restart-watchdog.bat"
    bat.write_text("\r\n".join(lines), encoding="gbk")
    return bat


def plan_restart() -> tuple[Path, Path]:
    """手动重启的前置计算：返回 (看护脚本路径, 待启动的 exe)。

    仅 Windows 安装版支持（同 can_apply 门槛）；不满足时抛 UpdateError，
    由 Web 层转 400 提示（源码：git pull 自行重启；Docker：重启容器）。
    """
    if not can_apply():
        raise UpdateError(
            "当前运行环境不支持程序内重启：" +
            ("请源码方式自行 Ctrl+C 后重新启动（改动在下次启动生效）。"
             if sys.platform == "win32" and not getattr(sys, "frozen", False)
             else "请按部署方式手动重启（Docker 重启容器 / Linux 重启进程）。"))
    app_dir = Path(sys.executable).parent
    return write_restart_watchdog(app_dir, os.getpid()), Path(sys.executable)


def run_local_install(package: Path | str, app_dir: Path | str,
                      log: Path | str) -> dict:
    """静默安装主流程（后台线程调用，零 UAC）：校验 → 启动看护 → 换名落位。

    成功返回 {"version", "replaced"}，随后调用方应退出本进程，由看护脚本
    等旧进程退出后拉起新版。失败抛 UpdateError：日志已含 traceback，
    调用方负责弹窗并自动打开日志（看护已按需终止，不会干扰重试）。
    """
    package, app_dir, log = Path(package), Path(app_dir), Path(log)
    if not can_apply():
        raise UpdateError(apply_gate_message())
    info = validate_package(package)
    if not is_writable(app_dir):
        # Program Files 等系统目录：提权兜底（一次 UAC），不再是死路报错
        return run_elevated_install(package, app_dir, log, info)
    append_install_log(log, f"==== 开始安装 {package.name}"
                            f"（目标版本 {info['version'] or '未知'}）→ {app_dir} ====")
    # 崩溃面包屑：若进程在完成前被杀，下次启动消费到「中断」并如实提示
    write_update_result({"ok": False, "reason": "install-interrupted"})
    bat = write_watchdog(app_dir, log)
    append_install_log(log, f"启动看护脚本 {bat.name}（等待旧进程退出后拉起新版）")
    proc = start_watchdog(bat)
    try:
        replaced = stage_package(package, app_dir, log)
    except UpdateError:
        if proc is not None:  # 失败不退出进程：终止看护，避免它误判中断/回滚
            try:
                proc.kill()
            except OSError:
                pass
        clear_update_result()  # 错误已在本进程即时弹窗+开日志，避免下次启动重复报
        raise
    (app_dir / _SWAP_FLAG_NAME).write_text("", encoding="utf-8")
    append_install_log(log, "换名落位完成；旧进程退出后看护脚本将拉起新版（成功不打开日志）")
    return {"version": info["version"], "replaced": replaced}


# ---- 提权兜底安装（安装目录不可写，如 C:\Program Files\Orcsy） ----
#
# 角色分工（新程序绝不运行在管理员态）：
# - 管理员复制脚本（copier，UAC 提权的 cmd）：等旧进程退出 → 备份 .old →
#   复制新文件 → 失败整体回滚 → 写纯文本完成标记（OK / FAIL <原因>）；
# - 看护脚本（watchdog，普通权限，复用 start_watchdog）：等旧进程退出 →
#   轮询完成标记 → 写 result JSON（先写后拉，契约同零 UAC 版）→ 拉起新托盘
#   → 健康检查（新进程消费掉 result 即算启动成功）→ 出错才打开安装日志。
# 两个脚本与暂存区都放在用户 temp（安装目录写不进去）；提权进程按内嵌的
# 绝对路径访问用户 temp 与安装日志（管理员可写）。

def _copier_bat(staging: Path, app_dir: Path, log: Path, marker: Path,
                pid: int, members: list[str]) -> str:
    """构建管理员复制脚本（.bat）：等旧进程退出后备份-复制-回滚-写标记。

    只写 ASCII 行（控制台代码页安全）；成员名来自 validate_package 白名单
    （纯文件名、无空格），for 列表安全。
    """
    member_list = " ".join(f'"{m}"' for m in members)
    lines = [
        "@echo off",
        "setlocal",
        f'set "STAGING={staging}"',
        f'set "APPDIR={app_dir}"',
        f'set "LOG={log}"',
        f'set "MARKER={marker}"',
        f"set PID={pid}",
        f"set MEMBERS={member_list}",
        # 时间戳统一 ASCII（理由同 _watchdog_bat：避免 GBK/UTF-8 混编乱码）
        'set "NOW="',
        'for /f "usebackq delims=" %%T in (`powershell -NoProfile -Command '
        '"(Get-Date).ToString(\'yyyy-MM-dd HH:mm:ss\')"`) do set "NOW=%%T"',
        'if not defined NOW set "NOW=%time%"',
        "set ERR=0",
        "rem ---- wait for the old process to exit (max ~60s) ----",
        'powershell -NoProfile -Command "if (Get-Process -Id %PID% -ErrorAction '
        'SilentlyContinue) { try { Wait-Process -Id %PID% -Timeout 60 -ErrorAction '
        'Stop } catch { exit 1 } }"',
        "if errorlevel 1 goto timeout",
        'echo [%NOW%] elevated copy started >> "%LOG%"',
        "rem ---- backup + copy each member ----",
        'for %%M in (%MEMBERS%) do call :copy_one "%%~M"',
        'if "%ERR%"=="1" goto copyfail',
        'del /f /q "%APPDIR%\\*.old" >nul 2>&1',
        'echo OK> "%MARKER%"',
        'echo [%NOW%] elevated copy completed >> "%LOG%"',
        "goto end",
        ":copy_one",
        'set "SRC=%STAGING%\\%~1"',
        'set "DST=%APPDIR%\\%~1"',
        'if not exist "%SRC%" goto :eof',
        'if exist "%DST%" move /y "%DST%" "%DST%.old" >nul',
        'copy /y "%SRC%" "%DST%" >nul',
        "if errorlevel 1 (",
        "  set ERR=1",
        '  echo [%NOW%] ERROR: copy failed for %~1 >> "%LOG%"',
        '  if exist "%DST%.old" move /y "%DST%.old" "%DST%" >nul',
        ")",
        "goto :eof",
        ":copyfail",
        "rem ---- full rollback: restore every .old backup ----",
        'for %%M in (%MEMBERS%) do if exist "%APPDIR%\\%%M.old" '
        'move /y "%APPDIR%\\%%M.old" "%APPDIR%\\%%M" >nul',
        'echo FAIL copy-failed> "%MARKER%"',
        'echo [%NOW%] ERROR: elevated copy rolled back >> "%LOG%"',
        "goto end",
        ":timeout",
        'echo FAIL old-process-lingering> "%MARKER%"',
        'echo [%NOW%] ERROR: old process did not exit within 60s >> "%LOG%"',
        "goto end",
        ":end",
        "exit /b 0",
    ]
    return "\r\n".join(lines)


def _watchdog_bat_elevated(app_dir: Path, log: Path, result: Path, tray: Path,
                           pid: int, marker: Path) -> str:
    """构建提权流程的看护脚本（普通权限）：等复制完成标记 → 写 result → 拉托盘。

    与零 UAC 版 _watchdog_bat 共享同一套 result/健康检查契约（结果标记先写、
    新进程消费即健康、出错才开日志），但没有 .old 清理与 app_dir swap flag
    ——那些都由管理员复制脚本在它自己的权限里完成了。
    """
    app_dir_s, log_s, result_s, tray_s, marker_s = (
        str(p) for p in (app_dir, log, result, tray, marker))
    lines = [
        "@echo off",
        "setlocal",
        # 与零 UAC 版同款：剥离 PyInstaller bootstrap 变量，防新 exe 误认
        # 自己是已解压子进程（详见 _watchdog_bat 内注释）
        'set "_PYI_APPLICATION_HOME_DIR="',
        'set "_PYI_PARENT_PROCESS_LEVEL="',
        'set "_PYI_ARCHIVE_FILE="',
        'set "_PYI_SPLASH_IPC="',
        'set "_MEIPASS="',
        'set "_MEIPASS2="',
        'set "PYINSTALLER_RESET_ENVIRONMENT=1"',
        f'set "APPDIR={app_dir_s}"',
        f'set "LOG={log_s}"',
        f'set "RESULT={result_s}"',
        f'set "TRAY={tray_s}"',
        f'set "MARKER={marker_s}"',
        f"set PID={pid}",
        "set ERR=0",
        "set REASON=ok",
        "rem ---- wait for the old process to exit (max ~60s) ----",
        'powershell -NoProfile -Command "if (Get-Process -Id %PID% -ErrorAction '
        'SilentlyContinue) { try { Wait-Process -Id %PID% -Timeout 60 -ErrorAction '
        'Stop } catch { exit 1 } }"',
        "if errorlevel 1 goto timeout",
        "rem ---- wait for the elevated copier marker (max ~120s) ----",
        "set /a W=0",
        ":waitmarker",
        'if exist "%MARKER%" goto marker',
        "ping -n 2 127.0.0.1 >nul",
        "set /a W+=1",
        "if %W% GEQ 60 goto markertimeout",
        "goto waitmarker",
        ":markertimeout",
        "set ERR=1",
        "set REASON=elevated-copy-timeout",
        'echo [%NOW%] ERROR: elevated copier marker missing after 120s >> "%LOG%"',
        "goto start_tray",
        ":marker",
        'findstr /b "OK" "%MARKER%" >nul',
        "if errorlevel 1 (",
        "  set ERR=1",
        "  set REASON=elevated-copy-failed",
        '  type "%MARKER%" >> "%LOG%"',
        '  echo [%NOW%] ERROR: elevated copier reported failure >> "%LOG%"',
        ")",
        'del /f /q "%MARKER%" >nul 2>&1',
        ":start_tray",
        'if not exist "%TRAY%" goto notray',
        "rem result marker is written BEFORE relaunch: the new process consumes it",
        "rem on startup, so a later write would race with that read",
        'if "%ERR%"=="0" goto write_ok',
        'echo {"ok": false, "reason": "%REASON%"} > "%RESULT%"',
        "goto relaunch",
        ":write_ok",
        'echo {"ok": true} > "%RESULT%"',
        ":relaunch",
        'start "" "%TRAY%" --tray',
        "rem health = the new app consumed the result marker (proves it booted);",
        "rem a process stuck on an error dialog would fool a liveness probe",
        "set /a H=0",
        ":health",
        "ping -n 2 127.0.0.1 >nul",
        'if not exist "%RESULT%" goto running',
        "set /a H+=1",
        "if %H% GEQ 30 goto boot_failed",
        "goto health",
        ":boot_failed",
        "set ERR=1",
        "set REASON=tray-died-on-start",
        'echo {"ok": false, "reason": "tray-died-on-start"} > "%RESULT%"',
        'echo [%NOW%] ERROR: new version never finished starting >> "%LOG%"',
        "goto finish",
        ":running",
        "goto finish",
        ":notray",
        "set ERR=1",
        "set REASON=tray-missing",
        'echo {"ok": false, "reason": "tray-missing"} > "%RESULT%"',
        'echo [%NOW%] ERROR: "%TRAY%" not found, cannot restart >> "%LOG%"',
        "goto finish",
        ":timeout",
        "set ERR=1",
        "set REASON=old-process-lingering",
        'echo {"ok": false, "reason": "old-process-lingering"} > "%RESULT%"',
        'echo [%NOW%] ERROR: old process did not exit within 60s >> "%LOG%"',
        "goto finish",
        ":finish",
        'if "%ERR%"=="0" goto done',
        'echo [%NOW%] ERROR: finished with errors, reason=%REASON% >> "%LOG%"',
        'start "" notepad "%LOG%"',
        ":done",
        "exit /b 0",
    ]
    return "\r\n".join(lines)


def run_elevated_install(package: Path, app_dir: Path, log: Path,
                         info: dict) -> dict:
    """提权兜底安装：一次 UAC，管理员复制 + 普通权限看护拉起新托盘。

    调用前提：run_local_install 已判定 app_dir 不可写。UAC 同意框在
    Start-Process -Verb RunAs 时弹出（阻塞到用户决定）：
    - 批准 → 复制脚本启动，函数返回，调用方退出进程，看护接管后续；
    - 拒绝 → 终止看护、清理 temp 与面包屑，抛 UpdateError（程序继续运行）。
    """
    work = Path(tempfile.gettempdir()) / f"orcsy-elev-{os.getpid()}"
    staging = work / "staging"
    marker = work / "copy-done.txt"
    shutil.rmtree(work, ignore_errors=True)  # 上次失败残留
    staging.mkdir(parents=True)
    append_install_log(log, f"==== 开始提权安装 {package.name}"
                            f"（目标版本 {info['version'] or '未知'}）→ {app_dir}"
                            f"（安装目录不可写，将请求一次管理员授权） ====")
    with zipfile.ZipFile(package) as zf:
        zf.extractall(staging)
    members = [n for n in sorted(info["members"]) if (staging / n).is_file()]
    if not members:
        shutil.rmtree(work, ignore_errors=True)
        raise UpdateError("更新包解压后没有可安装的文件")

    pid = os.getpid()
    copier = work / "orcsy-elev-copy.bat"
    copier.write_text(_copier_bat(staging, app_dir, log, marker, pid, members),
                      encoding="gbk")
    wdog = work / "orcsy-elev-watchdog.bat"
    wdog.write_text(_watchdog_bat_elevated(app_dir, log, update_result_path(),
                                           app_dir / "orcsy-tray.exe", pid, marker),
                    encoding="gbk")
    # 崩溃面包屑：提权复制完成前进程被杀，下次启动如实报「中断」
    write_update_result({"ok": False, "reason": "install-interrupted"})

    # 先拉管理员复制脚本（UAC 同意框在这里弹出，阻塞到用户决定），批准成功后
    # 再启动看护——顺序不能反：看护等旧进程退出只给 60 秒，若用户迟迟不点
    # UAC，先启动的看护会误报「旧进程未退出」退出，复制完成后就没人拉起新版了
    # cmd /c ""path"" 的双引号形态处理带空格路径（C:\Program Files\...）
    ps_cmd = (f'Start-Process -FilePath "$env:SystemRoot\\System32\\cmd.exe" '
              f'-ArgumentList \'/c ""{copier}""\' -Verb RunAs -WindowStyle Hidden')
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps_cmd],
            capture_output=True, timeout=300,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        rc, err_text = r.returncode, (r.stderr or b"").decode("utf-8", "replace").strip()
    except (OSError, subprocess.TimeoutExpired) as e:
        rc, err_text = -1, str(e)
    if rc != 0:
        # UAC 被拒绝/取消：清理痕迹、如实报错（程序继续运行，可重试）
        shutil.rmtree(work, ignore_errors=True)
        clear_update_result()
        append_install_log(log, f"提权启动失败或被取消（returncode={rc}）：{err_text[:200]}")
        raise UpdateError(
            "用户取消了管理员授权（UAC），更新未开始，程序继续运行。可重试更新；"
            "或把程序迁移到用户可写目录（如 %LOCALAPPDATA%\\Orcsy）后即可零 UAC 静默更新。")
    proc = start_watchdog(wdog)
    append_install_log(log, f"UAC 已批准，复制脚本运行中；启动看护脚本 {wdog.name}"
                            "（普通权限：等复制完成后拉起新托盘）")
    append_install_log(log, "旧进程即将退出，复制完成后看护脚本将拉起新版（成功不打开日志）")
    return {"version": info["version"], "replaced": members, "elevated": True,
            "watchdog": proc is not None}
