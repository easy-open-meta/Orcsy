"""镜像源管理自检：配置校验/落盘、HTTP 测速引擎（本地假源）、优选排序、
应用最快源到 env.vars、env-setup 技能动态装饰、speedtest_mirrors 工具与
Web 端点（mirror_test / mirror_apply）。

风格与 test_env_vars.py 一致：main() + assert，直跑无 pytest。
测速一律打本地假镜像源（内置真实源全部禁用）——不依赖外网、毫秒级稳定。
"""

from __future__ import annotations

import re
import shutil
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from orcsy.approvals import requires_approval
from orcsy.config import AppConfig, LLMConfig, load_config
from orcsy.mirrors import (BUILTIN_MIRRORS, MirrorRuntime, decorate_skill_body,
                           speedtest_many, speedtest_one, validate_mirrors)
from orcsy.scheduler import Scheduler
from orcsy.tools import TOOLS, TOGGLEABLE_TOOLS, ToolContext, dispatch_tool
from orcsy.web import AppState


class _FakeMirrorHandler(BaseHTTPRequestHandler):
    """假镜像源：遵守 Range 返回 206+前 N 字节（与真实 CDN 行为一致）；
    /dead 路径 404（模拟防盗链拦截）。"""

    def do_GET(self):  # noqa: N802
        if self.path.startswith("/dead"):
            # 干净的 404（空 body）：send_error 会带 HTML 体，客户端不读即关
            # 会发 RST，污染后续回环连接（真实防盗链镜像多为空体拒绝）
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        m = re.match(r"bytes=0-(\d+)", self.headers.get("Range", ""))
        body = b"x" * (min(int(m.group(1)) + 1, 4096) if m else 2048)
        self.send_response(206 if m else 200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except OSError:
            pass  # 客户端可能提前关闭连接

    def log_message(self, *a):  # 静默访问日志
        pass


def _start_fake() -> tuple[ThreadingHTTPServer, str]:
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _FakeMirrorHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


def _flaky_retry(fn, ok, what: str, tries: int = 4):
    """Windows 回环偶发连接重置（本项目已知环境怪癖，e2e 同款处理）：
    网络类断言整体重试几次，避免环境抖动误报产品问题。"""
    last = None
    for _ in range(tries):
        last = fn()
        if ok(last):
            return last
    raise AssertionError(f"{what}：重试 {tries} 次仍失败 → {last}")


_ALL_BUILTIN_URLS = [m.url for rows in BUILTIN_MIRRORS.values() for m in rows]


def check_validate() -> None:
    assert validate_mirrors(None) == {"custom": {}, "disabled": []}
    ok = validate_mirrors({"custom": {"goproxy": ["https://a.example/", "https://a.example/"]},
                           "disabled": ["https://goproxy.io"]})
    assert ok["custom"] == {"goproxy": ["https://a.example/"]}  # 去重保序
    assert ok["disabled"] == ["https://goproxy.io"]
    for bad, why in [({"custom": {"xxx": ["https://a"]}}, "未知类别"),
                     ({"custom": {"goproxy": "https://a"}}, "非列表"),
                     ({"custom": {"goproxy": ["ftp://a"]}}, "非法 scheme"),
                     ({"custom": {"goproxy": ["https://a b"]}}, "含空白"),
                     ({"custom": {"goproxy": ["notaurl"]}}, "非 URL"),
                     ({"disabled": {"a": 1}}, "disabled 非列表"),
                     (["x"], "非字典")]:
        try:
            validate_mirrors(bad)
            raise AssertionError(f"应拒绝：{why}")
        except ValueError:
            pass
    print("ok: validate_mirrors（类别/URL 校验/去重/非法拒绝）")


def check_speed_engine() -> None:
    srv, base = _start_fake()
    try:
        ms = speedtest_one(base + "/mirror/")
        assert isinstance(ms, float) and ms >= 0, ms
        assert speedtest_one(base + "/dead") is None          # 404 → 失败
        assert speedtest_one("http://127.0.0.1:1/x", timeout=1.0) is None  # 拒连
        r = _flaky_retry(
            lambda: speedtest_many([(base + "/a", ""), (base + "/b", ""),
                                    ("http://127.0.0.1:1/x", "")]),
            lambda r: r[base + "/a"] is not None and r[base + "/b"] is not None,
            "speedtest_many 并发测速")
        assert r["http://127.0.0.1:1/x"] is None
        assert speedtest_many([]) == {}
        print("ok: 测速引擎（本地假源 ms/404 与拒连判失败/并发批量/空表）")
    finally:
        srv.shutdown()


def check_runtime_and_preferred() -> None:
    srv, base = _start_fake()
    tmp = Path(tempfile.mkdtemp(prefix="mirror-rt-"))
    try:
        fast, slow = base + "/fast", base + "/slow"
        rt = MirrorRuntime(custom={"goproxy": [fast, slow]},
                           disabled=_ALL_BUILTIN_URLS,   # 排除真实网络源：测试稳定
                           speeds_path=tmp / "mirror_speeds.json")
        rows = rt.sources("goproxy")["goproxy"]
        # sources 保留禁用行（带 enabled=False 标记，供 UI 展示开关）
        assert [r["url"] for r in rows if r["enabled"]] == [fast, slow]
        assert all(not r["enabled"] for r in rows if r["builtin"])  # 内置全被禁用
        # 与内置重复的 custom URL：展示层去重（内置行保留、custom 行不重复出现）
        rt2 = MirrorRuntime(custom={"goproxy": ["https://goproxy.cn"]})
        assert sum(1 for r in rt2.sources("goproxy")["goproxy"]
                   if r["url"] == "https://goproxy.cn") == 1
        # 未测速：preferred 保持默认顺序
        assert [r["url"] for r in rt.preferred("goproxy")] == [fast, slow]
        # 实测（本地假源），再用受控数值断言排序：快 → 慢 → 未测 → 失败
        results = _flaky_retry(
            lambda: rt.run_speedtest("goproxy"),
            lambda r: r.get(fast) is not None and r.get(slow) is not None,
            "run_speedtest 本地假源")
        untested = base + "/untested"
        rt.custom["goproxy"].append(untested)
        rt.save_speeds({fast: 500.0, slow: 10.0, "https://goproxy.io": None})
        order = [r["url"] for r in rt.preferred("goproxy")]
        assert order == [slow, fast, untested], order  # 失败源被禁用不在列
        # 边车持久化 + 合并语义（单类别测速不抹掉其它类别旧结果）
        rt.save_speeds({base + "/npmx": 5.0})
        loaded = rt.load_speeds()
        assert loaded["results"][slow] == 10.0 and loaded["results"][base + "/npmx"] == 5.0
        assert loaded["tested_at"]
        # 坏文件容错：按未测速处理
        (tmp / "mirror_speeds.json").write_text("{broken", encoding="utf-8")
        assert rt.load_speeds() == {"tested_at": "", "results": {}}
        # apply_fastest：只取测速成功的 proxy 类；GOPROXY 追加 ",direct"
        rt.save_speeds({slow: 10.0, fast: 500.0})
        applied = rt.apply_fastest()
        assert applied == {"GOPROXY": slow + ",direct"}, applied
        print("ok: MirrorRuntime（清单合并/去重/优选排序/边车合并与容错/应用最快）")
    finally:
        srv.shutdown()
        shutil.rmtree(tmp, ignore_errors=True)


def check_decorate() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="mirror-dec-"))
    try:
        rt = MirrorRuntime(speeds_path=tmp / "s.json")
        body = "# 静态正文"
        assert decorate_skill_body("env-setup", body, rt) == body   # 无结果不装饰
        assert decorate_skill_body("other-skill", body, rt) == body  # 其它技能不动
        assert decorate_skill_body("env-setup", body, None) == body  # runtime None 安全
        rt.save_speeds({"https://goproxy.cn": 120.0,
                        "https://proxy.golang.org": None,
                        "https://mirrors.aliyun.com/golang/": 300.0})
        out = decorate_skill_body("env-setup", body, rt)
        assert out.startswith(body) and "镜像源测速优选" in out
        assert "https://goproxy.cn（120ms）" in out, out[-400:]
        section = out.split("镜像源测速优选", 1)[1]
        assert "proxy.golang.org" not in section   # 测速失败的源不进推荐
        assert "speedtest_mirrors" in section      # 指引模型可重测
        print("ok: 技能正文动态优选段（无结果不装饰/失败源不推荐/ms 格式/工具指引）")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def check_tool_and_web() -> None:
    srv, base = _start_fake()
    tmp = Path(tempfile.mkdtemp(prefix="mirror-web-"))
    try:
        # 工具注册与审批属性：可开关；纯探测非 mutating，各模式都不挂起
        assert "speedtest_mirrors" in TOOLS and "speedtest_mirrors" in TOGGLEABLE_TOOLS
        assert requires_approval("ask_on_change", "speedtest_mirrors") is False
        assert requires_approval("autonomous", "speedtest_mirrors") is False

        cfg_file = tmp / "config.yaml"
        cfg_file.write_text("llm: {}\n", encoding="utf-8")
        cfg = AppConfig(llm=LLMConfig(api_key="fake", base_url="http://localhost:9",
                                      model="fake"), source_path=cfg_file)
        state = AppState(cfg, Scheduler(cfg))
        # 禁用全部内置源（测速只打本地假源，不依赖外网）
        state.save_settings({"mirrors": {
            "custom": {"goproxy": [base + "/g"], "pip": [base + "/p"]},
            "disabled": _ALL_BUILTIN_URLS}})
        # 工具 dispatch：单类别 / 全部 / 非法类别 / runtime 未接线
        ctx = ToolContext(root=tmp, mirror_runtime=state.mirror_runtime)
        out = _flaky_retry(
            lambda: dispatch_tool(ctx, "speedtest_mirrors", {"category": "goproxy"}),
            lambda o: base + "/g" in o and "失败" not in o,
            "speedtest_mirrors 单类别")
        assert "测速完成" in out and "ms" in out, out
        out_all = _flaky_retry(
            lambda: dispatch_tool(ctx, "speedtest_mirrors", {}),
            lambda o: base + "/p" in o and "失败" not in o,
            "speedtest_mirrors 全类别")
        assert "pip index" in out_all, out_all[:400]
        assert "未知镜像类别" in dispatch_tool(ctx, "speedtest_mirrors",
                                             {"category": "nope"})
        assert "镜像源运行时不可用" in dispatch_tool(ToolContext(root=tmp),
                                                    "speedtest_mirrors", {})
        # 边车落盘在配置目录（与 config.yaml 同级）
        assert (tmp / "mirror_speeds.json").is_file()
        # Web 端点：测速（含非法类别 400）与应用最快源
        r = _flaky_retry(
            lambda: state.mirror_test({"category": "pip"}),
            lambda r: r["results"].get(base + "/p") is not None,
            "mirror_test pip 类别")
        assert r["ok"]
        try:
            state.mirror_test({"category": "bad"})
            raise AssertionError("应拒绝非法类别")
        except ValueError:
            pass
        r = state.mirror_apply({})
        assert r["ok"], r
        assert r["applied"] == {"GOPROXY": base + "/g,direct",
                                "PIP_INDEX_URL": base + "/p"}, r["applied"]
        # 落盘 roundtrip：env.vars 与 mirrors 都持久化
        disk = load_config(cfg_file, require_api_key=False)
        assert disk.env.vars["GOPROXY"] == base + "/g,direct"
        assert disk.env.vars["PIP_INDEX_URL"] == base + "/p"
        assert disk.mirrors.custom["goproxy"] == [base + "/g"]
        assert disk.mirrors.disabled == _ALL_BUILTIN_URLS
        fs = state.full_state()
        assert fs["env_cfg"]["PIP_INDEX_URL"] == base + "/p"
        assert fs["mirror_speeds"]["results"][base + "/g"] is not None
        assert len(fs["mirror_categories"]) == 7
        # 无测速结果时 apply 拒绝（不瞎写 env.vars）
        cfg2_file = tmp / "c2.yaml"
        cfg2_file.write_text("llm: {}\n", encoding="utf-8")
        cfg2 = AppConfig(llm=cfg.llm, source_path=cfg2_file)
        st2 = AppState(cfg2, Scheduler(cfg2))
        try:
            st2.mirror_apply({})
            raise AssertionError("应拒绝无测速结果的应用")
        except ValueError as e:
            assert "测速" in str(e)
        print("ok: speedtest_mirrors 工具 + mirror_test/mirror_apply 端点 + 配置落盘")
    finally:
        srv.shutdown()
        shutil.rmtree(tmp, ignore_errors=True)


