"""远程仓库 Issues 集成：识别平台、拉取 Issues、导入为项目任务。

支持 GitHub / Gitee / GitLab（含自托管，host 含平台关键词即识别）。
一期为单向导入：Issues → 项目任务队列（看板手动触发），不回写 Issue 状态。
去重以任务文件名为主键（issue-{平台}-{仓库}-{编号}），可存活任务全生命周期；
frontmatter 额外写 source 字段作人类可读溯源（看板编辑任务时会被重建，无碍去重）。
"""

from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx
import yaml

from . import USER_AGENT

# 单次拉取的分页上限：3 页 × 每页 50 条，覆盖绝大多数手工同步场景
_MAX_PAGES = 3
_PER_PAGE = 50
# Issue 正文写入任务文件的长度上限（防止超长 issue 撑爆上下文）
_MAX_BODY_CHARS = 20000

_PLATFORM_NAMES = {"github": "GitHub", "gitee": "Gitee",
                   "gitlab": "GitLab", "gitea": "Gitea"}


class IssueError(Exception):
    """Issues 拉取/导入失败的领域错误（Web 层直接转 400 提示）。"""


def detect_repo(url: str, provider: str | None = None) -> dict[str, str]:
    """从仓库 URL 解析平台与 owner/repo。

    provider 显式给出时跳过 host 关键词识别（自托管 Gitea/GitLab 等场景），
    否则按 host 含 github/gitee/gitlab 关键词判断；两者皆无则抛 IssueError。
    返回额外带 origin（协议+主机），供 Gitea 这类「API 与仓库同源」的平台使用。
    忽略 .git 后缀与末尾斜杠。
    """
    raw = (url or "").strip()
    m = re.match(r"^(https?://)([^/]+)/(.+?)/?$", raw)
    if not m:
        raise IssueError(
            f"无法识别仓库地址：{raw or '(空)'}。请填 https:// 开头的仓库 URL，"
            "支持 github.com / gitee.com / gitlab.com / Gitea（自托管请在平台里显式选择）。")
    scheme, host, path = m.group(1), m.group(2).lower(), m.group(3)
    prov = provider or next((p for p in ("github", "gitee", "gitlab") if p in host), None)
    if prov not in _PLATFORM_NAMES:
        raise IssueError(
            f"无法从主机名 {host} 识别平台（自托管 Gitea/GitLab 请在平台下拉里显式选择）。")
    parts = [p for p in path.split("/") if p and p != "-"]
    if len(parts) < 2:
        raise IssueError(f"仓库 URL 缺少 owner/repo 路径：{raw}")
    return {"provider": prov, "owner": parts[0],
            "repo": parts[1].removesuffix(".git"), "origin": scheme + host}


def normalize_repo(data: dict[str, Any], context: str = "repo") -> dict[str, Any]:
    """清洗/校验项目 repo 绑定字典：url 必填、provider 可显式、token 或
    账号密码（HTTP Basic）二选一认证、api_base 可选（自托管/测试桩）。

    供 load_config 与 Web 保存路径共用，保证两路行为一致。
    """
    url = str(data.get("url") or "").strip()
    if not url:
        raise IssueError(f"{context}.url 不能为空。")
    provider = str(data.get("provider") or "").strip() or None
    try:
        info = detect_repo(url, provider)
    except IssueError as e:
        raise IssueError(f"{context}：{e}") from e
    out: dict[str, Any] = {"url": url, "provider": info["provider"],
                           "token": str(data.get("token") or "").strip()}
    username = str(data.get("username") or "").strip()
    if username:
        out["username"] = username
        out["password"] = str(data.get("password") or "")
    api_base = str(data.get("api_base") or "").strip()
    if api_base:
        out["api_base"] = api_base
    return out


def _platform_label(provider: str) -> str:
    return _PLATFORM_NAMES.get(provider, provider)


def _headers_token_only(token: str) -> dict[str, str]:
    """仓库 token 可选：为空时不带认证头（公开仓库 + 免 httpx 空头值问题）。"""
    return {"Authorization": f"Bearer {token}"} if token else {}


def _normalize_labels(raw: Any) -> list[str]:
    """三平台的 labels 形态不同（github/gitee 是对象数组，gitlab 是字符串数组）。"""
    if not isinstance(raw, list):
        return []
    return [str(x.get("name") if isinstance(x, dict) else x).strip()
            for x in raw if str(x.get("name") if isinstance(x, dict) else x).strip()]


