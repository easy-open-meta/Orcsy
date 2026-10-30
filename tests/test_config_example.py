"""config.example.yaml 与内置 CONFIG_EXAMPLE 常量的同步自检。

仓库根目录的 config.example.yaml 供「复制为 config.yaml」使用（.gitignore 与
config.py 的报错信息都引用它）；orcsy init 则从 CONFIG_EXAMPLE 常量生成。
两处内容必须一致，此测试防止改动常量后忘记同步模板文件（反之亦然）。
"""

from __future__ import annotations

import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):  # GBK 控制台打不出 ✅ 等字符
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "config.example.yaml"


def main() -> int:
    if not EXAMPLE.is_file():
        print(f"缺少模板文件：{EXAMPLE}\n"
              f"修复：uv run python -c \"from pathlib import Path; "
              f"from orcsy.__main__ import CONFIG_EXAMPLE; "
              f"Path('{EXAMPLE.name}').write_text(CONFIG_EXAMPLE, encoding='utf-8')\"")
        return 1

    from orcsy.__main__ import CONFIG_EXAMPLE

    file_text = EXAMPLE.read_text(encoding="utf-8")
    if file_text != CONFIG_EXAMPLE:
        print("config.example.yaml 与 orcsy.__main__.CONFIG_EXAMPLE 内容不一致，请同步后重跑。")
        import difflib

        for line in difflib.unified_diff(
                CONFIG_EXAMPLE.splitlines(), file_text.splitlines(),
                "CONFIG_EXAMPLE", "config.example.yaml", lineterm=""):
            print(line)
        return 1
    print("config.example.yaml 与 CONFIG_EXAMPLE 保持同步 ✅")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
