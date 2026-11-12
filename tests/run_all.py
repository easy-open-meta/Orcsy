"""批量测试回归运行器。

工程化执行 `tests/` 目录下所有的直跑式测试文件（test_*.py 和 e2e_*.py），
统计每个测试的耗时与通过状态，并给出最终汇总报告。

用法：
    uv run python tests/run_all.py             # 运行全部测试
    uv run python tests/run_all.py --unit-only # 仅运行单元/子系统测试（排除较慢的 e2e）
    uv run python tests/run_all.py masking     # 按名称过滤运行匹配的测试
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description="Orcsy 批量回归测试套件")
    parser.add_argument("filter", nargs="?", default="", help="可选：过滤测试文件名子串")
    parser.add_argument("--unit-only", action="store_true", help="仅运行 test_*.py，跳过 e2e_*.py")
    parser.add_argument("--timeout", type=int, default=120, help="单个测试最大超时时间（秒，默认 120）")
    args = parser.parse_args()

    tests_dir = Path(__file__).resolve().parent
    test_files: list[Path] = []

    for p in sorted(tests_dir.glob("*.py")):
        if p.name == "run_all.py":
            continue
        if args.unit_only and p.name.startswith("e2e_"):
            continue
        if not (p.name.startswith("test_") or p.name.startswith("e2e_")):
            continue
        if args.filter and args.filter.lower() not in p.name.lower():
            continue
        test_files.append(p)

    if not test_files:
        print(f"未找到匹配的测试文件（过滤器：{args.filter!r}）")
        return 0

    print("=" * 70)
    print(f"Orcsy 自动化回归测试：共 {len(test_files)} 个测试文件")
    print(f"解释器：{sys.executable}")
    print("=" * 70)

    passed = 0
    failed: list[tuple[str, str]] = []
    start_total = time.monotonic()

    for idx, path in enumerate(test_files, start=1):
        rel_name = path.name
        print(f"[{idx:02d}/{len(test_files):02d}] 正在执行 {rel_name:<35} ... ", end="", flush=True)

        t0 = time.monotonic()
        try:
            res = subprocess.run(
                [sys.executable, str(path)],
                cwd=str(tests_dir.parent),
                capture_output=True,
                text=True,
                timeout=args.timeout,
                check=False,
            )
            elapsed = time.monotonic() - t0
            if res.returncode == 0:
                passed += 1
                print(f"✓ PASS ({elapsed:.2f}s)")
            else:
                err_msg = (res.stderr or res.stdout or "").strip()
                failed.append((rel_name, err_msg[-500:] if len(err_msg) > 500 else err_msg))
                print(f"✗ FAIL ({elapsed:.2f}s, exit code {res.returncode})")
        except subprocess.TimeoutExpired:
            elapsed = time.monotonic() - t0
            failed.append((rel_name, f"执行超时（>{args.timeout}s）"))
            print(f"✗ TIMEOUT ({elapsed:.2f}s)")
        except Exception as e:  # noqa: BLE001
            elapsed = time.monotonic() - t0
            failed.append((rel_name, f"运行时异常: {e}"))
            print(f"✗ ERROR ({elapsed:.2f}s)")

    total_time = time.monotonic() - start_total
    print("=" * 70)
    print(f"测试完成：{passed} 通过, {len(failed)} 失败，总耗时 {total_time:.2f}s")

    if failed:
        print("\n失败用例明细：")
        for name, err in failed:
            print("-" * 50)
            print(f"【{name}】")
            print(err)
        return 1

    print("全部测试通过！✓")
    return 0


if __name__ == "__main__":
    sys.exit(main())