def _get_json(url: str, *, headers: dict[str, str] | None = None,
              params: dict[str, Any] | None = None,
              auth: tuple[str, str] | None = None) -> Any:
    # Windows 本机回环偶发 ConnectionResetError（快速连断竞态，见 e2e_skills 同款注释）：
    # 幂等 GET 重试最多 3 次，递进退避
    from urllib.parse import urlparse
    host = urlparse(url).hostname or ""
    trust_env = host not in ("127.0.0.1", "localhost", "::1")  # 环回地址不走系统代理
    headers = {"User-Agent": USER_AGENT, **(headers or {})}
    last_err: Exception | None = None
    resp = None
    for attempt in range(3):
        try:
            resp = httpx.get(url, headers=headers, params=params, timeout=30,
                             auth=auth, trust_env=trust_env)
            break
        except httpx.HTTPError as e:
            last_err = e
            if attempt < 2 and isinstance(e, httpx.TransportError):
                time.sleep(0.3 * (attempt + 1))
                continue
            raise IssueError(f"请求仓库 API 失败：{e}") from e
    if resp is None:  # 循环走完仍未拿到响应（理论上不会：异常分支均已 raise）
        raise IssueError(f"请求仓库 API 失败：{last_err}")
    if resp.status_code == 401:
        raise IssueError(f"仓库 API 认证失败（401）：token 缺失或无效。{url}")
    if resp.status_code == 403:
        raise IssueError(f"仓库 API 拒绝访问（403）：可能触发限流或无权限。{url}")
    if resp.status_code != 200:
        raise IssueError(f"仓库 API 返回 HTTP {resp.status_code}：{resp.text[:200]}")
    try:
        return resp.json()
    except ValueError as e:
        raise IssueError(f"仓库 API 响应不是 JSON：{url}") from e


def fetch_issues(repo: dict[str, Any]) -> list[dict[str, Any]]:
    """按平台拉取 open Issues，统一为 {provider, owner, repo, number, title,
    body, labels, url, author} 列表（已过滤 GitHub 的 PR 条目）。

    认证优先级：token > 账号密码（HTTP Basic）> 匿名（公开仓库）。
    GitHub 自 2020 年起不支持密码认证，只收 token。
    """
    info = detect_repo(str(repo.get("url") or ""),
                       str(repo.get("provider") or "") or None)
    provider = info["provider"]
    owner, repo_name = info["owner"], info["repo"]
    token = str(repo.get("token") or "")
    username = str(repo.get("username") or "").strip()
    password = str(repo.get("password") or "")
    basic = (username, password) if username and password else None
    api_base = str(repo.get("api_base") or "").rstrip("/")

    if provider == "github":
        if basic and not token:
            raise IssueError("GitHub API 不支持账号密码认证（2020 年起已移除），"
                             "请改填 Personal Access Token。")
        api = api_base or "https://api.github.com"
        url = f"{api}/repos/{owner}/{repo_name}/issues"
        # X-GitHub-Api-Version：官方推荐固定版本头，防 API 演进造成行为突变
        headers = {"Accept": "application/vnd.github+json",
                   "X-GitHub-Api-Version": "2022-11-28",
                   **_headers_token_only(token)}
        params = {"state": "open", "per_page": _PER_PAGE}
    elif provider == "gitee":
        api = api_base or "https://gitee.com/api/v5"
        url = f"{api}/repos/{owner}/{repo_name}/issues"
        headers = {}
        params = {"state": "open", "per_page": _PER_PAGE}
        if token:
            params["access_token"] = token
        elif not basic:
            pass  # 公开仓库匿名拉取
    elif provider == "gitea":
        # Gitea（含自托管）：API 与仓库同源，认证用 Authorization: token
        api = api_base or info["origin"]
        url = f"{api}/api/v1/repos/{owner}/{repo_name}/issues"
        headers = {"Authorization": f"token {token}"} if token else {}
        params = {"state": "open", "type": "issues", "limit": _PER_PAGE}
    else:  # gitlab
        api = api_base or "https://gitlab.com"
        url = f"{api}/api/v4/projects/{quote(f'{owner}/{repo_name}', safe='')}/issues"
        if token:
            headers = {"PRIVATE-TOKEN": token}
        else:
            headers = {}
        params = {"state": "opened", "per_page": _PER_PAGE}

    items: list[dict[str, Any]] = []
    for page in range(1, _MAX_PAGES + 1):
        data = _get_json(url, headers=headers, params={**params, "page": page},
                         auth=basic)
        if not isinstance(data, list) or not data:
            break
        items.extend(data)

    issues: list[dict[str, Any]] = []
    for it in items:
        # GitHub 只在 PR 条目上带 pull_request 键；Gitea 则对所有 issue 都带该键
        # （普通 issue 时值为 null）——必须按值过滤，不能按键存在过滤
        if not isinstance(it, dict) or it.get("pull_request"):
            continue
        number = it.get("number") or it.get("iid")
        if not number:
            continue
        issues.append({
            "provider": provider, "owner": owner, "repo": repo_name,
            "number": number,
            "title": str(it.get("title") or "").strip(),
            "body": str(it.get("body") or it.get("description") or ""),
            "labels": _normalize_labels(it.get("labels")),
            "url": str(it.get("html_url") or it.get("web_url") or ""),
            "author": str((it.get("user") or it.get("author") or {}).get("username")
                          or (it.get("user") or {}).get("login") or "unknown"),
        })
    return issues