def check_executor_decoration() -> None:
    """执行器接线：有测速结果时 env-setup 正文进 ToolContext 前已被装饰。"""
    tmp = Path(tempfile.mkdtemp(prefix="mirror-exe-"))
    try:
        cfg_file = tmp / "config.yaml"
        cfg_file.write_text("llm: {}\n", encoding="utf-8")
        cfg = AppConfig(llm=LLMConfig(api_key="k", base_url="http://localhost:9",
                                      model="m"), source_path=cfg_file)
        sched = Scheduler(cfg)
        assert sched.mirror_runtime is not None
        # 边车写入结果后，agent 执行链的 decorate 在 _agent_loop 内生效——
        # 这里直接验证装饰函数 + runtime 组合（_agent_loop 需要 LLM，不在此测）
        sched.mirror_runtime.save_speeds({"https://goproxy.cn": 88.0})
        body = decorate_skill_body("env-setup", "# 技能正文", sched.mirror_runtime)
        assert "镜像源测速优选" in body and "88ms" in body
        # reload_config 后 runtime 重建（custom/disabled 变化生效）
        cfg.mirrors.custom = {"npm": ["https://npm.internal/"]}
        sched.reload_config(cfg)
        assert "https://npm.internal/" in [
            r["url"] for r in sched.mirror_runtime.sources("npm")["npm"]]
        print("ok: 执行器/调度器接线（runtime 构造/装饰/热重载重建）")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main() -> int:
    check_validate()
    check_speed_engine()
    check_runtime_and_preferred()
    check_decorate()
    check_tool_and_web()
    check_executor_decoration()
    print("\n镜像源管理自检全部通过 ✅")
    return 0


if __name__ == "__main__":
    sys.exit(main())
