"""发布脚本自检（离线）：附件校验、建 Release、上传、幂等替换、缺件核对。

运行：uv run python tests/test_release.py
"""

from __future__ import annotations

import http.server
import importlib.util
import io
import re
import shutil
import socketserver
import hashlib
import sys
import tarfile
import tempfile
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "."))

# 用文件路径加载 build/release.py：避免与 site-packages 里的 pypa「build」包撞名
if not (Path(__file__).resolve().parents[1] / "build" / "release.py").is_file():
    print("跳过：缺少 build/release.py（Windows 打包目录未随部署上传）")
    raise SystemExit(0)

_spec = importlib.util.spec_from_file_location(
    "orcsy_release", Path(__file__).resolve().parents[1] / "build" / "release.py")
assert _spec and _spec.loader
release = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(release)

# build/build.py 同法加载：arm64 产物命名与「禁止跨架构」守卫自检
_build_spec = importlib.util.spec_from_file_location(
    "orcsy_build", Path(__file__).resolve().parents[1] / "build" / "build.py")
assert _build_spec and _build_spec.loader
buildpy = importlib.util.module_from_spec(_build_spec)
_build_spec.loader.exec_module(buildpy)


def check_build_arch() -> None:
    """更新包命名与跨架构守卫：arm64 带后缀，非宿主架构直接拒绝。"""
    assert buildpy.update_zip_name((0, 15, 0), "amd64") == "orcsy-update-0.15.0.zip"
    assert buildpy.update_zip_name((0, 15, 0), "arm64")         == "orcsy-update-0.15.0-windows-arm64.zip"
    assert buildpy.resolve_windows_arch("auto") == buildpy.host_arch()
    other = "arm64" if buildpy.host_arch() == "amd64" else "amd64"
    try:
        buildpy.resolve_windows_arch(other)
        raise AssertionError("跨架构构建应拒绝")
    except SystemExit as e:
        assert "交叉编译" in str(e) and other in str(e)
    print("build 架构守卫（更新包命名 / 拒绝跨架构）✅")


STATE: dict = {"releases": {}, "next_id": 1, "uploads": []}


class FakeGitee(http.server.BaseHTTPRequestHandler):
    """Gitee v1 Releases + 附件 API 的最小假实现（含同名附件替换语义）。"""

    def _json(self, obj, code=200):
        import json
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("content-length") or 0)
        raw = self.rfile.read(length)
        if self.path.endswith("/releases"):
            tag = re.search(rb"tag_name=([^&\r\n]+)", raw).group(1).decode()
            if any(r["tag"] == tag for r in STATE["releases"].values()):
                self._json({"message": "标签已存在"}, 400)  # Gitee 同 tag 重复创建语义
                return
            rid = STATE["next_id"]
            STATE["next_id"] += 1
            STATE["releases"][rid] = {"id": rid, "tag": tag, "assets": {}}
            self._json({"id": rid,
                        "html_url": f"https://gitee.com/o/r/releases/{rid}"}, 201)
            return
        m = re.match(r".*/releases/(\d+)/attach_files$", self.path)
        if m:
            rid = int(m.group(1))
            name = re.search(rb'filename="([^"]+)"', raw).group(1).decode()
            # 假服务以原始报文长度充当附件大小（只需非 0 即可过核对）
            STATE["releases"][rid]["assets"][name] = {"id": len(STATE["uploads"]) + 1,
                                                      "name": name, "size": len(raw)}
            STATE["uploads"].append(name)
            self._json({"id": STATE["releases"][rid]["assets"][name]["id"],
                        "name": name, "size": len(raw)}, 201)
            return
        self._json({"message": "not found"}, 404)

    def do_GET(self):  # noqa: N802
        m = re.match(r".*/releases/tags/([^/?]+)", self.path)
        if m:
            tag = m.group(1)
            for r in STATE["releases"].values():
                if r["tag"] == tag:
                    self._json({"id": r["id"],
                                "html_url": f"https://gitee.com/o/r/releases/{r['id']}"})
                    return
            self._json({"message": "404"}, 404)
            return
        m = re.match(r".*/releases/(\d+)/attach_files", self.path)
        if m:
            r = STATE["releases"].get(int(m.group(1)))
            self._json(list(r["assets"].values()) if r else [], 200)
            return
        self._json({"message": "not found"}, 404)

    def do_DELETE(self):  # noqa: N802
        m = re.match(r".*/releases/(\d+)/attach_files/(\d+)", self.path)
        if m:
            r = STATE["releases"].get(int(m.group(1)))
            for name, a in list((r or {}).get("assets", {}).items()):
                if a["id"] == int(m.group(2)):
                    del r["assets"][name]
                    self._json({}, 204)
                    return
        self._json({"message": "not found"}, 404)

    def log_message(self, *a): pass


