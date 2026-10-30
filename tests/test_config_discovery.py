"""find_config / load_config 的健壮性自检（目录占位、读取失败降级、清晰报错）。

背景：配置可能躺在 OneDrive「文档」目录。开机自启的托盘常抢在 OneDrive 就绪
之前，云端占位文件 exists() 为真但读取报 OSError EINVAL——此前会带着裸
traceback 崩掉。现在 find_config 只认普通文件、探针试读、失败换下一个候选；
load_config 把读取失败包装成含 OneDrive 提示的清晰报错。
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):  # GBK 控制台打不出 ✅ 等字符
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from orcsy.__main__ import find_config
from orcsy.config import load_config


def _make_run_config(tmp: Path) -> Path:
    run_dir = tmp / "run"
    run_dir.mkdir(exist_ok=True)
    cfg = run_dir / "config.yaml"
    cfg.write_text("llm:\n  api_key: sk-test\n", encoding="utf-8")
    return cfg


def check_skip_directory(tmp: Path) -> None:
    """候选位置被同名目录占用（exists() 为真）时跳过，继续找下一个。"""
    (tmp / "config.yaml").mkdir()
    expected = _make_run_config(tmp)
    cwd = os.getcwd()
    os.chdir(tmp)
    try:
        assert find_config(None) == expected.resolve()
    finally:
        os.chdir(cwd)
    print("1. find_config：同名目录占位被跳过，命中 run/config.yaml ✅")


def check_unreadable_falls_through(tmp: Path) -> None:
    """候选文件存在但读取报 EINVAL（模拟 OneDrive 云端占位）时降级到下一候选。"""
    if (tmp / "config.yaml").is_dir():  # 上一用例的同名目录占位先清掉
        (tmp / "config.yaml").rmdir()
    (tmp / "config.yaml").write_text("llm: {}\n", encoding="utf-8")
    expected = _make_run_config(tmp)
    real_open = Path.open

    def fake_open(self: Path, *args, **kwargs):
        # find_config 构造的是相对候选 Path("config.yaml")，按 str 匹配
        if str(self) == "config.yaml":
            raise OSError(22, "Invalid argument")
        return real_open(self, *args, **kwargs)

    Path.open = fake_open  # type: ignore[method-assign]
    cwd = os.getcwd()
    os.chdir(tmp)
    try:
        assert find_config(None) == expected.resolve()
    finally:
        Path.open = real_open  # type: ignore[method-assign]
        os.chdir(cwd)
    print("2. find_config：占位文件读取失败自动换下一个候选 ✅")


def check_explicit_rejects_directory(tmp: Path) -> None:
    """显式 --config 指向目录时报错退出，不再带出一个目录去 read_text。"""
    try:
        find_config(str(tmp))
    except SystemExit as e:
        assert e.code == 1, e.code
    else:
        raise AssertionError("目录应被拒绝（SystemExit 1）")
    print("3. find_config：显式路径指向目录被拦截 ✅")


def check_load_config_clear_error(tmp: Path) -> None:
    """load_config 把读取失败包装成含路径与 OneDrive 提示的清晰报错。"""
    p = tmp / "config.yaml"
    p.write_text("llm:\n  api_key: sk-test\n", encoding="utf-8")
    real_read_text = Path.read_text

    def fake_read_text(self: Path, *args, **kwargs):
        if self == p:
            raise OSError(22, "Invalid argument")
        return real_read_text(self, *args, **kwargs)

    Path.read_text = fake_read_text  # type: ignore[method-assign]
    try:
        load_config(p, require_api_key=False)
    except FileNotFoundError as e:
        assert str(p) in str(e), e
        assert "OneDrive" in str(e), e
    else:
        raise AssertionError("读取失败应抛 FileNotFoundError（含提示）")
    finally:
        Path.read_text = real_read_text  # type: ignore[method-assign]
    print("4. load_config：EINVAL 包装为含 OneDrive 提示的清晰报错 ✅")


def check_appdata_priority(tmp: Path) -> None:
    """默认数据目录（%APPDATA%\\Orcsy）排在文档目录旧位置之前；ORCSY_DATA_DIR 可改址。"""
    tmp.mkdir(parents=True, exist_ok=True)
    data = tmp / "data"
    data.mkdir()
    (data / "config.yaml").write_text("llm:\n  api_key: sk-test\n", encoding="utf-8")
    cwd = os.getcwd()
    old = os.environ.get("ORCSY_DATA_DIR")
    os.environ["ORCSY_DATA_DIR"] = str(data)
    os.chdir(tmp)  # 空目录：CWD 候选落空，验证数据目录候选生效
    try:
        from orcsy.__main__ import default_data_dir, find_config

        assert default_data_dir() == data
        assert find_config(None) == (data / "config.yaml").resolve()
    finally:
        os.chdir(cwd)
        if old is None:
            os.environ.pop("ORCSY_DATA_DIR", None)
        else:
            os.environ["ORCSY_DATA_DIR"] = old
    print("5. 数据目录：%APPDATA%\\Orcsy 优先于文档目录旧位置，ORCSY_DATA_DIR 改址生效 ✅")


def check_frozen_skips_cwd(tmp: Path, monkey_data: Path) -> None:
    """安装版（frozen）不按 CWD 找配置——托盘启动 CWD 不可控，杜绝误绑。"""
    tmp.mkdir(parents=True, exist_ok=True)
    (tmp / "config.yaml").write_text("llm:\n  api_key: sk-cwd\n", encoding="utf-8")
    monkey_data.mkdir(exist_ok=True)
    (monkey_data / "config.yaml").write_text("llm:\n  api_key: sk-data\n", encoding="utf-8")
    cwd = os.getcwd()
    old = os.environ.get("ORCSY_DATA_DIR")
    frozen_flag = getattr(sys, "frozen", False)
    os.environ["ORCSY_DATA_DIR"] = str(monkey_data)
    sys.frozen = True  # type: ignore[attr-defined]  # 模拟 PyInstaller 冻结环境
    os.chdir(tmp)
    try:
        from orcsy.__main__ import find_config

        assert find_config(None) == (monkey_data / "config.yaml").resolve()
    finally:
        os.chdir(cwd)
        if old is None:
            os.environ.pop("ORCSY_DATA_DIR", None)
        else:
            os.environ["ORCSY_DATA_DIR"] = old
        if frozen_flag:
            sys.frozen = frozen_flag  # type: ignore[attr-defined]
        else:
            del sys.frozen  # type: ignore[attr-defined]
    print("6. 安装版：frozen 模式跳过 CWD 候选，只认数据目录链 ✅")


def check_migration(tmp: Path) -> None:
    """旧「文档」配置与技能库一次性复制到新数据目录；原件保留；幂等。"""
    import orcsy.__main__ as m

    legacy, target = tmp / "legacy-docs" / "Orcsy", tmp / "appdata" / "Orcsy"
    legacy.mkdir(parents=True)
    (legacy / "config.yaml").write_text("llm:\n  api_key: sk-old\n", encoding="utf-8")
    skill = legacy / "skills" / "demo"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: demo\n---\n正文\n", encoding="utf-8")

    real_ddd, real_ccd = m.default_data_dir, m._candidate_config_dirs
    m.default_data_dir = lambda: target
    m._candidate_config_dirs = lambda: [target, legacy]
    try:
        m._migrate_legacy_config()
        assert (target / "config.yaml").read_text(encoding="utf-8") == "llm:\n  api_key: sk-old\n"
        assert (target / "skills" / "demo" / "SKILL.md").is_file()
        assert (legacy / "config.yaml").is_file(), "原件应保留作备份"
        m._migrate_legacy_config()  # 目标已有可读配置 → 幂等无操作
        assert (legacy / "config.yaml").is_file()
    finally:
        m.default_data_dir = real_ddd
        m._candidate_config_dirs = real_ccd
    print("7. 自动迁移：旧文档目录配置+技能库复制到数据目录，原件保留、幂等 ✅")


def check_init_default_dir(tmp: Path) -> None:
    """orcsy init 不带 --dir 时初始化默认数据目录。"""
    data = tmp / "init-data"
    cwd = os.getcwd()
    old = os.environ.get("ORCSY_DATA_DIR")
    os.environ["ORCSY_DATA_DIR"] = str(data)
    try:
        import argparse

        from orcsy.__main__ import cmd_init

        cmd_init(argparse.Namespace(dir=None))
        assert (data / "config.yaml").is_file()
    finally:
        os.chdir(cwd)
        if old is None:
            os.environ.pop("ORCSY_DATA_DIR", None)
        else:
            os.environ["ORCSY_DATA_DIR"] = old
    print("8. orcsy init：默认初始化到数据目录（ORCSY_DATA_DIR 可改址）✅")


def check_autocreate_when_empty(tmp: Path) -> None:
    """所有候选都不存在时，在默认数据目录自动生成模板配置（零配置开箱即用）。"""
    import orcsy.__main__ as m

    data = tmp / "data"
    cwd = os.getcwd()
    old = os.environ.get("ORCSY_DATA_DIR")
    os.environ["ORCSY_DATA_DIR"] = str(data)
    real_ccd = m._candidate_config_dirs
    m._candidate_config_dirs = lambda: [m.default_data_dir()]  # 隔离真机文档目录
    tmp.mkdir(parents=True, exist_ok=True)
    os.chdir(tmp)  # 空目录：CWD 候选落空
    try:
        from orcsy.__main__ import CONFIG_EXAMPLE, find_config

        got = find_config(None)
        assert got == (data / "config.yaml").resolve(), got
        assert (data / "config.yaml").read_text(encoding="utf-8") == CONFIG_EXAMPLE
    finally:
        os.chdir(cwd)
        m._candidate_config_dirs = real_ccd
        if old is None:
            os.environ.pop("ORCSY_DATA_DIR", None)
        else:
            os.environ["ORCSY_DATA_DIR"] = old
    print("9. 自动建配置：候选全空时在数据目录生成模板 config.yaml ✅")


def check_no_shadow_when_unreadable(tmp: Path) -> None:
    """已有候选只是暂时读不了（如 OneDrive 占位）时不自动建空配置遮蔽真实配置。"""
    import orcsy.__main__ as m

    data = tmp / "data"
    cwd = os.getcwd()
    old = os.environ.get("ORCSY_DATA_DIR")
    os.environ["ORCSY_DATA_DIR"] = str(data)  # 目标不存在：若误触发自动建会落在这里
    real_ccd = m._candidate_config_dirs
    m._candidate_config_dirs = lambda: [m.default_data_dir()]
    tmp.mkdir(parents=True, exist_ok=True)
    (tmp / "config.yaml").write_text("llm:\n  api_key: sk-real\n", encoding="utf-8")
    real_open = Path.open

    def fake_open(self: Path, *args, **kwargs):
        if str(self).endswith("config.yaml"):  # 所有 config.yaml 一律读不了
            raise OSError(22, "Invalid argument")
        return real_open(self, *args, **kwargs)

    Path.open = fake_open  # type: ignore[method-assign]
    os.chdir(tmp)
    try:
        from orcsy.__main__ import find_config

        find_config(None)
    except SystemExit as e:
        assert e.code == 1, e.code
    else:
        raise AssertionError("候选存在但都读不了时应报错退出")
    finally:
        os.chdir(cwd)
        Path.open = real_open  # type: ignore[method-assign]
        m._candidate_config_dirs = real_ccd
        if old is None:
            os.environ.pop("ORCSY_DATA_DIR", None)
        else:
            os.environ["ORCSY_DATA_DIR"] = old
    assert not (data / "config.yaml").exists(), "不应生成空配置遮蔽真实配置"
    print("10. 不遮蔽：候选存在但全读不了时报错退出，不生成空配置 ✅")


def check_init_no_config(tmp: Path) -> None:
    """orcsy init --no-config：只落任务/指引模板，不生成 config.yaml。"""
    import argparse

    from orcsy.__main__ import cmd_init

    target = tmp / "init-templates"
    cmd_init(argparse.Namespace(dir=str(target), no_config=True))
    assert (target / "AGENT.md").is_file(), "模板应已生成"
    assert not (target / "config.yaml").exists(), "不应生成 config.yaml"
    print("11. init --no-config：只生成模板，config.yaml 留给应用首次启动自动创建 ✅")


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="orcsy-cfgdiscovery-"))
    try:
        check_skip_directory(tmp)
        check_unreadable_falls_through(tmp)
        check_explicit_rejects_directory(tmp)
        check_load_config_clear_error(tmp)
        check_appdata_priority(tmp / "prio")
        check_frozen_skips_cwd(tmp / "frozen-cwd", tmp / "frozen-data")
        check_migration(tmp / "migrate")
        check_init_default_dir(tmp / "init")
        check_autocreate_when_empty(tmp / "autocreate")
        check_no_shadow_when_unreadable(tmp / "noshadow")
        check_init_no_config(tmp / "initnc")
        print("\n配置发现健壮性自检全部通过 ✅")
        return 0
    finally:
        os.chdir(Path.home())
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
