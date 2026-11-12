"""执行中任务的强制停止注册表：看板置信号，执行器与工具循环轮询响应。

每个运行中的任务在 TaskExecutor.run_task 开始时登记一个 threading.Event，
结束时注销；看板 POST /api/task/stop {project, id} 只置位事件。工具循环在
轮次/工具边界检查（抛 TaskStopped 立刻收尾），run_command 等待期间按 0.2s
切片轮询并杀掉子进程——不会等命令自然跑完。
"""

from __future__ import annotations

import threading

from .logging_setup import get_logger

log = get_logger("task_control")


class TaskControls:
    """进程级注册表：调度线程写（begin/end）、Web 线程写（stop）、两边读。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._events: dict[tuple[str, str], threading.Event] = {}

    def begin(self, project: str, task_id: str) -> threading.Event:
        ev = threading.Event()
        with self._lock:
            self._events[(project, task_id)] = ev
        return ev

    def end(self, project: str, task_id: str) -> None:
        with self._lock:
            self._events.pop((project, task_id), None)

    def stop(self, project: str, task_id: str) -> bool:
        """置位停止信号；任务不在执行中返回 False（看板据此报 400）。"""
        with self._lock:
            ev = self._events.get((project, task_id))
        if ev is None:
            return False
        log.info("看板请求强制停止任务 [%s] %s", project, task_id)
        ev.set()
        return True

    def running(self) -> list[dict]:
        with self._lock:
            return [{"project": p, "task_id": t} for (p, t) in self._events]

    def clear(self) -> None:
        """全部置位并清空（测试/退出用）。"""
        with self._lock:
            items = list(self._events.values())
            self._events.clear()
        for ev in items:
            ev.set()


_CONTROLS = TaskControls()


def get_task_controls() -> TaskControls:
    return _CONTROLS


__all__ = [
    "TaskControls",
    "get_task_controls",
]

