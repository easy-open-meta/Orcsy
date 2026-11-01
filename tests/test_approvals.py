"""人工审批机制单元测试：分类矩阵、ApprovalManager、dispatch 审批门、
配置解析与 Web 层（save_settings/save_project/full_state/approval_respond）。

风格与 test_project_llm.py 一致：main() + assert，直跑无 pytest。
"""

from __future__ import annotations

import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from orcsy.approvals import (MUTATING_TOOLS, RISKY_TOOLS, ApprovalManager,
                             requires_approval)
from orcsy.config import (AppConfig, LLMConfig, PermissionsConfig, ProjectConfig,
                          load_config, resolve_permission_mode, validate_permission_mode)
from orcsy.scheduler import Scheduler
from orcsy.tools import TOOLS, ToolContext, dispatch_tool
from orcsy.web import AppState


def check_classification() -> None:
    readonly = {"list_dir", "read_file", "grep", "find_files", "read_skill",
                "web_fetch", "finish"}
    for tool in TOOLS:
        # 完全访问：任何工具都不询问
        assert requires_approval("full_access", tool) is False
        # 只读类与 finish：任何模式都不询问
        if tool in readonly:
            assert requires_approval("ask_on_change", tool) is False
            assert requires_approval("autonomous", tool) is False
    # 变更前询问 = 全部变更类；自主模式 = 仅高风险
    assert requires_approval("ask_on_change", "write_file") is True
    assert requires_approval("ask_on_change", "edit_file") is True
    assert requires_approval("ask_on_change", "multi_edit") is True
    assert requires_approval("ask_on_change", "delete_file") is True
    assert requires_approval("ask_on_change", "move_file") is True
    assert requires_approval("ask_on_change", "run_command") is True
    assert requires_approval("autonomous", "write_file") is False
    assert requires_approval("autonomous", "edit_file") is False
    assert requires_approval("autonomous", "delete_file") is True
    assert requires_approval("autonomous", "move_file") is True
    assert requires_approval("autonomous", "run_command") is True
    # 未知模式按最宽松处理（校验层已挡非法值，运行期不因脏值卡死任务）
    assert requires_approval("bogus", "delete_file") is False
    assert RISKY_TOOLS < MUTATING_TOOLS  # 高风险是变更类的真子集
    print("ok: requires_approval 分类矩阵（3 模式 × 全部工具）")


def check_manager() -> None:
    mgr = ApprovalManager()
    # 批准：后台线程挂起请求，主线程响应
    out = {}

    def worker():
        out["r"] = mgr.request("proj", "T1", "delete_file",
                               {"path": "a.txt"}, "autonomous", 5)
    t = threading.Thread(target=worker)
    t.start()
    deadline = time.time() + 3
    while not mgr.pending() and time.time() < deadline:
        time.sleep(0.02)
    pend = mgr.pending()
    assert len(pend) == 1, pend
    p = pend[0]
    for key in ("id", "project", "task_id", "tool", "args_preview", "mode",
                "created_at", "waited_seconds"):
        assert key in p, f"pending 缺字段 {key}"
    assert p["project"] == "proj" and p["tool"] == "delete_file"
    assert "a.txt" in p["args_preview"]
    assert mgr.respond(p["id"], True, always=True) is True
    t.join(3)
    assert out["r"].approved is True and out["r"].always is True
    assert mgr.pending() == [], "响应后应摘除条目"
    # 重复响应/未知 id → False
    assert mgr.respond(p["id"], False) is False
    assert mgr.respond("no-such-id", True) is False

    # 拒绝（always 只在批准时有意义）
    def worker2():
        out["r2"] = mgr.request("proj", "T2", "run_command", ["rm"], "autonomous", 5)
    t2 = threading.Thread(target=worker2)
    t2.start()
    deadline = time.time() + 3
    while not mgr.pending() and time.time() < deadline:
        time.sleep(0.02)
    assert mgr.respond(mgr.pending()[0]["id"], False, always=True) is True
    t2.join(3)
    assert out["r2"].approved is False and out["r2"].always is False

    # 超时自动拒绝（request 内部最短 1s，测试等得起）
    r3 = mgr.request("proj", "T3", "write_file", {}, "ask_on_change", 0.1)
    assert r3.approved is False and r3.timed_out is True
    assert mgr.pending() == []
    print("ok: ApprovalManager（批准/拒绝/always 语义/超时自动拒绝/未知 id）")


