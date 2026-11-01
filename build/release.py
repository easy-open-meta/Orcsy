"""发布 Gitee Release：一条命令同时上传 首次安装包(exe) 与 自更新包(zip)。

发版流程：build.bat（产出两个附件）→ 本脚本（建 Release 并上传全部附件）。
在线更新只认 orcsy-update-*.zip，exe 只用于首次安装——两个附件必须一起发。

用法：
  uv run python build/release.py                       # 读配置 update.repo / token
  uv run python build/release.py --repo https://gitee.com/xxx/orcsy --token <令牌>
  uv run python build/release.py --notes-file 发布说明.md
  uv run python build/release.py --dry-run              # 只打印将要发布的清单

token 优先级：--token > 环境变量 GITEE_TOKEN > 配置 update.token。
同一 tag 重跑：已存在的 Release 直接复用，同名旧附件先删再传（幂等，
改包重发不用去网页手工删）。tag 约定 V{版本}（与 orcsy/updater.py 识别一致）。
"""

from __future__ import annotations

import argparse
import importlib.util
import os
import sys
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from orcsy import USER_AGENT  # noqa: E402
from orcsy.issues import IssueError, detect_repo  # noqa: E402


class ReleaseError(Exception):
    """发布失败的领域错误（中文可行动文案）。"""


