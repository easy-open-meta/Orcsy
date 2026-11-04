"""新工具实现级测试：delete_file / move_file / multi_edit / find_files / web_fetch。

直接调用工具函数（dispatch 层与开关机制在 test_tools_toggle.py 覆盖）。
web_fetch 的集成测试在本机起一次性 HTTP 服务，并 monkeypatch 掉 SSRF 守卫
（守卫本身单独直测 _assert_public_url）。
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from orcsy import tools as T
from orcsy.tools import ToolContext


def make_root() -> Path:
    root = Path(tempfile.mkdtemp(prefix="tools-extra-"))
    (root / "src").mkdir()
    (root / "src" / "app.py").write_text("value = 1\nname = 'a'\n", encoding="utf-8")
    (root / "src" / "util.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    (root / "docs").mkdir()
    (root / "docs" / "readme.md").write_text("# hi\n", encoding="utf-8")
    (root / "node_modules").mkdir()
    (root / "node_modules" / "junk.js").write_text("x\n", encoding="utf-8")
    return root


def check_delete() -> None:
    root = make_root()
    try:
        ctx = ToolContext(root=root)
        assert T.tool_delete_file(ctx, {"path": "docs/readme.md"}).startswith("已删除")
        assert not (root / "docs" / "readme.md").exists()
        # 目录不带 recursive → 拒绝
        r = T.tool_delete_file(ctx, {"path": "docs"})
        assert r.startswith("错误") and "recursive" in r, r
        assert (root / "docs").is_dir()
        # 目录带 recursive → 整树删除
        assert T.tool_delete_file(ctx, {"path": "docs", "recursive": True}).startswith("已删除")
        assert not (root / "docs").exists()
        # 项目根不可删
        assert T.tool_delete_file(ctx, {"path": "."}).startswith("错误")
        # 不存在 → 错误文本（不抛异常）
        assert T.tool_delete_file(ctx, {"path": "nope.txt"}).startswith("错误")
        # 路径穿越：直接调用抛 ValueError；经 dispatch_tool（模型实际路径）转错误文本
        try:
            T.tool_delete_file(ctx, {"path": "../outside"})
            raise AssertionError("路径穿越应拒绝")
        except ValueError:
            pass
        assert T.dispatch_tool(ctx, "delete_file", {"path": "../outside"}
                               ).startswith("错误：路径越界")
        print("ok: delete_file（文件/目录 recursive/根保护/穿越拒绝）")
    finally:
        shutil.rmtree(root, ignore_errors=True)


def check_move() -> None:
    root = make_root()
    try:
        ctx = ToolContext(root=root)
        # 重命名
        assert T.tool_move_file(ctx, {"source": "src/util.py",
                                      "destination": "src/helper.py"}).startswith("已移动")
        assert (root / "src" / "helper.py").is_file() and not (root / "src" / "util.py").exists()
        # 目标已存在且未带 overwrite → 拒绝，双方原样
        r = T.tool_move_file(ctx, {"source": "src/helper.py", "destination": "src/app.py"})
        assert r.startswith("错误") and "overwrite" in r, r
        assert (root / "src" / "helper.py").is_file()
        # overwrite=true → 覆盖
        assert T.tool_move_file(ctx, {"source": "src/helper.py", "destination": "src/app.py",
                                      "overwrite": True}).startswith("已移动")
        assert "def f()" in (root / "src" / "app.py").read_text(encoding="utf-8")
        # 自动创建目标父目录；目录整体移动
        assert T.tool_move_file(ctx, {"source": "node_modules",
                                      "destination": "vendor/nm"}).startswith("已移动")
        assert (root / "vendor" / "nm" / "junk.js").is_file()
        # 源不存在 / 穿越
        assert T.tool_move_file(ctx, {"source": "nope", "destination": "x"}).startswith("错误")
        try:
            T.tool_move_file(ctx, {"source": "src", "destination": "../out"})
            raise AssertionError("路径穿越应拒绝")
        except ValueError:
            pass
        print("ok: move_file（重命名/覆盖保护/建父目录/目录移动/穿越拒绝）")
    finally:
        shutil.rmtree(root, ignore_errors=True)


def check_multi_edit() -> None:
    root = make_root()
    try:
        ctx = ToolContext(root=root)
        r = T.tool_multi_edit(ctx, {"path": "src/app.py", "edits": [
            {"old_string": "value = 1", "new_string": "value = 2"},
            {"old_string": "name = 'a'", "new_string": "name = 'b'"},
        ]})
        assert r.startswith("已修改") and "2 处" in r, r
        assert (root / "src" / "app.py").read_text(encoding="utf-8") == "value = 2\nname = 'b'\n"
        # 原子性：第 2 条不存在 → 整体失败，文件保持上一次成功后的内容
        before = (root / "src" / "app.py").read_text(encoding="utf-8")
        r = T.tool_multi_edit(ctx, {"path": "src/app.py", "edits": [
            {"old_string": "value = 2", "new_string": "value = 3"},
            {"old_string": "missing", "new_string": "x"},
        ]})
        assert r.startswith("错误") and "第 2 条" in r and "未改动" in r, r
        assert (root / "src" / "app.py").read_text(encoding="utf-8") == before
        # 非唯一匹配 → 拒绝
        r = T.tool_multi_edit(ctx, {"path": "src/app.py", "edits": [
            {"old_string": "= ", "new_string": "=  "}]})
        assert r.startswith("错误") and "唯一" in r, r
        # 非法入参
        assert T.tool_multi_edit(ctx, {"path": "src/app.py", "edits": []}).startswith("错误")
        assert T.tool_multi_edit(ctx, {"path": "nope.py",
                                       "edits": [{"old_string": "a", "new_string": "b"}]}
                                 ).startswith("错误")
        print("ok: multi_edit（多处替换/原子性/唯一匹配校验/非法入参）")
    finally:
        shutil.rmtree(root, ignore_errors=True)


def check_find_files() -> None:
    root = make_root()
    try:
        ctx = ToolContext(root=root)
        out = T.tool_find_files(ctx, {"pattern": "*.py"})
        assert "src/app.py" in out and "src/util.py" in out, out
        assert "junk.js" not in out
        # ignores 生效：node_modules 里的文件不出现
        assert "node_modules" not in T.tool_find_files(ctx, {"pattern": "*.js"}), "应忽略 node_modules"
        # 子目录起点 + 无匹配 + 空 pattern
        assert "readme.md" in T.tool_find_files(ctx, {"pattern": "*.md", "path": "docs"})
        assert T.tool_find_files(ctx, {"pattern": "*.zzz"}) == "（无匹配）"
        assert T.tool_find_files(ctx, {"pattern": " "}).startswith("错误")
        print("ok: find_files（glob/ignores/起点目录/无匹配/空 pattern）")
    finally:
        shutil.rmtree(root, ignore_errors=True)


def check_ssrf_guard() -> None:
    bad = [
        "http://127.0.0.1:5116/api/state",   # 回环
        "http://localhost/x",                 # 回环域名
        "http://[::1]/x",                     # IPv6 回环
        "http://169.254.169.254/latest/meta-data",  # 云元数据
        "http://192.168.1.1/admin",           # 私网
        "http://10.0.0.8/",                   # 私网
        "ftp://example.com/f",                # 非 http(s)
        "file:///C:/Windows/win.ini",          # 本地文件
        "not-a-url",                          # 无 scheme
    ]
    for url in bad:
        try:
            T._assert_public_url(url)
            raise AssertionError(f"SSRF 守卫应拒绝：{url}")
        except ValueError:
            pass
    T._assert_public_url("http://93.184.216.34/")  # 公网 IP 字面量（example.com），无需 DNS
    T._assert_public_url("https://example.com/docs")  # DNS 不可达时抛「无法解析」也属守卫行为
    print("ok: _assert_public_url（回环/私网/元数据/非 http 拒绝，公网放行）")


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        if self.path == "/page":
            body = ("<html><head><title>t</title><style>b{}</style></head><body>"
                    "<h1>标题</h1><script>evil()</script><p>正文段落 内容</p>"
                    "</body></html>").encode()
            ctype = "text/html; charset=utf-8"
        elif self.path == "/raw.txt":
            body, ctype = "plain text body".encode(), "text/plain"
        elif self.path == "/img":
            body, ctype = b"\x89PNG\x00\x00", "image/png"
        else:
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):  # noqa: A002
        pass


def check_web_fetch() -> None:
    root = make_root()
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    # 集成测试放行回环（守卫逻辑已在 check_ssrf_guard 单独直测）
    original = T._assert_public_url
    T._assert_public_url = lambda url: None
    try:
        ctx = ToolContext(root=root)

        def _fetch(url, **kw):
            # 回环偶发 RST（本项目已知环境怪癖，WinError 10054）：重试至多 3 次
            last = ""
            for _ in range(3):
                last = T.tool_web_fetch(ctx, {"url": url, **kw})
                if not last.startswith("错误：请求失败"):
                    return last
            return last

        out = _fetch(f"http://127.0.0.1:{port}/page")
        assert "正文段落 内容" in out and "标题" in out, out
        assert "evil()" not in out and "b{}" not in out, "script/style 应被剥离"
        assert out.startswith("[HTTP 200"), out
        out = _fetch(f"http://127.0.0.1:{port}/raw.txt")
        assert "plain text body" in out, out
        # 二进制类型拒绝；404 报错文本；空 url
        assert _fetch(f"http://127.0.0.1:{port}/img"
                      ).startswith("错误：不支持的内容类型"), "image/* 应拒绝"
        assert "HTTP 404" in _fetch(f"http://127.0.0.1:{port}/nope")
        assert T.tool_web_fetch(ctx, {"url": ""}).startswith("错误")
        # 截断参数生效
        out = _fetch(f"http://127.0.0.1:{port}/raw.txt", max_chars=1000)
        assert "plain text body" in out
        print("ok: web_fetch（HTML 剥标签/文本原样/二进制拒绝/404/空 url）")
    finally:
        T._assert_public_url = original
        server.shutdown()
        shutil.rmtree(root, ignore_errors=True)


def check_scan_tool_env() -> None:
    """启动扫描：扩充 PATH 语义下解析命令；目录注入的命令能被发现。"""
    res = T.scan_tool_env()
    assert set(res) == {"tools", "extra_dirs"}
    assert "python" in res["tools"], res  # 测试本身就跑在 python 上，必然可解析
    for name, p in res["tools"].items():
        assert Path(p).is_absolute() and Path(p).exists(), (name, p)
    # 临时常见目录里的假工具：证明 which 用的是扩充后的 PATH（注册表/常见目录），
    # 而不是进程 PATH 快照——部署机上 npm 不在快照里也能被扫出来。
    # 用白名单里本机没装的命令（deno），扫描只认 _KNOWN_TOOLS
    fake_dir = Path(tempfile.mkdtemp(prefix="scan-env-"))
    try:
        exe = fake_dir / ("deno.cmd" if sys.platform == "win32" else "deno")
        exe.write_text("echo ok\r\n" if sys.platform == "win32" else "#!/bin/sh\necho ok\n",
                       encoding="utf-8")
        if sys.platform != "win32":
            exe.chmod(0o755)
        original = T._TOOL_PATH_DIRS
        try:
            T._TOOL_PATH_DIRS = original + (str(fake_dir),)
            res2 = T.scan_tool_env()
            assert "deno" in res2["tools"], res2
            assert Path(res2["tools"]["deno"]).parent == fake_dir
            assert str(fake_dir) in [str(Path(d)) for d in res2["extra_dirs"]]
        finally:
            T._TOOL_PATH_DIRS = original
    finally:
        shutil.rmtree(fake_dir, ignore_errors=True)
    print("ok: scan_tool_env（扩充 PATH 解析/绝对路径/目录注入命令可发现）")


class _FakeKey:
    """记录 SetValueEx 的假注册表键。"""

    def __init__(self, value, vtype):
        self.value, self.vtype = value, vtype
        self.written = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def check_persist_path_dirs() -> None:
    """写用户 PATH：去重/只增不删/类型保留/windowsapps 跳过/幂等/失败不抛。"""
    if sys.platform != "win32":
        print("skip: persist_path_dirs（非 Windows）")
        return
    import sys as _sys
    import types
    fake_mod = types.ModuleType("winreg")
    fake_mod.HKEY_CURRENT_USER = 0x80000001
    fake_mod.KEY_READ = 0x20019
    fake_mod.KEY_WRITE = 0x20006
    fake_mod.REG_EXPAND_SZ = 2
    key = _FakeKey(r"%JAVA_HOME%\bin" + os.pathsep + r"C:\Program Files\nodejs", 2)
    opened = []

    def fake_openkey(hive, subkey, _reserved=0, _access=0):
        assert hive == fake_mod.HKEY_CURRENT_USER and subkey == "Environment"
        opened.append(1)
        return key

    fake_mod.OpenKey = fake_openkey
    fake_mod.QueryValueEx = lambda k, name: (k.value, k.vtype)

    def fake_setvalue(k, name, _r, vtype, data):
        k.written = (vtype, data)
        k.value = data  # 模拟真注册表：写回后重读可见（幂等断言依赖这点）

    fake_mod.SetValueEx = fake_setvalue

    real_mod = _sys.modules.get("winreg")
    _sys.modules["winreg"] = fake_mod
    real_os_name = os.name
    try:
        # 新目录追加：旧条目原样保留（含未展开的 %VAR%），类型保留
        added = T.persist_path_dirs([r"C:\Program Files\nodejs",
                                     r"C:\Tools\NewKit",
                                     r"C:\Users\x\AppData\Local\Microsoft\WindowsApps"])
        assert added == [r"C:\Tools\NewKit"], added
        vtype, data = key.written
        assert vtype == 2 and data.startswith(r"%JAVA_HOME%\bin")
        assert r"C:\Program Files\nodejs" in data and r"C:\Tools\NewKit" in data
        assert "windowsapps" not in data.lower()
        # 幂等：再跑一次不再追加
        assert T.persist_path_dirs([r"C:\Tools\NewKit"]) == []
        assert key.written[1] == data  # 没有第二次写入
        # 注册表不可用（OSError）：返回空且不抛
        def broken_open(*a, **k):
            raise OSError("access denied")
        fake_mod.OpenKey = broken_open
        assert T.persist_path_dirs([r"C:\Tools\Other"]) == []
    finally:
        if real_mod is not None:
            _sys.modules["winreg"] = real_mod
        else:
            _sys.modules.pop("winreg", None)
    print("ok: persist_path_dirs（去重/只增不删/类型保留/幂等/失败不抛）")


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):  # GBK 控制台打不出 ✅ 等字符
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    check_delete()
    check_move()
    check_multi_edit()
    check_find_files()
    check_ssrf_guard()
    check_web_fetch()
    check_scan_tool_env()
    check_persist_path_dirs()
    print("\n新工具实现级测试全部通过 ✅")
    return 0


if __name__ == "__main__":
    sys.exit(main())