def _task_stem(issue: dict[str, Any]) -> str:
    """issue → 任务文件名（去重主键）：合规字符 + 仓库段截断，防超长/非法字符。"""
    prov = issue["provider"]
    repo_slug = re.sub(r"[^\w.-]+", "-", issue["repo"])[:20].strip("-") or "repo"
    return f"issue-{prov}-{repo_slug}-{issue['number']}"


def _priority_from_labels(labels: list[str]) -> str:
    """Issue 标签里的 P0~P4 覆盖默认优先级；无则 P2。"""
    for lab in labels:
        m = re.fullmatch(r"[Pp]([0-4])", lab.strip())
        if m:
            return f"P{m.group(1)}"
    return "P2"


def build_task_file(issue: dict[str, Any]) -> tuple[str, str]:
    """issue → (文件名, 任务文件内容)。frontmatter 走 yaml.safe_dump 保证转义安全。"""
    stem = _task_stem(issue)
    title = re.sub(r"[\r\n]+", " ", issue["title"]).strip() or f"Issue #{issue['number']}"
    body = (issue["body"] or "").strip()[:_MAX_BODY_CHARS] or title
    labels = list(dict.fromkeys(issue["labels"]))[:10]
    meta = {
        "title": title,
        "priority": _priority_from_labels(labels),
        "status": "open",
        "labels": labels,
        "source": f"{issue['provider']} {issue['owner']}/{issue['repo']}#{issue['number']}",
    }
    head = "---\n" + yaml.safe_dump(meta, allow_unicode=True, sort_keys=False) + "---\n"
    intro = (f"> 来自 {_platform_label(issue['provider'])} Issue "
             f"#{issue['number']} · 作者 {issue['author']}"
             + (f" · {issue['url']}" if issue["url"] else "") + "\n\n")
    return stem + ".md", head + "\n" + intro + body + "\n"


def existing_task_stems(tasks_dir: Path) -> set[str]:
    """现有任务 stem 集合：含 done/ 归档——已处理过的 Issue 不重复导入。"""
    stems = {p.stem for p in tasks_dir.glob("*.md")}
    if (tasks_dir / "done").is_dir():
        stems |= {p.stem for p in (tasks_dir / "done").glob("*.md")}
    return stems


def sync_issues(project_name: str, repo: dict[str, Any], tasks_dir: Path) -> dict[str, int]:
    """拉取并导入：新 Issue 写任务文件，已存在的跳过。返回 {imported, skipped}。"""
    issues = fetch_issues(repo)
    known = existing_task_stems(tasks_dir)
    imported = skipped = 0
    for issue in issues:
        filename, content = build_task_file(issue)
        if Path(filename).stem in known:
            skipped += 1
            continue
        target = tasks_dir / filename
        try:
            tasks_dir.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        except OSError as e:
            raise IssueError(f"写入任务文件失败：{target}（{e}）") from e
        known.add(Path(filename).stem)
        imported += 1
    return {"imported": imported, "skipped": skipped,
            "total": len(issues), "project": project_name}
