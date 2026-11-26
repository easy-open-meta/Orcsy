"""Risk assessment: path/content rules, fail-closed behavior and digest binding."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from orcsy.risk import assess_risk, failed_assessment


def level(files: list[str], diff: str = "", **kwargs) -> str:
    return assess_risk(files=files, diff=diff, **kwargs).level


def main() -> None:
    assert level(["docs/a.md"]) == "low"
    assert level(["README.md", "CHANGELOG.md"]) == "low"
    assert level(["tests/test_x.py"]) == "low"
    assert level(["orcsy/executor.py"]) == "medium"
    assert level(["dashboard/src/App.vue"]) == "medium"
    assert level(["unknown/path.xyz"]) == "medium"

    assert level(["orcsy/updater.py"]) == "high"
    assert level(["orcsy/auth.py"]) == "high"
    assert level(["orcsy/egress.py"]) == "high"
    assert level(["uv.lock"]) == "high"
    assert level(["package-lock.json"]) == "high"
    assert level([".github/workflows/ci.yml"]) == "high"
    assert level(["build/installer.iss"]) == "high"

    # 内容信号：同一条 config.py，schema 迁移才算 high
    assert level(["orcsy/config.py"], "CONFIG_VERSION = 3\n") == "high"
    assert level(["orcsy/config.py"], "x = 1\n") == "medium"
    assert level(["orcsy/web.py"], "AUTH_COOKIE = 'x'\n") == "high"
    assert level(["orcsy/web.py"], "return 1\n") == "medium"

    # 项目自定义高风险 glob
    custom = level(["src/billing/pay.py"], extra_high_risk_paths=["src/billing/**"])
    assert custom == "high", custom

    # 多文件取最高等级；原因去重保序
    mixed = assess_risk(files=["docs/a.md", "orcsy/updater.py", "orcsy/executor.py"],
                        diff="x")
    assert mixed.level == "high"
    assert any("自更新" in r for r in mixed.reasons)
    assert any("后端代码" in r for r in mixed.reasons)

    # 空改动 → low，且仍产出稳定 digest
    empty = assess_risk(files=[], diff="")
    assert empty.level == "low" and empty.digest

    # digest 绑定 commit + diff + 等级/原因
    base = assess_risk(files=["orcsy/updater.py"], diff="a", branch_sha="abc")
    same = assess_risk(files=["orcsy/updater.py"], diff="a", branch_sha="abc")
    other_sha = assess_risk(files=["orcsy/updater.py"], diff="a", branch_sha="def")
    other_diff = assess_risk(files=["orcsy/updater.py"], diff="b", branch_sha="abc")
    assert base.digest == same.digest
    assert base.digest != other_sha.digest
    assert base.digest != other_diff.digest

    # 评估失败 fail closed
    failed = failed_assessment("git diff 失败")
    assert failed.level == "high"
    assert "风险评估失败" in failed.reasons[0]

    print("test_risk: all checks passed")


if __name__ == "__main__":
    main()