def check_dispatch_gate() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="appr-gate-"))
    try:
        # 拒绝门：返回错误文本 → 工具不执行、零副作用
        deny = ToolContext(root=tmp, approval_gate=lambda n, a: "错误：用户拒绝了该操作（未执行）")
        r = dispatch_tool(deny, "write_file", {"path": "x.txt", "content": "hi"})
        assert r.startswith("错误：用户拒绝"), r
        assert not (tmp / "x.txt").exists(), "被拒操作不得产生副作用"
        # 放行门（返回 None）：正常执行
        allow = ToolContext(root=tmp, approval_gate=lambda n, a: None)
        assert dispatch_tool(allow, "write_file", {"path": "y.txt", "content": "hi"}
                             ).startswith("已创建")
        assert (tmp / "y.txt").exists()
        # 门自身异常：按拒绝处理（宁严勿松），不炸循环
        def boom(n, a):
            raise RuntimeError("审批基础设施故障")
        ctx = ToolContext(root=tmp, approval_gate=boom)
        r = dispatch_tool(ctx, "delete_file", {"path": "y.txt"})
        assert r.startswith("错误：审批系统异常"), r
        assert (tmp / "y.txt").exists(), "异常按拒绝处理，文件应还在"
        # 无门（默认 None）：与历史行为一致
        plain = ToolContext(root=tmp)
        assert dispatch_tool(plain, "delete_file", {"path": "y.txt"}).startswith("已删除")
        print("ok: dispatch_tool 审批门（拒绝零副作用/放行/门异常按拒绝/无门零变化）")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def check_config() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="appr-cfg-"))
    try:
        proj = tmp / "proj"
        proj.mkdir()
        good = tmp / "good.yaml"
        good.write_text(
            "llm:\n  api_key: k\n  model: m\n"
            "permissions:\n  mode: autonomous\n  timeout_minutes: 10\n"
            "projects:\n"
            f"  - name: p1\n    path: {proj.as_posix()}\n"
            "    permission_mode: ask_on_change\n"
            f"  - name: p2\n    path: {proj.as_posix()}\n",
            encoding="utf-8")
        cfg = load_config(good, require_api_key=False)
        assert cfg.permissions.mode == "autonomous"
        assert cfg.permissions.timeout_minutes == 10
        assert cfg.projects[0].permission_mode == "ask_on_change"
        assert cfg.projects[1].permission_mode is None
        # 生效值：项目覆盖优先，None 跟随全局
        assert resolve_permission_mode(cfg.permissions, cfg.projects[0]) == "ask_on_change"
        assert resolve_permission_mode(cfg.permissions, cfg.projects[1]) == "autonomous"
        # 缺省：full_access + 30 分钟（零配置零行为变化）
        bare = tmp / "bare.yaml"
        bare.write_text("llm:\n  api_key: k\n  model: m\n", encoding="utf-8")
        cfg2 = load_config(bare, require_api_key=False)
        assert cfg2.permissions.mode == "full_access"
        assert cfg2.permissions.timeout_minutes == 30
        # 非法值加载期即报错
        for body, why in [("permissions:\n  mode: yolo\n", "非法模式"),
                          ("permissions:\n  timeout_minutes: 0\n", "超时越界"),
                          ("permissions:\n  timeout_minutes: abc\n", "超时非整数"),
                          ("projects:\n  - name: p1\n    path: " + proj.as_posix()
                           + "\n    permission_mode: nope\n", "项目非法模式")]:
            bad = tmp / "bad.yaml"
            bad.write_text("llm:\n  api_key: k\n  model: m\n" + body, encoding="utf-8")
            try:
                load_config(bad, require_api_key=False)
                raise AssertionError(f"应拒绝：{why}")
            except ValueError:
                pass
        assert validate_permission_mode("autonomous") == "autonomous"
        print("ok: config（permissions 解析/缺省/项目覆盖/非法值加载期报错）")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def check_web_layer() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="appr-web-"))
    try:
        cfg_file = tmp / "config.yaml"
        cfg_file.write_text("llm: {}\n", encoding="utf-8")
        proj_dir = tmp / "proj"
        proj_dir.mkdir()
        cfg = AppConfig(
            llm=LLMConfig(api_key="fake", base_url="http://localhost:9", model="fake"),
            source_path=cfg_file)
        state = AppState(cfg, Scheduler(cfg))

        # full_state 曝光：permissions_cfg + approvals + 目录
        fs = state.full_state()
        assert fs["permissions_cfg"] == {"mode": "full_access", "timeout_minutes": 30}
        assert fs["approvals"] == []

        # 全局保存：合法值生效并落盘；非法值 400 且不污染
        r = state.save_settings({"permissions": {"mode": "ask_on_change",
                                                 "timeout_minutes": 15}})
        assert r["ok"] is True
        assert state.config.permissions == PermissionsConfig("ask_on_change", 15)
        for bad in ({"mode": "yolo"}, {"timeout_minutes": 0}, {"timeout_minutes": "x"}):
            try:
                state.save_settings({"permissions": bad})
                raise AssertionError(f"应拒绝：{bad}")
            except ValueError:
                pass
        assert state.config.permissions == PermissionsConfig("ask_on_change", 15)

        # 项目级覆盖：保存/回显/生效值/清除
        r = state.save_project({"name": "proj1", "path": str(proj_dir),
                                "permission_mode": "autonomous"})
        assert r["ok"] is True
        fs = state.full_state()
        p = fs["projects"][0]
        assert p["permission_mode"] == "autonomous"
        assert p["permission_mode_effective"] == "autonomous"  # 项目覆盖全局
        r = state.save_project({"name": "proj1", "path": str(proj_dir),
                                "original_name": "proj1", "permission_mode": None})
        assert r["ok"] is True
        p = state.full_state()["projects"][0]
        assert p["permission_mode"] is None
        assert p["permission_mode_effective"] == "ask_on_change"  # 跟随全局
        try:
            state.save_project({"name": "proj1", "path": str(proj_dir),
                                "original_name": "proj1", "permission_mode": "nope"})
            raise AssertionError("应拒绝非法项目模式")
        except ValueError:
            pass

        # 持久化 roundtrip
        state.save_project({"name": "proj1", "path": str(proj_dir),
                            "original_name": "proj1", "permission_mode": "autonomous"})
        disk = load_config(cfg_file, require_api_key=False)
        assert disk.permissions.mode == "ask_on_change"
        assert disk.permissions.timeout_minutes == 15
        assert disk.projects[0].permission_mode == "autonomous"

        # approval_respond：未知 id → ValueError（Web 层转 400）
        try:
            state.approval_respond({"id": "nope", "approved": True})
            raise AssertionError("未知审批 id 应报错")
        except ValueError as e:
            assert "不存在或已处理" in str(e)
        try:
            state.approval_respond({"approved": True})
            raise AssertionError("缺 id 应报错")
        except ValueError:
            pass
        print("ok: Web 层（save_settings/save_project/full_state/roundtrip/approval_respond）")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main() -> int:
    check_classification()
    check_manager()
    check_dispatch_gate()
    check_config()
    check_web_layer()
    print("\n人工审批机制单元测试全部通过 ✅")
    return 0


if __name__ == "__main__":
    sys.exit(main())
