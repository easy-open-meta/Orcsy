"""DevServerManager 开发服务管理器自检测试。

运行：uv run python tests/test_devserver.py
"""

from __future__ import annotations

import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "."))

import httpx
from orcsy.devserver import DevServerManager, find_free_port


def test_free_port() -> None:
    p1 = find_free_port()
    p2 = find_free_port()
    assert isinstance(p1, int) and p1 > 1024
    assert isinstance(p2, int) and p2 > 1024
    print("find_free_port 正常分配端口 ✅")


def test_devserver_lifecycle() -> None:
    tmp_dir = Path(tempfile.mkdtemp(prefix="orcsy-test-devserver-")).resolve()
    mgr = DevServerManager(root=tmp_dir, allowed_commands=["python*"], max_services=2)

    try:
        # 1. 启动一个真实的 python -m http.server
        res = mgr.start(
            "web",
            [sys.executable, "-m", "http.server", "{PORT}"],
            ready_url="http://127.0.0.1:{PORT}/",
            ready_timeout_seconds=10,
        )
        assert res["name"] == "web"
        port = res["port"]
        assert isinstance(port, int) and port > 0
        base_url = res["base_url"]
        assert f":{port}" in base_url

        # 验证服务确实可访问
        resp = httpx.get(f"{base_url}/", timeout=2.0)
        assert resp.status_code == 200

        # 2. 读取日志
        time.sleep(0.5)
        logs = mgr.logs("web")
        assert "web" in logs
        assert "正在运行中" in logs

        # 3. 增量读日志
        inc_logs = mgr.logs("web", since_last=True)
        assert "暂无新日志" in inc_logs or inc_logs.strip()

        # 4. 重复同名服务应拒绝
        try:
            mgr.start("web", [sys.executable, "-v"])
            assert False, "应拒绝重复同名服务"
        except ValueError as e:
            assert "已在运行" in str(e)

        # 5. 停止服务
        stop_msg = mgr.stop("web")
        assert "已停止" in stop_msg
        assert "web" not in mgr._services

        print("DevServerManager 真实服务生命周期与日志读取 ✅")
    finally:
        mgr.close()
        import shutil
        shutil.rmtree(tmp_dir, ignore_errors=True)


def test_devserver_security_and_limits() -> None:
    tmp_dir = Path(tempfile.mkdtemp(prefix="orcsy-test-devserver-sec-")).resolve()
    mgr = DevServerManager(root=tmp_dir, allowed_commands=["python*"], max_services=1)

    try:
        # 非白名单命令
        try:
            mgr.start("bad", ["unknown_cmd_123"])
            assert False, "应拦截非白名单命令"
        except ValueError as e:
            assert "白名单" in str(e)

        # cwd 越界
        try:
            mgr.start("bad_cwd", [sys.executable, "-v"], cwd_rel="../../..")
            assert False, "应拦截越界目录"
        except ValueError as e:
            assert "越界" in str(e)

        # 启动一个占位服务达到上限
        mgr.start("s1", [sys.executable, "-c", "import time; time.sleep(30)"])
        try:
            mgr.start("s2", [sys.executable, "-c", "import time; time.sleep(30)"])
            assert False, "应拦截超出 max_services"
        except ValueError as e:
            assert "上限" in str(e)

        print("DevServerManager 安全拦截与上限保护 ✅")
    finally:
        mgr.close()
        import shutil
        shutil.rmtree(tmp_dir, ignore_errors=True)


def main() -> None:
    test_free_port()
    test_devserver_lifecycle()
    test_devserver_security_and_limits()
    print("\nAll DevServerManager tests passed! ✨")


if __name__ == "__main__":
    main()
