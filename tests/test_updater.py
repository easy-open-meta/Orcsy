"""程序自更新自检（离线）：版本比较、Gitee Release 检查、更新包校验、
换名落位、看护脚本、结果标记、安装日志、配置往返。

运行：uv run python tests/test_updater.py
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import threading
import http.server
import socketserver
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "."))

from orcsy.config import load_config
from orcsy.updater import (UpdateError, _clean_env, _copier_bat, _watchdog_bat,
                           _watchdog_bat_elevated, _WATCHDOG_FLAGS,
                           append_install_log, check_update, clear_update_result,
                           detect_installer_version, download_installer,
                           fetch_expected_sha256, install_log_path, is_writable,
                           parse_sha256_file, parse_version, prune_stale_download_dirs,
                           read_install_log, read_update_result, result_message,
                           run_local_install, stage_package, update_result_path,
                           validate_package, version_newer, write_update_result)

AUTH: dict[str, str] = {}  # 假服务捕获的请求头（User-Agent / Authorization 断言用）


def check_parse_version() -> None:
    assert parse_version("V0.13.0") == (0, 13, 0)
    assert parse_version("v0.14") == (0, 14)
    assert parse_version("0.14.0.1") == (0, 14, 0, 1)
    assert parse_version("") == ()
    assert version_newer("0.14.0", "0.13.0")
    assert version_newer("V0.14", "0.13.9")   # 段数不齐按 0 补齐比较
    assert not version_newer("0.13.0", "0.13.0")
    assert not version_newer("0.12.9", "0.13.0")
    assert not version_newer("unknown", "0.13.0")  # 解析失败宁可不更
    print("版本解析与比较 ✅")


def check_detect_installer() -> None:
    assert detect_installer_version("orcsy-setup-0.14.0.exe") == "0.14.0"
    assert detect_installer_version("orcsy-update-0.15.0.zip") == "0.15.0"
    assert detect_installer_version("orcsy-setup-0.14.exe") == "0.14"
    assert detect_installer_version("随便.exe") is None
    print("更新包文件名版本解析 ✅")


def _make_zip(path: Path, members: dict[str, bytes]) -> Path:
    with zipfile.ZipFile(path, "w") as zf:
        for name, data in members.items():
            zf.writestr(name, data)
    return path


def check_validate_package() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="orcsy-upd-pkg-"))
    try:
        good = _make_zip(tmp / "orcsy-update-0.15.0.zip", {
            "orcsy-web.exe": b"web", "orcsy-tray.exe": b"tray",
            "README.md": b"readme"})
        info = validate_package(good)
        assert info["version"] == "0.15.0", info
        assert set(info["members"]) == {"orcsy-web.exe", "orcsy-tray.exe", "README.md"}

        # 缺 exe
        missing = _make_zip(tmp / "orcsy-update-0.15.1.zip", {"orcsy-tray.exe": b"t"})
        try:
            validate_package(missing)
            raise AssertionError("缺 orcsy-web.exe 应报错")
        except UpdateError as e:
            assert "orcsy-web.exe" in str(e)

        # 白名单外成员
        bad = _make_zip(tmp / "orcsy-update-0.15.2.zip", {
            "orcsy-web.exe": b"w", "orcsy-tray.exe": b"t", "evil.bat": b"x"})
        try:
            validate_package(bad)
            raise AssertionError("白名单外文件应报错")
        except UpdateError as e:
            assert "evil.bat" in str(e)

        # zip-slip：带路径穿越的成员
        slip = tmp / "orcsy-update-0.15.3.zip"
        with zipfile.ZipFile(slip, "w") as zf:
            zf.writestr("../evil.exe", b"x")
            zf.writestr("orcsy-web.exe", b"w")
            zf.writestr("orcsy-tray.exe", b"t")
        try:
            validate_package(slip)
            raise AssertionError("zip-slip 成员应报错")
        except UpdateError as e:
            assert "非法路径" in str(e) or "evil" in str(e)

        # 非 zip / 损坏 zip / 不存在
        not_zip = tmp / "orcsy-update-0.15.4.zip"
        not_zip.write_bytes(b"this is not a zip")
        try:
            validate_package(not_zip)
            raise AssertionError("损坏 zip 应报错")
        except UpdateError as e:
            assert "zip" in str(e).lower()
        exe_only = tmp / "orcsy-setup-0.15.5.exe"
        exe_only.write_bytes(b"MZ")
        try:
            validate_package(exe_only)
            raise AssertionError("exe 不是更新包，应报错")
        except UpdateError as e:
            assert "zip" in str(e).lower()
        try:
            validate_package(tmp / "no-such.zip")
            raise AssertionError("不存在应报错")
        except UpdateError as e:
            assert "不存在" in str(e)
        print("validate_package（好包/缺exe/白名单/zip-slip/损坏/非zip）✅")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def check_stage_package() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="orcsy-upd-stage-"))
    try:
        app = tmp / "app"
        app.mkdir()
        (app / "orcsy-web.exe").write_bytes(b"OLD-WEB")   # 假装是运行中的旧 exe
        (app / "orcsy-tray.exe").write_bytes(b"OLD-TRAY")
        (app / "README.md").write_bytes(b"OLD-README")
        pkg = _make_zip(tmp / "orcsy-update-0.15.0.zip", {
            "orcsy-web.exe": b"NEW-WEB", "orcsy-tray.exe": b"NEW-TRAY",
            "README.md": b"NEW-README"})
        log = tmp / "install.log"

        replaced = stage_package(pkg, app, log)
        assert set(replaced) == {"orcsy-web.exe", "orcsy-tray.exe", "README.md"}, replaced
        # 新文件落位、旧文件改名保留（看护等进程退出后清理）
        assert (app / "orcsy-web.exe").read_bytes() == b"NEW-WEB"
        assert (app / "orcsy-web.exe.old").read_bytes() == b"OLD-WEB"
        assert (app / "orcsy-tray.exe").read_bytes() == b"NEW-TRAY"
        assert (app / "orcsy-tray.exe.old").read_bytes() == b"OLD-TRAY"
        assert (app / "README.md.old").read_bytes() == b"OLD-README"
        # 暂存区清空、每步留痕
        assert not (app / ".orcsy-staging").exists()
        text = read_install_log(log)
        assert "改名 orcsy-web.exe" in text and "落位 orcsy-tray.exe" in text, text

        # 覆盖安装（同版本重装）：目标已存在 → 依旧换名落位
        stage_package(pkg, app, log)
        assert (app / "orcsy-web.exe").read_bytes() == b"NEW-WEB"
        assert (app / "orcsy-web.exe.old").read_bytes() == b"NEW-WEB"  # 上轮的新版成了 .old
        print("stage_package（换名落位/覆盖重装/日志留痕/暂存清理）✅")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def check_apply_gate() -> None:
    # 源码运行（非 frozen）：应用更新必须被门禁拦下并给出指引
    try:
        run_local_install(Path("orcsy-update-0.15.0.zip"), Path("D:/x"), Path("t.log"))
        raise AssertionError("非 frozen 应拒绝应用更新")
    except UpdateError as e:
        assert "git pull" in str(e)
    print("apply 门禁（源码运行给指引）✅")


def check_watchdog_script() -> None:
    cmd = _watchdog_bat(Path("D:/x"), Path("C:/u/logs/install.log"),
                        Path("C:/t/orcsy-update-result.json"),
                        Path("D:/x/orcsy-tray.exe"), 4321)
    # 零 UAC：绝不出现提权动词
    assert "runas" not in cmd and "ShellExecute" not in cmd
    assert '\\"' not in cmd, f"引号被转义，cmd 会解析失败：{cmd}"
    # 时间戳纯 ASCII（%date% 在中文系统带「周一」，GBK 落盘与 UTF-8 行混编必乱码）
    assert "%date%" not in cmd and "[%NOW%]" in cmd and "Get-Date" in cmd
    assert "set PID=4321" in cmd and "Wait-Process" in cmd  # 按 PID 等旧进程退出
    assert "*.old" in cmd and "move /y" in cmd              # 清理 + 回滚残留
    # 脚本内剥 PyInstaller bootstrap 变量（防新 exe 误认「已解压子进程」）
    for var in ('set "_PYI_APPLICATION_HOME_DIR="', 'set "_PYI_PARENT_PROCESS_LEVEL="',
                'set "_PYI_ARCHIVE_FILE="', 'set "_MEIPASS="', 'set "_MEIPASS2="'):
        assert var in cmd, var
    assert 'set "PYINSTALLER_RESET_ENVIRONMENT=1"' in cmd
    # 结果标记必须在拉起程序之前写（新进程启动即消费，防竞态误报）
    launch_at = cmd.index('start "" "%TRAY%"')
    assert cmd.index('echo {"ok": true}') < launch_at
    assert cmd.index('echo {"ok": false, "reason": "%REASON%"}') < launch_at
    # 健康检查 = 结果标记被新进程消费（进程存活会骗过卡死在错误框的 exe）
    assert 'if not exist "%RESULT%"' in cmd
    assert "tasklist" not in cmd and "find /I" not in cmd  # 控制台子进程会弹窗，弃用
    # 成功分支不打开日志；只有 ERR!=0 才 notepad（要求：无错误不打开日志）
    assert 'start "" notepad "%LOG%"' in cmd
    assert cmd.index('start "" notepad') > cmd.index(":finish")
    assert 'if "%ERR%"=="0" goto done' in cmd
    print("看护脚本（等旧进程/回滚/结果先行/启动判据/出错才开日志/零提权）✅")


def check_clean_env() -> None:
    import os as _os
    old = dict(_os.environ)
    try:
        _os.environ["_PYI_APPLICATION_HOME_DIR"] = r"C:\Temp\_MEI1"
        _os.environ["_PYI_PARENT_PROCESS_LEVEL"] = "PARENT"
        _os.environ["_PYI_ARCHIVE_FILE"] = r"S:\Orcsy\orcsy-tray.exe"
        _os.environ["_MEIPASS"] = r"C:\Temp\_MEI1"
        _os.environ["_MEIPASS2"] = r"C:\Temp\_MEI1"
        _os.environ["KEEP_ME"] = "keep"
        env = _clean_env()
        for var in ("_PYI_APPLICATION_HOME_DIR", "_PYI_PARENT_PROCESS_LEVEL",
                    "_PYI_ARCHIVE_FILE", "_MEIPASS", "_MEIPASS2"):
            assert var not in env, var
        assert env.get("PYINSTALLER_RESET_ENVIRONMENT") == "1"
        assert env.get("KEEP_ME") == "keep"
        # 进程创建标志：绝不与 DETACHED_PROCESS 同用（会让 CREATE_NO_WINDOW
        # 失效、控制台子进程弹窗）
        assert not (_WATCHDOG_FLAGS & subprocess.DETACHED_PROCESS)
        assert _WATCHDOG_FLAGS & subprocess.CREATE_NO_WINDOW
        print("子进程环境剥离（_PYI_* 全家 + RESET_ENVIRONMENT + 无弹窗标志）✅")
    finally:
        _os.environ.clear()
        _os.environ.update(old)


def check_update_result_marker() -> None:
    clear_update_result()
    assert read_update_result() is None
    write_update_result({"ok": False, "reason": "tray-missing"})
    res = read_update_result()
    assert res == {"ok": False, "reason": "tray-missing"}, res
    clear_update_result()
    assert read_update_result() is None
    # reason → 中文说明兜底
    assert "orcsy-tray" in result_message("tray-missing")
    assert "未知" in result_message("no-such-reason") or "no-such-reason" in result_message("no-such-reason")
    print("更新结果标记读写 + reason 中文映射 ✅")


def check_install_log() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="orcsy-upd-log-"))
    try:
        # install_log_path：无配置回退 %TEMP%；带配置锚定 <配置目录>/logs/install.log
        assert install_log_path(None).name == "orcsy-setup-install.log"

        class _Cfg:
            source_path = tmp / "config.yaml"
        log = install_log_path(_Cfg())
        assert log == tmp / "logs" / "install.log", log
        append_install_log(log, "第一步")
        append_install_log(log, "第二步：失败样例")
        tail = read_install_log(log, max_chars=1000)
        assert "第一步" in tail and "第二步" in tail
        # 新建日志带 UTF-8 BOM（Notepad/VS Code 据此识别，且全文仅一个 BOM）
        raw = log.read_bytes()
        assert raw.startswith(b"\xef\xbb\xbf") and raw.count(b"\xef\xbb\xbf") == 1
        # 混编码旧日志（GBK 的 bat 行 + UTF-8 的 Python 行）自愈：改名归档重开
        mixed = tmp / "logs" / "mixed.log"
        mixed.write_bytes(
            "[2026/11/02 周一 3:31:07.39] elevated copy started\r\n".encode("gbk")
            + "[2026-11-02 03:31:03] UTF-8 中文行\n".encode("utf-8"))
        append_install_log(mixed, "新行")
        legacy = tmp / "logs" / "mixed.log.legacy-gbk"
        assert legacy.is_file(), "混编码旧日志应改名归档"
        assert mixed.read_bytes().startswith(b"\xef\xbb\xbf")
        assert "新行" in read_install_log(mixed)
        # 纯 UTF-8 旧日志不误伤
        clean = tmp / "logs" / "clean.log"
        clean.write_text("[x] 中文旧行\n", encoding="utf-8")
        append_install_log(clean, "追加")
        assert clean.is_file()
        assert not (tmp / "logs" / "clean.log.legacy-gbk").exists()
        print("安装日志路径锚定 + 追加/读取 + BOM + 混编码自愈 ✅")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def check_check_update() -> None:
    RELEASE = {
        "tag_name": "V0.14.0",
        "body": "新增自更新",
        "assets": [
            {"name": "orcsy-setup-0.14.0.exe", "browser_download_url": "https://gitee.com/o/r/releases/download/V0.14.0/orcsy-setup-0.14.0.exe"},
            {"name": "orcsy-update-0.14.0.zip", "browser_download_url": "https://gitee.com/o/r/releases/download/V0.14.0/orcsy-update-0.14.0.zip"},
            {"name": "README.md", "browser_download_url": "https://gitee.com/o/r/releases/download/V0.14.0/README.md"},
        ],
    }

    class FakeGitee(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            AUTH["ua"] = self.headers.get("User-Agent") or ""
            body = json.dumps(RELEASE).encode()
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a): pass

    class _Srv(socketserver.ThreadingTCPServer):
        allow_reuse_address = True
        daemon_threads = True

    srv = _Srv(("127.0.0.1", 0), FakeGitee)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        base = f"http://127.0.0.1:{srv.server_address[1]}"
        r = check_update({"repo": f"https://gitee.com/o/r", "api_base": base, "token": ""},
                         current_version="0.13.0")
        assert r["has_update"] and r["latest"] == "V0.14.0", r
        assert AUTH["ua"].startswith("Orcsy/"), f"UA 应标识程序身份：{AUTH['ua']}"
        assert r["asset_name"] == "orcsy-update-0.14.0.zip"  # 只认 zip 更新包，不挑 exe
        assert r["download_url"].startswith("https://gitee.com/")
        # 已是最新
        r = check_update({"repo": f"https://gitee.com/o/r", "api_base": base,},
                         current_version="0.14.0")
        assert not r["has_update"]
        # token 追加到下载地址（私有仓库）
        r = check_update({"repo": f"https://gitee.com/o/r", "api_base": base, "token": "tk"}, "0.13.0")
        assert "access_token=tk" in r["download_url"]
        # 只有 exe 附件 → 明确提示缺 zip，不静默回退 UAC 路径
        RELEASE["assets"] = [{"name": "orcsy-setup-0.14.0.exe",
                              "browser_download_url": "https://gitee.com/x.exe"}]
        try:
            check_update({"repo": f"https://gitee.com/o/r", "api_base": base}, "0.13.0")
            raise AssertionError("缺 zip 更新包应报错")
        except UpdateError as e:
            assert "zip" in str(e).lower()
        RELEASE["assets"] = [
            {"name": "orcsy-update-0.14.0.zip",
             "browser_download_url": "https://gitee.com/o/r/releases/download/V0.14.0/orcsy-update-0.14.0.zip"},
        ]
        # 404（无 Release）→ 明确报错
        class H404(FakeGitee):
            def do_GET(self):
                self.send_response(404)
                self.send_header("content-length", "0")
                self.end_headers()
        srv2 = _Srv(("127.0.0.1", 0), H404)
        threading.Thread(target=srv2.serve_forever, daemon=True).start()
        try:
            check_update({"repo": f"https://gitee.com/o/r",
                          "api_base": f"http://127.0.0.1:{srv2.server_address[1]}"}, "0.13.0")
            raise AssertionError("404 应报错")
        except UpdateError as e:
            assert "Release" in str(e) or "404" in str(e)
        print("check_update（假 Gitee：命中/最新/只认 zip/缺 zip 报错/404）✅")
    finally:
        srv.shutdown()


def check_config_roundtrip() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="orcsy-upd-cfg-"))
    try:
        (tmp / "config.yaml").write_text(
            "llm:\n  api_key: sk-x\n"
            "update:\n"
            "  repo: https://gitee.com/o/r\n"
            "  token: tok-1\n"
            "  auto_check: false\n",
            encoding="utf-8")
        cfg = load_config(tmp / "config.yaml")
        assert cfg.update.repo == "https://gitee.com/o/r"
        assert cfg.update.token == "tok-1" and cfg.update.auto_check is False
        # 老配置没有 update 节（或 repo 留空）→ 回退官方默认源，零配置可检查更新
        from orcsy.config import DEFAULT_UPDATE_REPO
        bare = tmp / "bare.yaml"
        bare.write_text("llm:\n  api_key: sk-x\n", encoding="utf-8")
        assert load_config(bare, require_api_key=False).update.repo == DEFAULT_UPDATE_REPO
        empty = tmp / "empty.yaml"
        empty.write_text("llm:\n  api_key: sk-x\nupdate:\n  repo: \"\"\n", encoding="utf-8")
        assert load_config(empty, require_api_key=False).update.repo == DEFAULT_UPDATE_REPO
        print("config.yaml update 节读回（含缺省回退官方源）✅")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---- 提权兜底（安装目录不可写，如 C:\Program Files\Orcsy） ----

def check_is_writable() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="orcsy-upd-w-"))
    try:
        assert is_writable(tmp) is True          # 用户 temp 可写
        assert is_writable(tmp / "nope") is False  # 不存在路径：False 而非抛错
        assert not list(tmp.glob(".orcsy-write-probe-*")), "探针文件应即写即删"
        print("is_writable 探测（可写/不可写/探针清理）✅")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def check_copier_script() -> None:
    cmd = _copier_bat(Path("C:/u/temp/orcsy-elev-1/staging"),
                      Path("C:/Program Files/Orcsy"),
                      Path("C:/u/logs/install.log"),
                      Path("C:/u/temp/orcsy-elev-1/copy-done.txt"),
                      4321, ["orcsy-tray.exe", "orcsy-web.exe"])
    cmd.encode("ascii")  # 纯 ASCII：控制台代码页安全
    assert "set PID=4321" in cmd and "Wait-Process" in cmd      # 等旧进程退出
    assert 'move /y "%DST%" "%DST%.old"' in cmd                 # 先备份
    assert 'copy /y "%SRC%" "%DST%"' in cmd                     # 再复制
    assert ":copyfail" in cmd and "%%M.old" in cmd              # 失败整体回滚
    assert 'echo OK> "%MARKER%"' in cmd                         # 成功标记
    assert "FAIL copy-failed" in cmd and "FAIL old-process-lingering" in cmd
    assert '"orcsy-tray.exe" "orcsy-web.exe"' in cmd            # 成员白名单进 for 列表
    assert "runas" not in cmd.lower().replace("-verb runas", "")  # 脚本自身不提权（由外部拉起）
    print("管理员复制脚本（等进程/备份/复制/整体回滚/纯文本标记/ASCII）✅")


def check_watchdog_elevated_script() -> None:
    cmd = _watchdog_bat_elevated(Path("C:/Program Files/Orcsy"),
                                 Path("C:/u/logs/install.log"),
                                 Path("C:/t/orcsy-update-result.json"),
                                 Path("C:/Program Files/Orcsy/orcsy-tray.exe"),
                                 4321, Path("C:/u/temp/orcsy-elev-1/copy-done.txt"))
    cmd.encode("ascii")
    # 看护自身绝不提权（UAC 只发生在拉起复制脚本那一次）
    assert "runas" not in cmd and "ShellExecute" not in cmd
    assert "set PID=4321" in cmd and "Wait-Process" in cmd      # 等旧进程退出
    assert ":waitmarker" in cmd and 'findstr /b "OK" "%MARKER%"' in cmd  # 轮询复制标记
    assert "elevated-copy-timeout" in cmd and "elevated-copy-failed" in cmd
    # 新程序以普通权限拉起（start 而非任何提权动词）
    assert 'start "" "%TRAY%" --tray' in cmd
    # 结果标记先写后拉（契约同零 UAC 版）
    launch_at = cmd.index('start "" "%TRAY%"')
    assert cmd.index('echo {"ok": true}') < launch_at
    assert cmd.index('echo {"ok": false, "reason": "%REASON%"}') < launch_at
    # 健康检查 = result 被新进程消费；出错才开日志
    assert 'if not exist "%RESULT%"' in cmd
    assert cmd.index('start "" notepad') > cmd.index(":finish")
    # 没有 app_dir swap flag 与 .old 清理段（那些在复制脚本的管理员权限里做）
    assert ".orcsy-swapped" not in cmd
    # PyInstaller bootstrap 变量剥离（同零 UAC 版）
    assert 'set "PYINSTALLER_RESET_ENVIRONMENT=1"' in cmd
    assert 'set "_PYI_ARCHIVE_FILE="' in cmd
    print("提权版看护脚本（轮询标记/新 reason/结果先行/普通权限拉托盘/出错才开日志）✅")


def _mock_elevated_env(rc: int):
    """给 run_local_install 的提权分支搭 mock 环境；返回 (patch还原fn, 捕获dict)。"""
    import orcsy.updater as U
    from types import SimpleNamespace

    captured: dict = {"watchdog": [], "ps": []}
    orig = {k: getattr(U, k) for k in
            ("can_apply", "is_writable", "start_watchdog", "subprocess")}

    class FakeProc:
        def kill(self) -> None:
            captured.setdefault("killed", []).append(True)

    U.can_apply = lambda: True
    U.is_writable = lambda p: False  # 强制走提权分支

    def fake_start_watchdog(bat):
        captured["watchdog"].append(Path(bat))
        return FakeProc()
    U.start_watchdog = fake_start_watchdog

    class FakeSubprocess:
        CREATE_NO_WINDOW = 0

        @staticmethod
        def run(cmd, **kw):
            captured["ps"].append((cmd, kw))
            return SimpleNamespace(returncode=rc, stderr=b"", stdout=b"")
    U.subprocess = FakeSubprocess

    def restore() -> None:
        for k, v in orig.items():
            setattr(U, k, v)
    return restore, captured


def check_elevated_install_flow() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="orcsy-upd-elev-"))
    import os as _os
    work = Path(tempfile.gettempdir()) / f"orcsy-elev-{_os.getpid()}"
    try:
        pkg = _make_zip(tmp / "orcsy-update-0.17.0.zip", {
            "orcsy-web.exe": b"WEB", "orcsy-tray.exe": b"TRAY", "README.md": b"# r"})
        app_dir = tmp / "ProgramFiles" / "Orcsy"   # is_writable 已 mock，目录只需存在
        app_dir.mkdir(parents=True)
        logf = tmp / "install.log"
        clear_update_result()
        restore, cap = _mock_elevated_env(rc=0)   # UAC 批准
        try:
            out = run_local_install(pkg, app_dir, logf)
        finally:
            restore()
        assert out["elevated"] is True and out["version"] == "0.17.0", out
        assert sorted(out["replaced"]) == ["README.md", "orcsy-tray.exe", "orcsy-web.exe"]
        # 暂存区解压落地 + 两个脚本写到用户 temp（安装目录写不进去）
        assert (work / "staging" / "orcsy-web.exe").is_file()
        copier = work / "orcsy-elev-copy.bat"
        assert copier.is_file() and str(app_dir) in copier.read_text(encoding="gbk")
        # UAC 批准成功后才启动看护（顺序不能反：防看护 60s 误报）
        assert len(cap["watchdog"]) == 1 and cap["watchdog"][0].name == \
            "orcsy-elev-watchdog.bat"
        # powershell 提权命令：-Verb RunAs + 复制脚本路径
        ps_cmd, kw = cap["ps"][0]
        joined = " ".join(ps_cmd)
        assert "-Verb RunAs" in joined and "orcsy-elev-copy.bat" in joined
        assert ps_cmd[0] == "powershell" and "-NoProfile" in ps_cmd
        # 崩溃面包屑已写（进程退出前被杀 → 下次启动如实报中断）
        assert read_update_result() == {"ok": False, "reason": "install-interrupted"}
        log_text = logf.read_text(encoding="utf-8")
        assert "提权安装" in log_text and "UAC 已批准" in log_text
        clear_update_result()
        print("提权安装流程（分流/暂存/脚本落地/UAC 后启动看护/面包屑/日志）✅")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        shutil.rmtree(work, ignore_errors=True)


def check_uac_cancel_flow() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="orcsy-upd-uac-"))
    import os as _os
    work = Path(tempfile.gettempdir()) / f"orcsy-elev-{_os.getpid()}"
    try:
        pkg = _make_zip(tmp / "orcsy-update-0.17.0.zip", {
            "orcsy-web.exe": b"WEB", "orcsy-tray.exe": b"TRAY"})
        app_dir = tmp / "Orcsy"
        app_dir.mkdir(parents=True)
        logf = tmp / "install.log"
        clear_update_result()
        restore, cap = _mock_elevated_env(rc=1)   # UAC 取消/被拒
        try:
            try:
                run_local_install(pkg, app_dir, logf)
                raise AssertionError("UAC 取消应抛 UpdateError")
            except UpdateError as e:
                assert "UAC" in str(e) and "继续运行" in str(e), e
        finally:
            restore()
        # 取消路径：看护绝不启动、temp 工作目录清干净、面包屑不残留
        assert cap["watchdog"] == [], "UAC 批准前不得启动看护"
        assert not work.exists(), "取消后应清理 temp 工作目录"
        assert read_update_result() is None, "取消后不应残留中断面包屑"
        assert "取消" in logf.read_text(encoding="utf-8")
        print("UAC 取消路径（如实报错/程序继续运行/痕迹清理/看护未启动）✅")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        shutil.rmtree(work, ignore_errors=True)


def check_elevated_reasons() -> None:
    assert "提权" in result_message("elevated-copy-timeout")
    assert "回滚" in result_message("elevated-copy-failed")
    print("提权流程 reason 码转述 ✅")


# ---- sha256 完整性校验（安全审计 2026-11-04 M2） ----

class _ReleaseSrv(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def _serve_release(release: dict) -> tuple[_ReleaseSrv, str]:
    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            body = json.dumps(release).encode()
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a): pass

    srv = _ReleaseSrv(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


def check_check_update_sha256() -> None:
    """Release 里同名 .sha256 附件 → sha256_url；asset.digest → sha256_value。"""
    zip_url = "https://gitee.com/o/r/releases/download/V0.19.0/orcsy-update-0.19.0.zip"
    release = {
        "tag_name": "V0.19.0",
        "assets": [
            {"name": "orcsy-update-0.19.0.zip", "browser_download_url": zip_url},
            {"name": "orcsy-update-0.19.0.zip.sha256",
             "browser_download_url": zip_url + ".sha256"},
        ],
    }
    srv, base = _serve_release(release)
    try:
        r = check_update({"repo": "https://gitee.com/o/r", "api_base": base}, "0.18.0")
        assert r["sha256_url"] == zip_url + ".sha256", r
        assert r["sha256_value"] == "", r
        # 私有仓库：token 同样追加到校验文件下载地址
        r = check_update({"repo": "https://gitee.com/o/r", "api_base": base,
                          "token": "tk"}, "0.18.0")
        assert "access_token=tk" in r["sha256_url"], r
        # GitHub 新版 API：asset.digest 直接给哈希值
        release["assets"] = [{
            "name": "orcsy-update-0.19.0.zip", "browser_download_url": zip_url,
            "digest": "sha256:" + "a" * 64,
        }]
        r = check_update({"repo": "https://gitee.com/o/r", "api_base": base}, "0.18.0")
        assert r["sha256_value"] == "a" * 64 and r["sha256_url"] == "", r
        # 都没有 → 两个键为空（上层据此拒绝在线更新）
        release["assets"] = [{"name": "orcsy-update-0.19.0.zip",
                              "browser_download_url": zip_url}]
        r = check_update({"repo": "https://gitee.com/o/r", "api_base": base}, "0.18.0")
        assert r["sha256_value"] == "" and r["sha256_url"] == "", r
        print("check_update sha256 来源（同名附件/digest/缺省为空 + token 透传）✅")
    finally:
        srv.shutdown()


def check_download_sha256_verify() -> None:
    """download_installer 校验：正确通过；篡改拒绝并删包；.sha256 解析。"""
    import hashlib

    payload = b"PK" + b"update-payload" * 500
    good_hash = hashlib.sha256(payload).hexdigest()
    sha_text = f"{good_hash}  orcsy-update-0.19.0.zip\n".encode()

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            body = payload if self.path == "/pkg.zip" else sha_text
            self.send_response(200)
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a): pass

    srv = _ReleaseSrv(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    tmp = Path(tempfile.mkdtemp(prefix="orcsy-upd-sha-"))
    try:
        base = f"http://127.0.0.1:{srv.server_address[1]}"
        # 正确哈希 → 通过
        dest = download_installer(f"{base}/pkg.zip", tmp, filename="ok.zip",
                                  expected_sha256=good_hash)
        assert dest.read_bytes() == payload
        # 篡改（期望哈希不符）→ 报错且不落盘
        try:
            download_installer(f"{base}/pkg.zip", tmp, filename="bad.zip",
                               expected_sha256="f" * 64)
            raise AssertionError("哈希不符应报错")
        except UpdateError as e:
            assert "sha256" in str(e) and "校验" in str(e), e
        assert not (tmp / "bad.zip").exists(), "哈希不符的包不能留在磁盘上"
        # .sha256 解析（sha256sum 标准格式）+ 下载解析
        sha_file = tmp / "x.zip.sha256"
        sha_file.write_text(f"{good_hash}  x.zip\n", encoding="ascii")
        assert parse_sha256_file(sha_file) == good_hash
        sha_file.write_text(good_hash.upper() + "\n", encoding="ascii")
        assert parse_sha256_file(sha_file) == good_hash  # 大小写归一
        sha_file.write_text("not-a-hash\n", encoding="ascii")
        try:
            parse_sha256_file(sha_file)
            raise AssertionError("非法校验文件应报错")
        except UpdateError as e:
            assert "无法识别" in str(e), e
        assert fetch_expected_sha256(f"{base}/pkg.zip.sha256", tmp) == good_hash
        print("download_installer 哈希校验（通过/篡改删包/.sha256 解析与下载）✅")
    finally:
        srv.shutdown()
        shutil.rmtree(tmp, ignore_errors=True)


def check_prune_stale_dirs() -> None:
    """在线更新残留临时目录：超龄清理、新鲜的保留。"""
    import os
    import time

    tmp_root = Path(tempfile.gettempdir())
    stale = tmp_root / f"orcsy-update-test-stale-{os.getpid()}"
    fresh = tmp_root / f"orcsy-update-test-fresh-{os.getpid()}"
    try:
        stale.mkdir()
        fresh.mkdir()
        old = time.time() - 2 * 86400
        os.utime(stale, (old, old))
        prune_stale_download_dirs()
        assert not stale.exists(), "超龄下载目录应清理"
        assert fresh.exists(), "1 天内的下载目录应保留"
        assert update_result_path().name == "orcsy-update-result.json"
        print("prune_stale_download_dirs（超龄清理/新鲜保留）✅")
    finally:
        shutil.rmtree(stale, ignore_errors=True)
        shutil.rmtree(fresh, ignore_errors=True)


def main() -> int:
    check_parse_version()
    check_detect_installer()
    check_validate_package()
    check_stage_package()
    check_apply_gate()
    check_watchdog_script()
    check_clean_env()
    check_update_result_marker()
    check_install_log()
    check_check_update()
    check_config_roundtrip()
    check_is_writable()
    check_copier_script()
    check_watchdog_elevated_script()
    check_elevated_install_flow()
    check_uac_cancel_flow()
    check_elevated_reasons()
    check_check_update_sha256()
    check_download_sha256_verify()
    check_prune_stale_dirs()
    print("\n全部断言通过 ✅")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
