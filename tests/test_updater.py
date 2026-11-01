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
from orcsy.updater import (UpdateError, _clean_env, _watchdog_bat,
                           _WATCHDOG_FLAGS, append_install_log,
                           check_update, clear_update_result,
                           detect_installer_version, install_log_path,
                           parse_version, read_install_log,
                           read_update_result, result_message, run_local_install,
                           stage_package, validate_package, version_newer,
                           write_update_result)

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
        print("安装日志路径锚定 + 追加/读取 ✅")
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
        print("config.yaml update 节读回 ✅")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


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
    print("\n全部断言通过 ✅")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
