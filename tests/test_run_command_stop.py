"""run_command 强制停止 / 超时收尾回归测试。

背景 bug：超时路径只 kill 直接子进程后调用**无界** communicate()——Windows 上
孙进程继承 stdout/stderr 管道句柄，父进程死后 EOF 永不来，执行线程永久挂起，
停止信号再无人检查（看板「扫描执行中」卡死数小时的根因）。修复后：整树终止
（taskkill /T /F）+ 收尾 communicate 一律带界。
"""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from orcsy import tools as T
from orcsy.tools import ToolContext


def _ctx_with_stop(tmp: Path) -> tuple[ToolContext, threading.Event]:
    stop = threading.Event()
    ctx = ToolContext(root=tmp)
    ctx.stop_event = stop
    return ctx, stop


def test_timeout_path_terminates_tree_and_returns(tmp_path) -> None:
    """命令超时后收尾必须返回（即使孙进程握着管道），不能永久挂起。"""
    # powershell 派生一个长活孙进程并原地等待：孙进程继承 stdout/stderr 管道，
    # 老实现 kill 掉 powershell 后 EOF 等不到 → 无界 communicate 挂死
    script = (
        "Start-Process -WindowStyle Hidden -FilePath python "
        "-ArgumentList '-c','import time; time.sleep(600)' | Out-Null; "
        "Start-Sleep 600"
    )
    ctx, _ = _ctx_with_stop(tmp_path)
    started = time.monotonic()
    out = T.tool_run_command(ctx, {"command": ["powershell", "-NoProfile", "-Command", script],
                                   "timeout_seconds": 2})
    elapsed = time.monotonic() - started
    assert "超时" in out, out
    # 修复前：这里会挂到孙进程 600s 退出才返回；修复后秒级收尾
    assert elapsed < 30, f"超时收尾耗时 {elapsed:.1f}s——疑似仍挂在无界 communicate"


def test_stop_path_kills_process_and_returns(tmp_path) -> None:
    """等待期间置位停止信号：进程树被终止，立刻抛 TaskStopped。"""
    ctx, stop = _ctx_with_stop(tmp_path)
    # 后台置位：让 run_command 先进入等待循环
    def set_stop() -> None:
        time.sleep(1.0)
        stop.set()
    threading.Thread(target=set_stop, daemon=True).start()
    started = time.monotonic()
    try:
        T.tool_run_command(ctx, {"command": ["python", "-c", "import time; time.sleep(600)"],
                                 "timeout_seconds": 120})
        raise AssertionError("置位停止信号后应抛 TaskStopped")
    except T.TaskStopped:
        pass
    assert time.monotonic() - started < 15


def test_kill_process_tree_helper_ends_windows_tree(tmp_path) -> None:
    """_kill_process_tree：对派生孙进程的父子链也要能整树终止。"""
    import subprocess
    # 起一个会再派生 sleep 孙进程的子进程，拿到孙进程 pid 一起验证
    proc = subprocess.Popen(
        ["powershell", "-NoProfile", "-Command",
         "Start-Process -WindowStyle Hidden -FilePath python "
         "-ArgumentList '-c','import time; time.sleep(600)'; Start-Sleep 600"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    try:
        time.sleep(1.5)  # 留出孙进程派生时间
        T._kill_process_tree(proc)
        try:
            proc.communicate(timeout=10)
        except Exception:
            pass
        assert proc.poll() is not None, "子进程未被终止"
    finally:
        if proc.poll() is None:
            proc.kill()


if __name__ == "__main__":
    import tempfile
    t1 = Path(tempfile.mkdtemp(prefix="rc-stop-"))
    test_timeout_path_terminates_tree_and_returns(t1)
    print("ok test_timeout_path_terminates_tree_and_returns")
    t2 = Path(tempfile.mkdtemp(prefix="rc-stop-"))
    test_stop_path_kills_process_and_returns(t2)
    print("ok test_stop_path_kills_process_and_returns")
    t3 = Path(tempfile.mkdtemp(prefix="rc-stop-"))
    test_kill_process_tree_helper_ends_windows_tree(t3)
    print("ok test_kill_process_tree_helper_ends_windows_tree")