def _load_build_module():
    spec = importlib.util.spec_from_file_location(
        "orcsy_build", ROOT / "build" / "build.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def read_version() -> tuple[int, int, int]:
    return _load_build_module().read_version()


def expected_artifacts(ver: tuple[int, int, int],
                      setup_dir: Path | str | None = None) -> list[Path]:
    """本次发布应上传的附件：首次安装包 + 自更新包（缺一不可）。"""
    v = ".".join(map(str, ver))
    d = Path(setup_dir) if setup_dir else ROOT / "build" / "Setup"
    return [d / f"orcsy-setup-{v}.exe",
            d / f"orcsy-update-{v}.zip"]


def check_artifacts(ver: tuple[int, int, int],
                    setup_dir: Path | str | None = None) -> list[Path]:
    files = expected_artifacts(ver, setup_dir)
    missing = [f.name for f in files if not f.is_file() or f.stat().st_size == 0]
    if missing:
        raise ReleaseError(
            f"缺少发布附件：{', '.join(missing)}。请先运行 build.bat（或 "
            f"uv run python build/build.py）产出两个安装包后再发布。")
    return files


def load_release_config() -> tuple[str, str]:
    """从 config.yaml 的 update 节读 发布仓库 URL 与 token（可被命令行覆盖）。"""
    from orcsy.__main__ import find_config
    from orcsy.config import load_config

    try:
        cfg = load_config(find_config(None), require_api_key=False)
    except (FileNotFoundError, ValueError) as e:
        raise ReleaseError(
            f"读取配置失败：{e}。可用 --repo / --token 直接指定发布目标。") from e
    return str(cfg.update.repo or ""), str(cfg.update.token or "")


def _http(repo_url: str, api_base: str | None = None) -> tuple[Any, dict[str, str]]:
    info = detect_repo(repo_url, "gitee")
    origin = (api_base or info["origin"]).rstrip("/")
    return info, {"User-Agent": USER_AGENT}


def _trust_env(url: str) -> bool:
    return urlparse(url).hostname not in ("127.0.0.1", "localhost", "::1")


def create_or_get_release(repo_url: str, tag: str, name: str, body: str,
                          token: str, api_base: str | None = None) -> dict:
    """建 Release（已存在则取回），返回 {id, html_url, created}。"""
    info, headers = _http(repo_url, api_base)
    base = (api_base or info["origin"]).rstrip("/")
    url = f"{base}/api/v1/repos/{info['owner']}/{info['repo']}/releases"
    resp = httpx.post(url, timeout=60, headers=headers, trust_env=_trust_env(url),
                      data={"access_token": token, "tag_name": tag, "name": name,
                            "body": body, "prerelease": "false", "draft": "false"})
    if resp.status_code in (200, 201):
        data = resp.json()
        return {"id": data["id"], "html_url": data.get("html_url", ""),
                "created": True}
    # 已存在同 tag 的 Release（400/422）→ 按 tag 取回，走附件替换
    get_url = f"{url}/tags/{tag}"
    got = httpx.get(get_url, params={"access_token": token}, timeout=60,
                    headers=headers, trust_env=_trust_env(get_url))
    if got.status_code == 200:
        data = got.json()
        return {"id": data["id"], "html_url": data.get("html_url", ""),
                "created": False}
    raise ReleaseError(
        f"创建 Release 失败：HTTP {resp.status_code} {resp.text[:200]}"
        + (f"；取回已有 Release 也失败：HTTP {got.status_code} {got.text[:200]}"
           if got.status_code != 200 else ""))


def upload_asset(repo_url: str, release_id: int, path: Path, token: str,
                 api_base: str | None = None) -> dict:
    """上传单个附件；同名旧附件先删再传（幂等，改包重发不残留）。"""
    info, headers = _http(repo_url, api_base)
    base = (api_base or info["origin"]).rstrip("/")
    url = f"{base}/api/v1/repos/{info['owner']}/{info['repo']}/releases/{release_id}/attach_files"

    def _list() -> list[dict]:
        r = httpx.get(url, params={"access_token": token}, timeout=60,
                      headers=headers, trust_env=_trust_env(url))
        if r.status_code != 200:
            raise ReleaseError(f"读取附件列表失败：HTTP {r.status_code} {r.text[:200]}")
        return [a for a in r.json() if isinstance(a, dict)]

    for old in _list():
        if str(old.get("name") or "") == path.name:
            d = httpx.delete(f"{url}/{old['id']}",
                             params={"access_token": token}, timeout=60,
                             headers=headers, trust_env=_trust_env(url))
            if d.status_code not in (200, 204):
                raise ReleaseError(
                    f"删除同名旧附件 {path.name} 失败：HTTP {d.status_code} {d.text[:200]}")
    with open(path, "rb") as f:
        resp = httpx.post(url, timeout=600, headers=headers,
                          trust_env=_trust_env(url),
                          data={"access_token": token, "name": path.name},
                          files={"_file": (path.name, f, "application/octet-stream")})
    if resp.status_code not in (200, 201):
        raise ReleaseError(f"上传附件 {path.name} 失败：HTTP {resp.status_code} {resp.text[:200]}")
    return resp.json()


def verify_assets(repo_url: str, release_id: int, expected: list[Path],
                  token: str, api_base: str | None = None) -> list[str]:
    """上传后核对：每个附件都在且大小非 0，缺一个就报错（不静默少传）。"""
    info, headers = _http(repo_url, api_base)
    base = (api_base or info["origin"]).rstrip("/")
    url = f"{base}/api/v1/repos/{info['owner']}/{info['repo']}/releases/{release_id}/attach_files"
    r = httpx.get(url, params={"access_token": token}, timeout=60,
                  headers=headers, trust_env=_trust_env(url))
    if r.status_code != 200:
        raise ReleaseError(f"核对附件失败：HTTP {r.status_code} {r.text[:200]}")
    remote = {str(a.get("name") or ""): a for a in r.json() if isinstance(a, dict)}
    problems = []
    for p in expected:
        a = remote.get(p.name)
        if not a:
            problems.append(f"{p.name}（未上传成功）")
        elif int(a.get("size") or 0) <= 0:
            problems.append(f"{p.name}（大小为 0）")
    if problems:
        raise ReleaseError("附件核对未通过：" + "；".join(problems))
    return sorted(remote)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="发布 Gitee Release（exe + zip 一并上传）")
    ap.add_argument("--repo", help="更新源仓库 URL（缺省读 config.yaml 的 update.repo）")
    ap.add_argument("--token", help="Gitee 访问令牌（缺省 GITEE_TOKEN 环境变量或 update.token）")
    ap.add_argument("--notes", default="", help="Release 正文（一行）")
    ap.add_argument("--notes-file", help="Release 正文文件（markdown）")
    ap.add_argument("--dry-run", action="store_true", help="只打印发布清单，不联网")
    ap.add_argument("--api-base", default=None, help=argparse.SUPPRESS)  # 测试注入假服务
    args = ap.parse_args(argv)

    try:
        ver = read_version()
        files = check_artifacts(ver)
        tag = f"V{'.'.join(map(str, ver))}"

        print(f"发布 {tag}")
        for f in files:
            print(f"  附件 {f.name}  ({f.stat().st_size / 1024 / 1024:.1f} MB)")
        if args.dry_run:
            print("（--dry-run：未联网）")
            return 0

        cfg_repo, cfg_token = load_release_config()
        repo = (args.repo or cfg_repo or "").strip()
        token = (args.token or os.environ.get("GITEE_TOKEN") or cfg_token or "").strip()
        if not repo:
            raise ReleaseError("未指定发布仓库：用 --repo 或在 config.yaml 的 update.repo 配置。")
        if not token:
            raise ReleaseError(
                "未指定访问令牌：用 --token、环境变量 GITEE_TOKEN 或 config.yaml 的 "
                "update.token 配置（需有该仓库 Releases 权限）。")

        body = args.notes
        if args.notes_file:
            body = Path(args.notes_file).read_text(encoding="utf-8")
        if not body.strip():
            body = (f"Orcsy {tag}\n\n"
                    f"- `{files[0].name}`：首次安装包（exe 安装向导）\n"
                    f"- `{files[1].name}`：自更新包（zip，看板「手动导入安装包」或在线更新使用）")

        rel = create_or_get_release(repo, tag, f"Orcsy {tag}", body, token,
                                    api_base=args.api_base)
        print(("创建 Release：" if rel["created"] else "复用已有 Release：")
              + (rel["html_url"] or f"id={rel['id']}"))
        for f in files:
            upload_asset(repo, rel["id"], f, token, api_base=args.api_base)
            print(f"  已上传 {f.name}")
        names = verify_assets(repo, rel["id"], files, token, api_base=args.api_base)
        print(f"附件核对通过（{len(names)} 个）：{', '.join(names)}")
        print(f"\n发布完成：{rel['html_url'] or tag}（安装版在线更新将识别 zip 更新包）")
        return 0
    except (ReleaseError, IssueError, OSError, httpx.HTTPError) as e:
        print(f"发布失败：{e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
