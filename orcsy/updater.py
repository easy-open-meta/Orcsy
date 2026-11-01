"""程序自更新：Gitee Release 检查 / 下载 / 手动导入 / 零 UAC 静默换名安装。

更新包为 ZIP（orcsy-update-*.zip，含 orcsy-web.exe / orcsy-tray.exe / README*）。
静默升级由后台线程执行「换名落位」：解压到安装目录暂存区，把运行中的 exe 改名
*.exe.old（Windows 允许对运行中的 PE 改名，绕开镜像锁），新文件 os.replace 落位，
再由看护脚本（detached、非提权）等旧进程退出后清理 .old 并拉起新托盘。全程同一
用户权限、不触发 UAC；安装日志逐步写入，升级结束若有错误自动打开日志文档，
无错误则不打开。Inno 安装包（orcsy-setup-*.exe）仅用于首次安装（允许 UAC）。
Docker / Linux / 源码运行只支持检查，应用时给出对应指引。
"""

from __future__ import annotations

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
from pathlib import Path
from typing import Any, Callable
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
    返回 {has_update, current, latest, notes, asset_name, download_url}。
    附件只认 orcsy-update-*.zip 更新包（exe 安装包仅首次安装用，不走在线更新）。
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
    return {
        "has_update": version_newer(latest, current_version),
        "current": current_version,
        "latest": latest,
        "notes": str(data.get("body") or ""),
        "asset_name": str(asset.get("name") or ""),
        "download_url": download_url,
    }


def download_installer(download_url: str, dest_dir: Path,
                       progress_cb: Callable[[int], None] | None = None,
                       filename: str = "orcsy-update-download.zip") -> Path:
    """流式下载更新包到 dest_dir，progress_cb 收到 0~100 的百分比。"""
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / filename
    # 环回地址绕过系统代理（Shadowsocks/Clash 类工具会把 127.0.0.1 的下载
    # 送进隧道导致超时）；外部地址仍尊重系统代理设置
    trust_env = urlparse(download_url).hostname not in ("127.0.0.1", "localhost", "::1")
    last_err: Exception | None = None
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
            last_err = e
            if attempt < 4 and isinstance(e, httpx.TransportError):
                time.sleep(0.5 * (attempt + 1))
                continue
            raise UpdateError(f"下载更新包失败：{e}") from e
    if not dest.exists() or dest.stat().st_size == 0:
        raise UpdateError("下载更新包失败：文件为空。")
    return dest


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
    """往安装日志追加一行（带时间戳）。日志写失败不掩盖主错误——调用方仍会弹窗。"""
    try:
        log = Path(log)
        log.parent.mkdir(parents=True, exist_ok=True)
        with open(log, "a", encoding="utf-8") as f:
            f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {text}\n")
    except OSError:
        pass


def read_install_log(log: Path | None = None, max_chars: int = 4000) -> str:
    """读取安装日志的尾部（诊断静默安装失败用）。"""
    log = Path(log) if log else install_log_path()
    try:
        return log.read_text(encoding="utf-8", errors="replace")[-max_chars:]
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


def check_writable(app_dir: Path) -> None:
    """安装目录可写性预检：换名落位需要当前用户写权限（零 UAC 方案的前提）。

    不可写（如 Program Files 默认权限）时明确报错并指引迁移目录——
    不尝试提权，也就不会出现 UAC 窗口。
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
        'echo [%date% %time%] ERROR: update interrupted before files swapped >> "%LOG%"',
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
        'echo [%date% %time%] ERROR: new version never finished starting >> "%LOG%"',
        "goto finish",
        ":running",
        "goto finish",
        ":notray",
        "set ERR=1",
        "set REASON=tray-missing",
        'echo {"ok": false, "reason": "tray-missing"} > "%RESULT%"',
        'echo [%date% %time%] ERROR: "%TRAY%" not found, cannot restart >> "%LOG%"',
        "goto finish",
        ":timeout",
        "set ERR=1",
        "set REASON=old-process-lingering",
        'echo {"ok": false, "reason": "old-process-lingering"} > "%RESULT%"',
        'echo [%date% %time%] ERROR: old process did not exit within 60s >> "%LOG%"',
        "goto finish",
        ":handle_old",
        'if exist "%~2" goto old_del',
        'move /y "%~1" "%~2" >nul',
        "set ERR=1",
        'if "%REASON%"=="ok" set REASON=partial-swap-rolled-back',
        'echo [%date% %time%] rolled back %~nx1 >> "%LOG%"',
        "goto :eof",
        ":old_del",
        'del /f /q "%~1" >nul 2>&1',
        "goto :eof",
        ":finish",
        'if "%ERR%"=="0" goto done',
        'echo [%date% %time%] ERROR: finished with errors, reason=%REASON% >> "%LOG%"',
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
    check_writable(app_dir)
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
