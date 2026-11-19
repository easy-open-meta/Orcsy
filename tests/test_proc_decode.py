"""测试子进程输出编码自愈解码（utf-8 → ANSI 代码页回退）。"""

from __future__ import annotations

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from orcsy.tools import _decode_proc_output, dispatch_tool, ToolContext


def test_decode_utf8_native() -> None:
    """utf-8 输出（go/node/python）原样解码，零损耗。"""
    raw = "中文路径测试 OK ✓ emoji".encode("utf-8")
    assert _decode_proc_output(raw) == "中文路径测试 OK ✓ emoji"
    assert _decode_proc_output(b"") == ""
    print("ok: utf-8 输出原样解码 / 空输出安全")


def test_decode_gbk_fallback() -> None:
    """GBK 输出（cmd/findstr/dir 原生命令的中文路径）自动回退 ANSI 解码。"""
    # Windows 简中系统 ANSI = cp936(GBK)
    gbk_path = r"D:\Repos\pixsy\P1-1-后端实现-Admin-用户管理-CRUD-接口与数据层方法\internal\api"
    raw = gbk_path.encode("gbk")
    text = _decode_proc_output(raw)
    assert text == gbk_path, repr(text)
    assert "\ufffd" not in text, "自愈解码后不应残留 U+FFFD"
    print("ok: GBK 中文路径自愈解码（不再出现乱码）")


def test_decode_gbk_high_ratio() -> None:
    """高占比乱码（实际日志样本：整行 GBK 中文路径被 utf-8 强解）。"""
    raw = "D:\\proj\\后端实现用户管理与数据库层方法".encode("gbk")
    text = _decode_proc_output(raw)
    assert "后端实现用户管理" in text, repr(text)
    print("ok: 高占比 GBK 乱码自愈")


def test_decode_gbk_long_output() -> None:
    """长输出中只夹一段 GBK 中文路径：按连续 U+FFFD 判定，不能受占比稀释。"""
    gbk_path = r"D:\Repos\caresy\.worktree-arranger\P1-3-frontend-梳理前端页面-CRUD-矩阵并核对前后端接口契约\caresy-api\src"
    raw = ("X" * 10000 + "\n" + gbk_path + "\n").encode("gbk")
    text = _decode_proc_output(raw)
    assert gbk_path in text, repr(text[-200:])
    assert "\ufffd" not in text
    print("ok: 长输出中的 GBK 中文路径自愈（不因占比低漏判）")


def test_decode_utf8_single_bad_byte() -> None:
    """真 utf-8 输出偶发 1 个坏字节：保持 utf-8，不误切 ANSI。"""
    raw = "中文路径测试".encode("utf-8") + b"\xff"
    text = _decode_proc_output(raw)
    assert "中文路径测试" in text, repr(text)
    assert text.count("\ufffd") == 1, repr(text)
    print("ok: utf-8 单坏字节不误切 ANSI")


def test_run_command_chinese_output() -> None:
    """端到端：run_command 捕获含中文输出的原生命令不再乱码。"""
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        ctx = ToolContext(root=Path(tmp), allowed_commands=["cmd", "echo"])
        # echo 走 cmd 原生（GBK 管道输出）；[cmd /c echo 中文] 在简中 Windows 上输出 GBK 字节
        r = dispatch_tool(ctx, "run_command", {"command": ["cmd", "/c", "echo", "后端实现测试"]})
        # 要么自愈解出中文，要么至少不残留 U+FFFD（如系统默认 utf-8 代码页时原样）
        assert "退出码 0" in r, r
        assert "\ufffd" not in r, f"输出残留乱码：{r[:200]}"
        print("ok: run_command 中文输出端到端无乱码")


def main() -> int:
    if sys.platform != "win32":
        print("跳过：断言 GBK 代码页控制台输出解码，仅 Windows 运行")
        return 0
    test_decode_utf8_native()
    test_decode_gbk_fallback()
    test_decode_gbk_high_ratio()
    test_decode_gbk_long_output()
    test_decode_utf8_single_bad_byte()
    test_run_command_chinese_output()
    print("\n子进程输出编码自愈全部测试通过 ✅")
    return 0


if __name__ == "__main__":
    sys.exit(main())
