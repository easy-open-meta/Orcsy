"""端到端真实浏览器（Edge / Chrome）调试流程自检测试。

运行：uv run python tests/e2e_browser.py
"""

from __future__ import annotations

import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "."))

from orcsy.browser.discover import find_browser
from orcsy.config import LocalDebugConfig
from orcsy.debug_session import DebugSession
from orcsy.tools import ToolContext
from orcsy.tools_debug import (tool_browser_click, tool_browser_close,
                               tool_browser_console, tool_browser_navigate,
                               tool_browser_screenshot, tool_browser_snapshot,
                               tool_browser_type, tool_browser_wait)

TEST_HTML = """<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <title>Orcsy 自动化测试台</title>
</head>
<body>
    <h1>Orcsy 测试服务</h1>
    <div id="content">
        <label for="username">用户名：</label>
        <input type="text" id="username" name="username" placeholder="输入姓名" value="" />
        <button id="btn-submit" onclick="handleClick()">提交表单</button>
        <div id="result" style="display:none;">已成功提交</div>
    </div>
    <script>
        function handleClick() {
            var val = document.getElementById('username').value;
            console.log('ACTION_SUBMITTED: ' + val);
            document.getElementById('result').style.display = 'block';
        }
    </script>
</body>
</html>
"""


class TestWebHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(TEST_HTML.encode("utf-8"))

    def log_message(self, format, *args) -> None:
        pass


def test_e2e_browser_workflow() -> None:
    # 1. 探测本机是否有可用浏览器（若无则跳过并打印说明）
    try:
        exe_path, engine = find_browser("auto")
        print(f"检测到可用浏览器：{engine} -> {exe_path}")
    except FileNotFoundError as e:
        print(f"跳过 e2e_browser 测试：{e}")
        return

    # 2. 启动本地测试 Web 服务
    server = HTTPServer(("127.0.0.1", 0), TestWebHandler)
    port = server.server_port
    threading.Thread(target=server.serve_forever, daemon=True).start()

    tmp_dir = Path(tempfile.mkdtemp(prefix="orcsy-e2e-browser-")).resolve()
    artifacts_dir = tmp_dir / "artifacts"
    artifacts_dir.mkdir(parents=True, exist_ok=True)

    local_debug_cfg = LocalDebugConfig(
        browser="auto",
        headless=True,
        allowed_hosts=["localhost", "127.0.0.1"],
    )

    session = DebugSession(
        root=tmp_dir,
        local_debug=local_debug_cfg,
        effective_hosts=["localhost", "127.0.0.1"],
        artifacts_dir=artifacts_dir,
    )
    ctx = ToolContext(root=tmp_dir, session=session)

    try:
        test_url = f"http://127.0.0.1:{port}/"

        # 1. 导航测试
        nav_res = tool_browser_navigate(ctx, {"url": test_url})
        assert "已成功导航" in nav_res
        assert "Orcsy 自动化测试台" in nav_res
        print("1. browser_navigate 导航成功 ✅")

        # 2. 快照测试
        snap_res = tool_browser_snapshot(ctx, {})
        assert "Title: Orcsy 自动化测试台" in snap_res
        assert 'heading level 1 "Orcsy 测试服务"' in snap_res
        assert "textbox" in snap_res
        assert "button" in snap_res
        print("2. browser_snapshot 获取文本树快照成功 ✅")

        # 3. 输入文本测试
        type_res = tool_browser_type(ctx, {"text": "AgentTester", "ref": "e2"})
        assert "已在" in type_res
        print("3. browser_type 文本输入成功 ✅")

        # 再次获取快照，验证输入值已体现在文本树中
        snap2 = tool_browser_snapshot(ctx, {})
        assert 'value="AgentTester"' in snap2

        # 4. 点击操作测试
        click_res = tool_browser_click(ctx, {"selector": "#btn-submit"})
        assert "已成功点击" in click_res
        print("4. browser_click 模拟点击成功 ✅")

        # 5. 等待条件达成
        wait_res = tool_browser_wait(ctx, {"text": "已成功提交", "timeout_seconds": 5})
        assert "等待达成" in wait_res
        print("5. browser_wait 等待页面响应达成 ✅")

        # 6. 控制台日志捕获
        console_res = tool_browser_console(ctx, {})
        assert "ACTION_SUBMITTED: AgentTester" in console_res
        print("6. browser_console 捕获控制台日志成功 ✅")

        # 7. 截图留证
        screen_res = tool_browser_screenshot(ctx, {})
        assert "已成功保存至" in screen_res
        png_files = list(artifacts_dir.glob("*.png"))
        assert len(png_files) > 0 and png_files[0].stat().st_size > 100
        print("7. browser_screenshot 成功落盘工件目录 ✅")

        # 8. 非白名单安全拦截
        bad_nav = tool_browser_navigate(ctx, {"url": "http://192.168.1.99:3000/"})
        assert "错误" in bad_nav
        assert "白名单安全拦截" in bad_nav
        print("8. 非白名单导航被安全拦截 ✅")

        # 9. 显式关闭浏览器
        close_res = tool_browser_close(ctx, {})
        assert "已关闭" in close_res
        print("9. browser_close 主动关闭浏览器成功 ✅")

    finally:
        session.close()
        server.shutdown()
        server.server_close()
        import shutil
        shutil.rmtree(tmp_dir, ignore_errors=True)
        print("10. 资源清理完毕 ✅")


def main() -> None:
    test_e2e_browser_workflow()
    print("\nAll e2e_browser tests passed! ✨")


if __name__ == "__main__":
    main()
