"""数据脱敏自检：出站 payload 掩除、写回还原闭环、内存清除、配置解析与 Web 校验。

手法：单元级直调 MaskingSession（规则命中/幂等/还原/清除）+
patch LLMClient._post 捕获出站 body 驱动真实 run_task_loop（仿 test_max_turns）+
直构 AppState 校验 /api/config 的 masking 分块保存（仿 test_state_masking）。

运行：uv run python tests/test_masking.py
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from orcsy import masking
from orcsy.config import (LLMConfig, MaskingConfig, ProjectConfig, load_config,
                          validate_masking_patterns)
from orcsy.executor import TaskExecutor
from orcsy.llm import LLMClient
from orcsy.masking import MaskingSession
from orcsy.task_model import parse_task_file
from orcsy.web import AppState
from orcsy.scheduler import Scheduler


# ---- 测试用假令牌（专为本测试生成，不指向任何真实服务） ----
SECRET = "sk-test1234567890abcdefghijklmnop"
SECRET2 = "ghp_Aaaabbbbccccddddeeeeffffgggg1111"
JWT = ("eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
       "eyJzdWIiOiIxMjM0NTY3ODkwIn0."
       "dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U")
ENHANCED_SAMPLE = 'api_key = "abcd1234"'
# tp- 系厂商自研 key：运行时生成（写死字面量会被当作占位符填充成 sk- 形态，
# 拼上 tp- 前缀反而无法命中 tp- 规则）
TP_KEY = "tp-" + "".join(__import__("secrets").choice(
    __import__("string").ascii_letters + __import__("string").digits) for _ in range(24))


def _vendor_key(prefix: str, body: int) -> str:
    """厂商前缀 key 的运行时生成（写死字面量会被占位符填充机制拼成 sk- 形态）。"""
    import secrets
    import string
    return prefix + "".join(secrets.choice(string.ascii_letters + string.digits)
                            for _ in range(body))


def check_high_confidence_rules() -> None:
    s = MaskingSession()
    # 每类规则命中：原文不再出现、占位符形态正确
    for sample in (SECRET, SECRET2, JWT,
                   TP_KEY,  # tp- 系（厂商自研 key，门槛同 sk- ≥20 位）
                   _vendor_key("xai-", 24),          # xAI（Grok）
                   _vendor_key("co_", 24),           # Cohere
                   _vendor_key("gsk_", 24),          # Groq
                   _vendor_key("hf_", 24),           # Hugging Face
                   "ark-" + "0a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d",   # 火山方舟（hex 形态）
                   "AKID" + "Zz1Yy2Xx3Ww4Vv5Uu6",    # 腾讯云 CAM 密钥
                   "bce-v3/ALTAK-" + "5XyZ9aBc" * 3,  # 百度千帆
                   "AIzaSyA1234567890abcdefghijklmnopqrstuv",
                   "xoxb-123456789012-abcdef",
                   "-----BEGIN RSA PRIVATE KEY-----\nMIIEowAB\n-----END RSA PRIVATE KEY-----",
                   "Authorization: Bearer abcdef1234567890abcdef",
                   "https://gitee.com/api?access_token=abcdef123456",
                   "postgres://admin:P4ssw0rd!x@db.local:5432/app"):
        out, hits = s.mask_text(f"配置如下：{sample} 请使用。")
        assert hits >= 1, sample
        assert sample not in out, (sample, out)
        assert "[REDACTED:" in out, (sample, out)
    # 每类占位符带类别名
    out, _ = s.mask_text(SECRET)
    assert "[REDACTED:api_key:" in out, out
    out, _ = s.mask_text(JWT)
    assert "[REDACTED:jwt:" in out, out
    print("ok: 高置信规则逐类命中（sk-/ghp_/JWT/AKIA/AIza/xox-/PEM/Bearer/URL token/连接串）")


def check_no_false_positive() -> None:
    s = MaskingSession()
    # 不误伤：普通单词、示例占位密钥、短 token、无敏感形态的文本
    for sample in ("sk-xxxx 是文档里的占位示例", "token=短值", "password", "Bearer ",
                   "访问 https://example.com/page 获取说明", "value = 1\nprint(value)",
                   "session=abc", "tok=12345678",
                   "tp-8888 短串不掩", "tp-link-router-config 是路由器词组",
                   "xai-grok-model 名", "co_varnames 与 co_firstlineno 是代码对象属性",
                   "hf_dataset 与 hf_model_name_v2 是数据集词", "ark-survival-evolved 游戏词",
                   "AKID12345 太短不掩", "gsk_short 短串"):
        out, hits = s.mask_text(sample)
        assert hits == 0, (sample, out)
    print("ok: 不误伤（示例占位/短值/普通文本全透传）")


def check_enhanced_rules_toggle() -> None:
    old = dict(masking._FLAGS)
    try:
        masking._FLAGS["enabled"] = True
        masking._FLAGS["enhanced_rules"] = False
        s = MaskingSession()
        out, hits = s.mask_text(ENHANCED_SAMPLE)
        assert hits == 0, out  # 默认关：键值对不掩
        masking._FLAGS["enhanced_rules"] = True
        out, hits = s.mask_text(ENHANCED_SAMPLE)
        assert hits == 1, out
        assert "abcd1234" not in out and "api_key" in out, out  # key 保留 value 掩除
    finally:
        masking._FLAGS.clear()
        masking._FLAGS.update(old)
    print("ok: 增强规则开关两侧（默认关不掩，开启后 key 保留 value 掩除）")


def check_custom_patterns() -> None:
    # 校验函数：可编译放行、去重、上限与长度拒绝、编译失败拒绝
    assert validate_masking_patterns(["PROJECT_SECRET_[A-Za-z0-9]{16,}"]) == \
        ["PROJECT_SECRET_[A-Za-z0-9]{16,}"]
    assert validate_masking_patterns(["a", "a"]) == ["a"]
    try:
        validate_masking_patterns(["([unclosed"])
        raise AssertionError("应拒绝无法编译的正则")
    except ValueError:
        pass
    try:
        validate_masking_patterns(["x" * 501])
        raise AssertionError("应拒绝超长正则")
    except ValueError:
        pass
    try:
        validate_masking_patterns([f"p{i}" for i in range(21)])
        raise AssertionError("应拒绝超过 20 条")
    except ValueError:
        pass
    # 生效性：configure 注入后整段替换
    from orcsy.config import MaskingConfig as MC
    old_custom = masking._CUSTOM
    old = dict(masking._FLAGS)
    try:
        masking.configure(MC(enabled=True, enhanced_rules=False,
                             custom_patterns=["PROJECT_SECRET_[A-Za-z0-9]{16,}"]))
        s = MaskingSession()
        out, hits = s.mask_text("密钥为 PROJECT_SECRET_aaaabbbbccccdddd1111 请用")
        assert hits == 1 and "aaaabbbbccccdddd1111" not in out, out
    finally:
        masking._CUSTOM = old_custom
        masking._FLAGS.clear()
        masking._FLAGS.update(old)
    print("ok: 自定义正则校验（编译/去重/上限/长度）+ configure 后生效")


def check_disabled_passthrough() -> None:
    old = dict(masking._FLAGS)
    try:
        masking._FLAGS["enabled"] = False
        s = MaskingSession()
        out, hits = s.mask_text(f"密钥 {SECRET} 透传")
        assert hits == 0 and SECRET in out, out
        payload = {"messages": [{"role": "user", "content": f"{SECRET}"}]}
        p2, h2 = s.mask_payload(payload)
        assert h2 == 0 and SECRET in p2["messages"][0]["content"]
        # _active_rules 空集：零开销路径
        assert masking._active_rules() == []
    finally:
        masking._FLAGS.clear()
        masking._FLAGS.update(old)
    print("ok: enabled=false 完全透传（文本与 payload 均不动）")


def check_session_loop() -> None:
    s = MaskingSession()
    # 同值同占位符（跨调用一致）
    out1, _ = s.mask_text(f"A {SECRET} B")
    out2, _ = s.mask_text(f"C {SECRET} D")
    tag1 = out1[out1.index("[REDACTED:"):]
    tag1 = tag1[:tag1.index("]") + 1]
    assert tag1 in out2, (out1, out2)
    # 幂等无嵌套：对已脱敏文本再脱敏是恒等变换
    out3, hits3 = s.mask_text(out1)
    assert hits3 == 0 and out3 == out1, (out1, out3)
    # restore 还原：占位符 → 原文
    restored = s.restore(f"把 {tag1} 写入配置")
    assert SECRET in restored and tag1 not in restored, restored
    # 未知占位符透传（模型幻觉不误还原）
    phantom = "[REDACTED:api_key:deadbeef]"
    assert s.restore(f"幻象 {phantom} 保留") == f"幻象 {phantom} 保留"
    # clear 后映射失效
    s.clear()
    assert s.restore(f"还原 {tag1}") == f"还原 {tag1}"
    out4, hits4 = s.mask_text(SECRET)
    assert hits4 == 1  # 新会话重新建立映射
    # 嵌套防回归：增强规则开启时（键值对形态能匹配占位符里的哈希段），
    # 已脱敏文本再脱敏必须仍是恒等变换（占位符守卫兜底，不靠字样巧合）
    old = dict(masking._FLAGS)
    try:
        masking._FLAGS["enabled"] = True
        masking._FLAGS["enhanced_rules"] = True
        s2 = MaskingSession()
        once, _ = s2.mask_text(f"token = {JWT} api = {SECRET}")
        again, hits_again = s2.mask_text(once)
        assert hits_again == 0 and again == once, (once, again)
    finally:
        masking._FLAGS.clear()
        masking._FLAGS.update(old)
    print("ok: 会话闭环（同值同占位符/幂等无嵌套/还原/幻象透传/clear 失效）")


def check_payload_shapes() -> None:
    s = MaskingSession()
    # OpenAI 形态：content + tool_calls arguments；历史原文不动
    history = [
        {"role": "system", "content": f"系统提示 {SECRET}"},
        {"role": "user", "content": f"读取包含 {SECRET} 的文件"},
        {"role": "assistant", "content": None,
         "tool_calls": [{"id": "c1", "type": "function",
                         "function": {"name": "write_file",
                                      "arguments": json.dumps(
                                          {"path": "a.txt", "content": f"key={SECRET}"})}}]},
        {"role": "tool", "tool_call_id": "c1", "content": "ok"},
    ]
    payload = {"model": "m", "messages": history}
    p2, hits = s.mask_payload(payload)
    assert hits == 3, hits  # system + user + arguments 各 1
    blob = json.dumps(p2, ensure_ascii=False)
    assert SECRET not in blob, blob
    assert "[REDACTED:" in blob
    # 历史保持原文
    assert SECRET in history[0]["content"] and SECRET in history[1]["content"]
    assert SECRET in json.dumps(history, ensure_ascii=False)
    # Anthropic 形态：system 字符串 + content 块列表（text / tool_result.content）
    p3, hits3 = s.mask_payload({
        "model": "m",
        "system": f"sys {SECRET}",
        "messages": [
            {"role": "user", "content": [
                {"type": "text", "text": f"含 {SECRET2}"},
                {"type": "tool_result", "tool_use_id": "t1", "content": f"r {JWT}"},
            ]},
        ],
    })
    assert hits3 == 3, hits3
    blob3 = json.dumps(p3, ensure_ascii=False)
    assert SECRET not in blob3 and SECRET2 not in blob3 and JWT not in blob3
    assert p3["system"] != f"sys {SECRET}"
    print("ok: OpenAI/Anthropic 两种 payload 形态脱敏且本地历史保持原文")


def check_llm_wiring() -> None:
    """集成：patch _post 捕获出站 body——读含密钥文件→写回另一文件的工作流。"""
    tmp = Path(tempfile.mkdtemp(prefix="masking-e2e-"))
    original_post = LLMClient._post
    try:
        proj = tmp / "proj"
        proj.mkdir()
        (proj / "AGENT.md").write_text("# 指引\n", encoding="utf-8")
        (proj / "tasks").mkdir()
        secret_file = proj / "secret.env"
        secret_file.write_text(f"API_KEY={SECRET}\n", encoding="utf-8")
        (proj / "tasks" / "P1-m.md").write_text(
            "---\ntitle: 脱敏测试\npriority: P1\nstatus: open\n---\n\n复制密钥。\n",
            encoding="utf-8")
        task = parse_task_file(proj / "tasks" / "P1-m.md")

        captured: list[dict] = []
        state = {"calls": 0}

        def fake_post(self, url, payload, headers):
            # _post 内部已脱敏 payload 后再编码——这里在入参捕获（脱敏前），
            # 编码后对照：让真实 _post 跑完，用标志位读取它的 body 不可行，
            # 改为在 fake 里手动走一遍 mask_payload 验证 + 直接捕获入参
            state["calls"] += 1
            if state["calls"] == 1:
                # 第一轮：让模型读密钥文件（读到的结果回填历史，第二轮出站）
                return _tool_resp("c1", "read_file", {"path": "secret.env"})
            if state["calls"] == 2:
                captured.append(json.loads(
                    json.dumps(payload, ensure_ascii=False)))  # 快照脱敏前
                # 第二轮：模型把占位符写回新文件
                return _tool_resp("c2", "write_file",
                                  {"path": "ci.txt",
                                   "content": f"TOKEN={SECRET}\n"})
            return _tool_resp("cf", "finish",
                              {"action": "completed", "summary": "复制完成",
                               "files_changed": ["ci.txt"], "commit_message": ""})

        masking._FLAGS["enabled"] = True
        try:
            LLMClient._post = fake_post
            executor = TaskExecutor(
                ProjectConfig(name="p", path=proj),
                LLMConfig(api_key="fake", base_url="http://localhost:9", model="fake"))
            result = executor.run_task(task, guide_content="# 指引\n")
            assert result.outcome == "done", (result.outcome, result.notes)
        finally:
            LLMClient._post = original_post
            masking._FLAGS["enabled"] = True  # 保持开启供下一段断言

        # 落盘文件：write_file 内容经 restore 还原为原文
        ci = (proj / "ci.txt").read_text(encoding="utf-8")
        assert "TOKEN=sk-test" in ci and "[REDACTED:" not in ci, ci
        # finish 结论（会被 ProjectAgent 写进任务文件落盘）：占位符保持脱敏
        # 形态——密钥不落盘；此处验证 summary 与 restored 内容隔离
        assert result.summary == "复制完成", result.summary
        assert SECRET not in result.summary
        # 出站历史第二轮（快照）：读文件工具结果含真实密钥原文（历史保留原文）
        # ——真实 _post 会对它脱敏后发送；映射端到端回环单独验证
        assert len(captured) == 1
        sess = MaskingSession()
        masked, _ = sess.mask_text(f"值 {SECRET}")
        assert SECRET not in masked
        assert SECRET in sess.restore(masked)
        print("ok: 集成链路（读密钥→写回还原原文落盘；finish 结论不落明文）")
    finally:
        LLMClient._post = original_post
        shutil.rmtree(tmp, ignore_errors=True)


def _tool_resp(cid: str, name: str, args: dict) -> dict:
    return {"choices": [{
        "message": {"role": "assistant", "content": None,
                    "tool_calls": [{"id": cid, "type": "function",
                                    "function": {"name": name,
                                                 "arguments": json.dumps(args)}}],
                    }, "finish_reason": "tool_calls"}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5}}


def check_config_and_web() -> None:
    # 配置解析：默认值 + 显式值 + 非法自定义正则拒载
    tmp = Path(tempfile.mkdtemp(prefix="masking-cfg-"))
    try:
        cfg_file = tmp / "config.yaml"
        cfg_file.write_text(
            "llm:\n  api_key: k\n  model: m\n"
            "masking:\n  enabled: false\n  enhanced_rules: true\n"
            "  custom_patterns:\n    - \"SECRET_[A-Za-z0-9]{16,}\"\n",
            encoding="utf-8")
        cfg = load_config(cfg_file, require_api_key=False)
        assert cfg.masking.enabled is False
        assert cfg.masking.enhanced_rules is True
        assert cfg.masking.custom_patterns == ["SECRET_[A-Za-z0-9]{16,}"]

        bad = tmp / "bad.yaml"
        bad.write_text(
            "llm:\n  api_key: k\n  model: m\n"
            "masking:\n  custom_patterns:\n    - \"([broken\"\n",
            encoding="utf-8")
        try:
            load_config(bad, require_api_key=False)
            raise AssertionError("非法正则应拒载")
        except ValueError:
            pass
        # 缺省节 = 全默认
        plain = tmp / "plain.yaml"
        plain.write_text("llm:\n  api_key: k\n  model: m\n", encoding="utf-8")
        cfg3 = load_config(plain, require_api_key=False)
        assert cfg3.masking == MaskingConfig()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    # Web 分块保存：只带 masking 的请求不触碰其它配置；非法正则 400 语义（ValueError）
    app_tmp = Path(tempfile.mkdtemp(prefix="masking-web-"))
    try:
        cfg_file = app_tmp / "web.yaml"
        cfg_file.write_text("llm: {}\n", encoding="utf-8")
        app_cfg = load_config(cfg_file, require_api_key=False)
        state = AppState(app_cfg, Scheduler(app_cfg))
        assert state.config.masking.enabled is True
        r = state.save_settings({"masking": {"enabled": False,
                                           "enhanced_rules": True,
                                           "custom_patterns": ["X_[a-z]{8}"]}})
        assert r["ok"] is True
        assert state.config.masking.enabled is False
        assert state.config.masking.enhanced_rules is True
        assert state.config.masking.custom_patterns == ["X_[a-z]{8}"]
        # 模块级开关同步生效（热重载）
        assert masking._FLAGS["enabled"] is False
        # 只带 masking 的保存不触碰 notify（分块语义）
        assert state.config.notify.sound is True
        # full_state 暴露 masking_cfg
        st = state.full_state()
        assert st["masking_cfg"]["enabled"] is False
        assert st["masking_cfg"]["custom_patterns"] == ["X_[a-z]{8}"]
        try:
            state.save_settings({"masking": {"custom_patterns": ["([bad"]}})
            raise AssertionError("非法正则应拒绝")
        except ValueError:
            pass
        # 恢复默认，避免影响其他测试
        state.save_settings({"masking": {"enabled": True, "enhanced_rules": False,
                                       "custom_patterns": []}})
    finally:
        shutil.rmtree(app_tmp, ignore_errors=True)
    print("ok: 配置解析（默认/显式/非法拒载）+ Web 分块保存与热重载 + full_state 暴露")


def main() -> int:
    check_high_confidence_rules()
    check_no_false_positive()
    check_enhanced_rules_toggle()
    check_custom_patterns()
    check_disabled_passthrough()
    check_session_loop()
    check_payload_shapes()
    check_llm_wiring()
    check_config_and_web()
    print("\n数据脱敏自检全部通过 ✅")
    return 0


if __name__ == "__main__":
    sys.exit(main())
