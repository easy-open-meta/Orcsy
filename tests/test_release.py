"""发布脚本自检（离线）：附件校验、建 Release、上传、幂等替换、缺件核对。

运行：uv run python tests/test_release.py
"""

from __future__ import annotations

import http.server
import importlib.util
import re
import shutil
import socketserver
import sys
import tempfile
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "."))

# 用文件路径加载 build/release.py：避免与 site-packages 里的 pypa「build」包撞名
_spec = importlib.util.spec_from_file_location(
    "orcsy_release", Path(__file__).resolve().parents[1] / "build" / "release.py")
assert _spec and _spec.loader
release = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(release)

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


def _fake_pkg_dir() -> Path:
    d = Path(tempfile.mkdtemp(prefix="orcsy-rel-pkg-"))
    (d / "orcsy-setup-0.15.0.exe").write_bytes(b"MZ" + b"x" * 2048)
    (d / "orcsy-update-0.15.0.zip").write_bytes(b"PK" + b"y" * 1024)
    return d


def check_artifacts() -> None:
    d = _fake_pkg_dir()
    try:
        files = release.check_artifacts((0, 15, 0), setup_dir=d)
        assert [f.name for f in files] == ["orcsy-setup-0.15.0.exe",
                                           "orcsy-update-0.15.0.zip"], files
        # 缺 zip（只发了 exe）→ 明确报错，不许「顺手」漏发
        (d / "orcsy-update-0.15.0.zip").unlink()
        try:
            release.check_artifacts((0, 15, 0), setup_dir=d)
            raise AssertionError("缺 zip 应报错")
        except release.ReleaseError as e:
            assert "orcsy-update-0.15.0.zip" in str(e) and "build" in str(e)
        print("check_artifacts（两附件齐全/缺 zip 拦下）✅")
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
        assert rel["created"] and rel["id"] == 1, rel
        for f in files:
            release.upload_asset("https://gitee.com/o/r", rel["id"], f, "tk",
                                 api_base=base)
        names = release.verify_assets("https://gitee.com/o/r", rel["id"], files,
                                      "tk", api_base=base)
        assert names == ["orcsy-setup-0.15.0.exe", "orcsy-update-0.15.0.zip"], names
        print("发布流程（建 Release → 上传 exe+zip → 核对）✅")
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
        assert len(STATE["uploads"]) == n_first + 2  # 各重传一次
        names = release.verify_assets("https://gitee.com/o/r", rel["id"], files,
                                      "tk", api_base=base)
        assert names == ["orcsy-setup-0.15.0.exe", "orcsy-update-0.15.0.zip"]
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
            (fake / "build" / "Setup" / name).write_bytes(data)
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


def main() -> int:
    check_artifacts()
    check_upload_flow()
    check_idempotent_replace()
    check_dry_run()
    print("\n全部断言通过 ✅")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