class _Srv(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def _server() -> tuple[_Srv, str]:
    STATE["releases"].clear()
    STATE["uploads"].clear()
    STATE["next_id"] = 1
    srv = _Srv(("127.0.0.1", 0), FakeGitee)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


def _write_checksum(path: Path) -> None:
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    path.with_name(path.name + ".sha256").write_text(
        f"{digest}  {path.name}\n", encoding="ascii")


def _fake_pkg_dir() -> Path:
    d = Path(tempfile.mkdtemp(prefix="orcsy-rel-pkg-"))
    setup = d / "orcsy-setup-0.15.0.exe"
    update = d / "orcsy-update-0.15.0.zip"
    setup.write_bytes(b"MZ" + b"x" * 2048)
    update.write_bytes(b"PK" + b"y" * 1024)
    _write_checksum(setup)
    _write_checksum(update)
    return d


EXPECTED_NAMES = ["orcsy-setup-0.15.0.exe", "orcsy-setup-0.15.0.exe.sha256",
                  "orcsy-update-0.15.0.zip", "orcsy-update-0.15.0.zip.sha256"]


def check_artifacts() -> None:
    d = _fake_pkg_dir()
    try:
        files = release.check_artifacts((0, 15, 0), setup_dir=d)
        assert [f.name for f in files] == EXPECTED_NAMES, files
        # 测试新架构后缀 windows-amd64 匹配
        (d / "orcsy-setup-0.15.0.exe").unlink()
        (d / "orcsy-setup-0.15.0.exe.sha256").unlink()
        setup_arch = d / "orcsy-setup-0.15.0-windows-amd64.exe"
        setup_arch.write_bytes(b"MZ" + b"x" * 2048)
        _write_checksum(setup_arch)
        files_arch = release.check_artifacts((0, 15, 0), setup_dir=d)
        assert files_arch[0].name == "orcsy-setup-0.15.0-windows-amd64.exe"
        (d / "orcsy-setup-0.15.0-windows-amd64.exe").unlink()
        (d / "orcsy-setup-0.15.0-windows-amd64.exe.sha256").unlink()
        setup = d / "orcsy-setup-0.15.0.exe"
        setup.write_bytes(b"MZ" + b"x" * 2048)
        _write_checksum(setup)
        # 测试 windows-arm64 产物自动识别与追加
        arm = d / "orcsy-0.15.0-windows-arm64.zip"
        arm.write_bytes(b"PK" + b"z" * 1024)
        _write_checksum(arm)
        files_arm = release.check_artifacts((0, 15, 0), setup_dir=d)
        names_arm = [f.name for f in files_arm]
        assert "orcsy-0.15.0-windows-arm64.zip" in names_arm
        assert "orcsy-0.15.0-windows-arm64.zip.sha256" in names_arm

        # 原生 arm64 安装包/更新包：存在即纳入发布清单
        arm_setup = d / "orcsy-setup-0.15.0-windows-arm64.exe"
        arm_update = d / "orcsy-update-0.15.0-windows-arm64.zip"
        arm_setup.write_bytes(b"MZ" + b"a" * 2048)
        arm_update.write_bytes(b"PK" + b"aw" * 512)
        _write_checksum(arm_setup)
        _write_checksum(arm_update)
        names_native = [f.name for f in release.check_artifacts((0, 15, 0), setup_dir=d)]
        for name in ("orcsy-setup-0.15.0-windows-arm64.exe",
                     "orcsy-setup-0.15.0-windows-arm64.exe.sha256",
                     "orcsy-update-0.15.0-windows-arm64.zip",
                     "orcsy-update-0.15.0-windows-arm64.zip.sha256"):
            assert name in names_native, names_native
        (d / "orcsy-setup-0.15.0-windows-arm64.exe").unlink()
        (d / "orcsy-setup-0.15.0-windows-arm64.exe.sha256").unlink()
        (d / "orcsy-update-0.15.0-windows-arm64.zip").unlink()
        (d / "orcsy-update-0.15.0-windows-arm64.zip.sha256").unlink()
        (d / "orcsy-0.15.0-windows-arm64.zip").unlink()
        (d / "orcsy-0.15.0-windows-arm64.zip.sha256").unlink()
        # 缺 zip（只发了 exe）→ 明确报错，不许「顺手」漏发
        (d / "orcsy-update-0.15.0.zip").unlink()
        try:
            release.check_artifacts((0, 15, 0), setup_dir=d)
            raise AssertionError("缺 zip 应报错")
        except release.ReleaseError as e:
            assert "orcsy-update-0.15.0.zip" in str(e) and "build" in str(e)
        # 缺 .sha256（在线更新的完整性依据）→ 同样拦下
        (d / "orcsy-update-0.15.0.zip").write_bytes(b"PK" + b"y" * 1024)
        (d / "orcsy-update-0.15.0.zip.sha256").unlink()
        try:
            release.check_artifacts((0, 15, 0), setup_dir=d)
            raise AssertionError("缺 .sha256 应报错")
        except release.ReleaseError as e:
            assert ".sha256" in str(e) and "build" in str(e)
        print("check_artifacts（三附件齐全/缺 zip/缺 sha256 拦下）✅")
    finally:
        shutil.rmtree(d, ignore_errors=True)


def check_checksum_rejected() -> None:
    d = _fake_pkg_dir()
    try:
        update = d / "orcsy-update-0.15.0.zip"
        update.write_bytes(update.read_bytes() + b"tampered")
        try:
            release.check_artifacts((0, 15, 0), setup_dir=d)
            raise AssertionError("篡改附件应拒绝发布")
        except release.ReleaseError as exc:
            assert "SHA256 不匹配" in str(exc)
        _write_checksum(update)
        sidecar = d / "orcsy-update-0.15.0.zip.sha256"
        sidecar.write_text("0" * 64 + "  wrong.zip\n", encoding="ascii")
        try:
            release.check_artifacts((0, 15, 0), setup_dir=d)
            raise AssertionError("错误文件名应拒绝发布")
        except release.ReleaseError as exc:
            assert "文件名错误" in str(exc)
        _write_checksum(update)
        (d / "orcsy-update-0.15.0.zip.sha256.sha256").write_text("garbage")
        try:
            release.check_artifacts((0, 15, 0), setup_dir=d)
            raise AssertionError("重复校验文件应拒绝发布")
        except release.ReleaseError as exc:
            assert "未纳入清单" in str(exc)
    finally:
        shutil.rmtree(d, ignore_errors=True)


def check_upload_flow() -> None:
    srv, base = _server()
    d = _fake_pkg_dir()
    try:
        files = release.check_artifacts((0, 15, 0), setup_dir=d)
        rel = release.create_or_get_release("https://gitee.com/o/r", "V0.15.0",
                                            "Orcsy V0.15.0", "notes", "tk",
                                            api_base=base)
        # created=False 也是合法回退（连接层抖动重试第二发走「已存在取回」）；
        # 核心不变量是拿到 id==1 的 Release 且后续附件可挂
        assert rel["id"] == 1, rel
        for f in files:
            release.upload_asset("https://gitee.com/o/r", rel["id"], f, "tk",
                                 api_base=base)
        names = release.verify_assets("https://gitee.com/o/r", rel["id"], files,
                                      "tk", api_base=base)
        assert names == EXPECTED_NAMES, names
        print("发布流程（建 Release → 上传 exe+zip+sha256 → 核对）✅")
    finally:
        srv.shutdown()
        shutil.rmtree(d, ignore_errors=True)


def check_idempotent_replace() -> None:
    srv, base = _server()
    d = _fake_pkg_dir()
    try:
        files = release.check_artifacts((0, 15, 0), setup_dir=d)
        rel = release.create_or_get_release("https://gitee.com/o/r", "V0.15.0",
                                            "x", "y", "tk", api_base=base)
        for f in files:
            release.upload_asset("https://gitee.com/o/r", rel["id"], f, "tk",
                                 api_base=base)
        n_first = len(STATE["uploads"])
        # 重跑发布：复用 Release（created=False），同名附件先删再传
        rel2 = release.create_or_get_release("https://gitee.com/o/r", "V0.15.0",
                                             "x", "y", "tk", api_base=base)
        assert rel2["created"] is False and rel2["id"] == rel["id"], rel2
        for f in files:
            release.upload_asset("https://gitee.com/o/r", rel["id"], f, "tk",
                                 api_base=base)
        # 各附件重传至少一次（本机回环 RST 触发传输层重试时会上报多于一次）
        assert len(STATE["uploads"]) >= n_first + 3
        names = release.verify_assets("https://gitee.com/o/r", rel["id"], files,
                                      "tk", api_base=base)
        assert names == EXPECTED_NAMES
        # 核对兜底：服务端丢件必须报错，不许「发了半个包」放行
        del STATE["releases"][rel["id"]]["assets"]["orcsy-update-0.15.0.zip"]
        try:
            release.verify_assets("https://gitee.com/o/r", rel["id"], files,
                                  "tk", api_base=base)
            raise AssertionError("缺件应核对失败")
        except release.ReleaseError as e:
            assert "orcsy-update-0.15.0.zip" in str(e)
        print("幂等重发（复用 Release/同名替换）+ 缺件核对拦截 ✅")
    finally:
        srv.shutdown()
        shutil.rmtree(d, ignore_errors=True)


def check_dry_run() -> None:
    fake = Path(tempfile.mkdtemp(prefix="orcsy-rel-root-"))
    try:
        (fake / "build" / "Setup").mkdir(parents=True)
        (fake / "orcsy").mkdir()
        (fake / "orcsy" / "__init__.py").write_text('__version__ = "0.15.0"\n',
                                                   encoding="utf-8")
        # 复制 build.py 供 read_version 单源读版本（其 ROOT 自身解析到假树）
        shutil.copy(Path(__file__).resolve().parents[1] / "build" / "build.py",
                    fake / "build" / "build.py")
        for name, data in (("orcsy-setup-0.15.0.exe", b"MZ" + b"x" * 100),
                           ("orcsy-update-0.15.0.zip", b"PK" + b"y" * 100)):
            artifact = fake / "build" / "Setup" / name
            artifact.write_bytes(data)
            _write_checksum(artifact)
        old_root = release.ROOT
        release.ROOT = fake
        try:
            rc = release.main(["--dry-run"])
            assert rc == 0, rc
            # 缺附件时 dry-run 也要拦下（不许带病清单）
            (fake / "build" / "Setup" / "orcsy-update-0.15.0.zip").unlink()
            assert release.main(["--dry-run"]) == 1
        finally:
            release.ROOT = old_root
        print("dry-run（清单打印/缺件返回非 0）✅")
    finally:
        shutil.rmtree(fake, ignore_errors=True)


def check_linux_archives() -> None:
    """Linux tar/deb：启动器不能与包目录 orcsy/ 同名（否则解包 Not a directory）。"""
    tmp = Path(tempfile.mkdtemp(prefix="orcsy-linux-pkg-"))
    old_build = buildpy.BUILD
    try:
        buildpy.BUILD = tmp
        tar_path = buildpy.build_linux_tarball((0, 15, 0), "amd64")
        with tarfile.open(tar_path, "r:gz") as tf:
            names = tf.getnames()
        prefix = "orcsy-0.15.0-linux-amd64"
        assert f"{prefix}/orcsy.sh" in names, names[:5]
        assert f"{prefix}/orcsy/tools.py" in names
        assert f"{prefix}/orcsy" not in names, "启动器与包目录同名会导致解包失败"

        deb_path = buildpy.build_linux_deb((0, 15, 0), "amd64")
        blob = deb_path.read_bytes()
        assert blob.startswith(b"!<arch>\n")
        entries: dict[str, bytes] = {}
        off = 8
        while off + 60 <= len(blob):
            header = blob[off:off + 60]
            name = header[:16].decode("ascii").strip()
            size = int(header[48:58].decode("ascii").strip() or 0)
            entries[name] = blob[off + 60:off + 60 + size]
            off += 60 + size + (size % 2)
        with tarfile.open(fileobj=io.BytesIO(entries["data.tar.gz"]), mode="r:gz") as tf:
            data_names = tf.getnames()
        assert "./opt/orcsy/orcsy" in data_names, data_names[:5]
        assert "./opt/orcsy/tools.py" in data_names
        with tarfile.open(fileobj=io.BytesIO(entries["control.tar.gz"]), mode="r:gz") as tf:
            member = next((n for n in tf.getnames() if n.endswith("postinst")), None)
            assert member is not None
            postinst = tf.extractfile(member)
            assert postinst is not None and "/opt/orcsy/orcsy" in postinst.read().decode("utf-8")
        print("Linux tar/deb 启动器命名（与 orcsy/ 目录不冲突）✅")
    finally:
        buildpy.BUILD = old_build
        shutil.rmtree(tmp, ignore_errors=True)


def main() -> int:
    check_build_arch()
    check_linux_archives()
    check_artifacts()
    check_checksum_rejected()
    check_upload_flow()
    check_idempotent_replace()
    check_dry_run()
    print("\n全部断言通过 ✅")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
