"""DebugSession 调试会话容器自检测试。

运行：uv run python tests/test_debug_session.py
"""

from __future__ import annotations

import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "."))

from orcsy.debug_session import DebugSession


def test_session_lifecycle_and_idempotence() -> None:
    tmp_dir = Path(tempfile.mkdtemp(prefix="orcsy-test-session-")).resolve()
    session = DebugSession(
        root=tmp_dir,
        allowed_commands=["python*"],
        effective_hosts=["localhost", "127.0.0.1"],
    )

    try:
        # 1. 获取 http_client
        client = session.http_client
        assert client is not None and not client.is_closed

        # 2. 启动一个后台常驻进程
        res = session.services.start(
            "sleep_proc",
            [sys.executable, "-c", "import time; time.sleep(60)"],
        )
        assert res["name"] == "sleep_proc"
        proc = session.services._services["sleep_proc"].proc
        assert proc.poll() is None  # 进程正在运行

        # 3. 关闭 session
        session.close()
        time.sleep(0.5)

        # 验证 http_client 已关闭
        assert client.is_closed

        # 验证后台服务已被安全杀灭
        assert proc.poll() is not None

        # 4. 幂等性验证：再次调用 close() 不报错
        session.close()

        # 5. 会话关闭后尝试获取 http_client 应抛出异常
        try:
            _ = session.http_client
            assert False, "会话关闭后应拒绝提供 http_client"
        except RuntimeError:
            pass

        print("DebugSession 生命周期管理与 close() 幂等性验证 ✅")
    finally:
        session.close()
        import shutil
        shutil.rmtree(tmp_dir, ignore_errors=True)


def main() -> None:
    test_session_lifecycle_and_idempotence()
    print("\nAll DebugSession tests passed! ✨")


if __name__ == "__main__":
    main()
