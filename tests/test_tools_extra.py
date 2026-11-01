"""新工具实现级测试：delete_file / move_file / multi_edit / find_files / web_fetch。

直接调用工具函数（dispatch 层与开关机制在 test_tools_toggle.py 覆盖）。
web_fetch 的集成测试在本机起一次性 HTTP 服务，并 monkeypatch 掉 SSRF 守卫
（守卫本身单独直测 _assert_public_url）。
"""

from __future__ import annotations

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
        out = T.tool_web_fetch(ctx, {"url": f"http://127.0.0.1:{port}/page"})
        assert "正文段落 内容" in out and "标题" in out, out
        assert "evil()" not in out and "b{}" not in out, "script/style 应被剥离"
        assert out.startswith("[HTTP 200"), out
        out = T.tool_web_fetch(ctx, {"url": f"http://127.0.0.1:{port}/raw.txt"})
        assert "plain text body" in out, out
        # 二进制类型拒绝；404 报错文本；空 url
        assert T.tool_web_fetch(ctx, {"url": f"http://127.0.0.1:{port}/img"}
                                ).startswith("错误：不支持的内容类型"), "image/* 应拒绝"
        assert "HTTP 404" in T.tool_web_fetch(ctx, {"url": f"http://127.0.0.1:{port}/nope"})
        assert T.tool_web_fetch(ctx, {"url": ""}).startswith("错误")
        # 截断参数生效
        out = T.tool_web_fetch(ctx, {"url": f"http://127.0.0.1:{port}/raw.txt",
                                     "max_chars": 1000})
        assert "plain text body" in out
        print("ok: web_fetch（HTML 剥标签/文本原样/二进制拒绝/404/空 url）")
    finally:
        T._assert_public_url = original
        server.shutdown()
        shutil.rmtree(root, ignore_errors=True)


def main() -> int:
    check_delete()
    check_move()
    check_multi_edit()
    check_find_files()
    check_ssrf_guard()
    check_web_fetch()
    print("\n新工具实现级测试全部通过 ✅")
    return 0


if __name__ == "__main__":
    sys.exit(main())
