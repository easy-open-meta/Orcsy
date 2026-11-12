"""开发服务管理器：在后台启动被测 Web / API 服务，提供端口分配、日志缓冲与安全停机。

设计要点：
1. 端口自动分配：避免与宿主机现有服务或其他并发任务冲突；命令行与环境变量中的 `{PORT}` 自动替换；
2. 命令白名单：与 run_command 共用项目命令白名单，cwd 严格禁锢在项目根目录内；
3. 输出环形缓冲：合并 stdout/stderr 异步读入 deque(maxlen=2000)，方便 Agent 查看最新日志；
4. 健康就绪探针：支持轮询探测 HTTP 接口就绪，支持看板强停信号中途打断；
5. 生命周期安全：任务结束或异常中断时，强制调用 close() 杀灭整棵进程树，杜绝孤儿进程。
"""

from __future__ import annotations

import collections
import os
import re
import socket
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .logging_setup import get_logger
from .tools import (
    _NO_WINDOW,
    _decode_proc_output,
    _kill_process_tree,
    _resolve_new_install,
    _windows_startupinfo,
    check_command_allowed,
    register_child_proc,
    unregister_child_proc,
)

log = get_logger("devserver")

_NAME_RE = re.compile(r"^[\w][\w\-]*$")


def find_free_port() -> int:
    """寻找本地一个可用的 TCP 空闲端口。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        s.listen(1)
        port = s.getsockname()[1]
        return int(port)


@dataclass
class ServiceInstance:
    """一个后台服务的运行实例。"""

    name: str
    command: list[str]
    cwd: Path
    port: int
    proc: subprocess.Popen
    started_at: float = field(default_factory=time.time)
    logs: collections.deque[str] = field(default_factory=lambda: collections.deque(maxlen=2000))
    last_read_len: int = 0
    read_thread: threading.Thread | None = None


class DevServerManager:
    """单个任务生命周期内的后台服务管理器。"""

    def __init__(self, root: Path, allowed_commands: list[str] | None = None,
                 max_services: int = 3, extra_env: dict[str, str] | None = None) -> None:
        self.root = root.resolve()
        self.allowed_commands = allowed_commands
        self.max_services = max(1, min(int(max_services or 3), 10))
        self.extra_env = extra_env
        self._services: dict[str, ServiceInstance] = {}
        self._lock = threading.Lock()

    def start(self, name: str, command: list[str], *, cwd_rel: str = ".",
              ready_url: str = "", ready_timeout_seconds: int = 60,
              custom_env: dict[str, str] | None = None,
              stop_event: object | None = None) -> dict[str, Any]:
        """后台启动一个开发服务。

        返回 {"name": name, "port": port, "base_url": f"http://127.0.0.1:{port}", "pid": pid}
        """
        name = str(name or "").strip()
        if not name or not _NAME_RE.match(name):
            raise ValueError(f"服务名称非法：'{name}'（仅限字母、数字、下划线与中划线）")

        with self._lock:
            # 清理已经退出的死服务条目
            dead = [n for n, s in self._services.items() if s.proc.poll() is not None]
            for n in dead:
                self._stop_instance(self._services.pop(n))

            if name in self._services:
                raise ValueError(f"服务 '{name}' 已在运行中，请先停止它或使用其他名称")
            if len(self._services) >= self.max_services:
                raise ValueError(
                    f"后台服务数量已达上限（最多 {self.max_services} 个），请先调用 dev_server_stop 停止不用的服务"
                )

        if not command or not isinstance(command, (list, tuple)):
            raise ValueError("command 必须是非空字符串数组")
        cmd_strs = [str(c) for c in command]

        # 白名单检查
        ok, err = check_command_allowed(cmd_strs, self.allowed_commands)
        if not ok:
            raise ValueError(err)

        # cwd 解析与越界检查
        p_cwd = Path(cwd_rel or ".")
        if p_cwd.is_absolute():
            resolved_cwd = p_cwd.resolve()
        else:
            resolved_cwd = (self.root / p_cwd).resolve()
        if resolved_cwd != self.root and self.root not in resolved_cwd.parents:
            raise ValueError(f"工作目录越界，超出项目根目录：{cwd_rel}")
        if not resolved_cwd.is_dir():
            raise ValueError(f"工作目录不存在：{cwd_rel}")

        # 端口分配
        port = find_free_port()

        # 替换 command 中的 {PORT} 占位符
        final_cmd = [c.replace("{PORT}", str(port)) for c in cmd_strs]
        final_cmd = _resolve_new_install(final_cmd)

        # 构建子进程环境变量
        sub_env = dict(os.environ)
        # 继承扩充的工具链 PATH
        from .tools import _tool_path_dirs
        extra = os.pathsep.join(_tool_path_dirs())
        if extra:
            sub_env["PATH"] = sub_env.get("PATH", "") + os.pathsep + extra
        if self.extra_env:
            sub_env.update(self.extra_env)
        if custom_env:
            sub_env.update({str(k): str(v) for k, v in custom_env.items()})
        sub_env["PORT"] = str(port)

        log.info("启动后台服务 [%s] (port=%d, cwd=%s): %s", name, port, resolved_cwd, final_cmd)

        try:
            # 输出按字节捕获：服务进程可能是原生命令（GBK 输出中文路径）也可能是
            # Node/Python（utf-8 输出），日志线程逐行自适应解码（utf-8 → ANSI 回退）
            proc = subprocess.Popen(
                final_cmd,
                cwd=str(resolved_cwd),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=False,
                shell=False,
                env=sub_env,
                startupinfo=_windows_startupinfo(),
                creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | _NO_WINDOW,
            )
        except FileNotFoundError:
            raise ValueError(f"命令不存在或不在 PATH：{final_cmd[0]}") from None

        register_child_proc(proc)

        instance = ServiceInstance(
            name=name,
            command=final_cmd,
            cwd=resolved_cwd,
            port=port,
            proc=proc,
        )

        # 启动日志抓取线程
        def _read_logs() -> None:
            try:
                assert proc.stdout is not None
                for raw in proc.stdout:
                    instance.logs.append(_decode_proc_output(raw).rstrip("\r\n"))
            except Exception:
                pass

        t = threading.Thread(target=_read_logs, name=f"devserver-{name}", daemon=True)
        t.start()
        instance.read_thread = t

        with self._lock:
            self._services[name] = instance

        # 如果指定了就绪探测 ready_url
        if ready_url:
            actual_ready_url = ready_url.replace("{PORT}", str(port))
            if not actual_ready_url.startswith(("http://", "https://")):
                actual_ready_url = f"http://127.0.0.1:{port}" + (
                    actual_ready_url if actual_ready_url.startswith("/") else f"/{actual_ready_url}"
                )
            log.info("等待服务 [%s] 就绪：%s (超时 %ds)", name, actual_ready_url, ready_timeout_seconds)
            self._wait_ready(instance, actual_ready_url, ready_timeout_seconds, stop_event)

        return {
            "name": name,
            "port": port,
            "base_url": f"http://127.0.0.1:{port}",
            "pid": proc.pid,
        }

    def _wait_ready(self, instance: ServiceInstance, url: str,
                    timeout_seconds: int, stop_event: object | None) -> None:
        """轮询就绪探针，期间监听进程存活与强停信号。"""
        import httpx

        deadline = time.monotonic() + max(1, timeout_seconds)
        while time.monotonic() < deadline:
            if stop_event is not None and getattr(stop_event, "is_set", lambda: False)():
                self.stop(instance.name)
                from .tools import TaskStopped
                raise TaskStopped()

            # 进程如果提前挂了，立刻报错返回日志
            code = instance.proc.poll()
            if code is not None:
                recent_logs = list(instance.logs)[-20:]
                log_snippet = "\n".join(recent_logs)
                self.stop(instance.name)
                raise RuntimeError(
                    f"服务 '{instance.name}' 启动后异常退出（退出码 {code}）。\n输出日志摘要：\n{log_snippet or '（无日志）'}"
                )

            try:
                resp = httpx.get(url, timeout=1.0)
                if 200 <= resp.status_code < 400:
                    log.info("服务 [%s] 就绪探测成功 (HTTP %d)", instance.name, resp.status_code)
                    return
            except Exception:
                pass
            time.sleep(0.3)

        # 超时
        recent_logs = list(instance.logs)[-20:]
        log_snippet = "\n".join(recent_logs)
        self.stop(instance.name)
        raise TimeoutError(
            f"服务 '{instance.name}' 就绪探测超时（>{timeout_seconds}s，探针：{url}）。\n输出日志摘要：\n{log_snippet or '（无日志）'}"
        )

    def logs(self, name: str, tail: int = 80, since_last: bool = False) -> str:
        """获取指定服务的最新日志。"""
        name = str(name or "").strip()
        with self._lock:
            instance = self._services.get(name)
        if not instance:
            return f"错误：未找到名为 '{name}' 的服务。当前运行的服务：{', '.join(self._services) or '（无）'}"

        code = instance.proc.poll()
        status_line = (
            f"状态：已退出（退出码 {code}）" if code is not None else "状态：正在运行中"
        )

        all_logs = list(instance.logs)
        if since_last:
            start_idx = instance.last_read_len
            instance.last_read_len = len(all_logs)
            lines = all_logs[start_idx:]
        else:
            n = max(1, min(int(tail or 80), 500))
            lines = all_logs[-n:]
            instance.last_read_len = len(all_logs)

        header = f"[{name} · port={instance.port} · {status_line}]\n"
        body = "\n".join(lines)
        return header + (body if body else "（暂无新日志）")

    def stop(self, name: str | None = None) -> str:
        """停止指定服务（或停止全部服务）。"""
        name = str(name or "").strip() if name else None
        with self._lock:
            if name:
                instance = self._services.pop(name, None)
                if not instance:
                    return f"服务 '{name}' 不存在或已停止"
                self._stop_instance(instance)
                return f"已停止服务 '{name}'"
            # 停止全部
            instances = list(self._services.values())
            self._services.clear()

        for inst in instances:
            self._stop_instance(inst)
        return f"已停止所有服务（共 {len(instances)} 个）"

    def _stop_instance(self, instance: ServiceInstance) -> None:
        """安全终止单个服务的进程树。"""
        log.info("停止后台服务 [%s] (pid=%s)", instance.name, instance.proc.pid)
        unregister_child_proc(instance.proc)
        _kill_process_tree(instance.proc)

    def close(self) -> None:
        """关闭并注销所有服务（任务结束收尾）。"""
        self.stop(None)
