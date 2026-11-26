"""外部审批 Provider 接口与 Phase 2A 假 Provider。

真实适配器（GitLab/GitHub/飞书/钉钉）在后续阶段实现；本模块先固定协议：
Provider 只负责「创建审批 → 轮询/回调 → 返回谁批准了」，Orcsy 负责 email
白名单、quorum、digest 与 fail-closed 校验。

回调统一使用 `X-Orcsy-Signature: sha256=<hex>` + `X-Orcsy-Timestamp: <epoch>`；
HMAC 覆盖 `timestamp + "." + raw_body`。真实 Provider 若有自己的签名格式，
适配器先验自己的签名，再归一化成 ProviderDecision。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from dataclasses import dataclass, field
from typing import Any, Protocol
from urllib.parse import quote

import httpx

from .config import ApprovalProviderConfig

CALLBACK_SIGNATURE_HEADER = "X-Orcsy-Signature"
CALLBACK_TIMESTAMP_HEADER = "X-Orcsy-Timestamp"
CALLBACK_WINDOW_SECONDS = 300


class ProviderError(RuntimeError):
    """Provider 不可达、协议错误或签名无效；调用方必须 fail closed。"""


@dataclass
class ApproverDecision:
    provider: str
    id: str
    name: str = ""
    decision: str = "approved"       # approved / rejected
    at: str = ""


@dataclass
class ProviderDecision:
    status: str                      # pending / approved / rejected
    approvers: list[ApproverDecision] = field(default_factory=list)
    comment: str = ""
    digest: str = ""
    external_id: str = ""
    request_id: str = ""


@dataclass
class ProviderHandle:
    external_id: str
    url: str = ""
    state: dict[str, Any] = field(default_factory=dict)


class ApprovalProvider(Protocol):
    type: str

    def create(self, manifest: dict) -> ProviderHandle: ...
    def poll(self, handle: ProviderHandle) -> ProviderDecision | None: ...
    def verify_callback(self, headers: dict[str, str],
                        raw_body: bytes) -> ProviderDecision: ...
    def cancel(self, handle: ProviderHandle) -> None: ...


def _http_json(method: str, url: str, *, headers: dict[str, str],
               body: dict | None = None, verify: bool = True,
               timeout: int = 10) -> Any:
    """Provider 出站请求统一过 egress；任何网络/HTTP 错误转 ProviderError。"""
    from . import egress
    try:
        egress.check(url, purpose="approval_provider")
    except Exception as exc:  # noqa: BLE001 — EgressDenied 等
        raise ProviderError(f"出网白名单拒绝：{exc}") from exc
    try:
        with httpx.Client(verify=verify, timeout=timeout,
                          follow_redirects=True) as client:
            resp = client.request(method, url, headers=headers, json=body)
    except httpx.HTTPError as exc:
        raise ProviderError(f"Provider 请求失败：{exc}") from exc
    if resp.status_code >= 400:
        text = resp.text[:300].replace("\n", " ")
        raise ProviderError(f"Provider HTTP {resp.status_code}：{text}")
    try:
        return resp.json()
    except ValueError as exc:
        raise ProviderError("Provider 返回不是有效 JSON") from exc


def _parse_approval_command(body: str) -> tuple[str, str]:
    """解析评论首行的 /approve 或 /reject；返回 (decision, comment)。"""
    text = str(body or "").strip()
    if not text:
        return "", ""
    first, _, rest = text.partition("\n")
    parts = first.strip().split(maxsplit=1)
    command = parts[0].lower() if parts else ""
    comment = parts[1].strip() if len(parts) > 1 else ""
    if command in ("/approve", "/lgtm"):
        return "approved", comment
    if command in ("/reject", "/deny"):
        return "rejected", comment
    return "", ""


def _issue_title(manifest: dict) -> str:
    return (f"[Orcsy 审批] {manifest.get('project', '')}/"
            f"{manifest.get('task_id', '')} · "
            f"{manifest.get('risk', {}).get('level', '')}")


def _issue_body(manifest: dict) -> str:
    risk = manifest.get("risk") or {}
    emails = ", ".join(manifest.get("allowed_approvers") or [])
    payload = json.dumps(manifest, ensure_ascii=False, indent=2)
    return "\n".join([
        "## Orcsy 合入前风险审批",
        "",
        f"- 请求：`{manifest.get('request_id', '')}`",
        f"- 项目 / 任务：{manifest.get('project', '')} / "
        f"{manifest.get('task_id', '')}",
        f"- 标题：{manifest.get('title', '')}",
        f"- 风险等级：**{risk.get('level', '')}**",
        f"- Commit：`{manifest.get('commit', '')}`",
        f"- Digest：`{manifest.get('digest', '')}`",
        f"- 需要会签：{manifest.get('required_approvals', 0)} 人",
        f"- 允许审批 email：{emails or '（未配置）'}",
        "",
        "审批方式：在本 issue 评论 `/approve` 或 `/reject 原因`。",
        "只有 Orcsy 配置中登记的 email 对应账号会计入 quorum；"
        "未登记账号的评论会被忽略。",
        "",
        "```json",
        payload,
        "```",
    ])


class GitLabProvider:
    """GitLab 审批工单（issue + 评论），不推送代码分支。"""

    type = "gitlab"

    def __init__(self, name: str, config: ApprovalProviderConfig):
        self.name = name
        self.config = config
        self.base = (config.base_url or "https://gitlab.com").rstrip("/")
        if not config.project_id:
            raise ProviderError(f"GitLab Provider {name} 缺少 project_id")
        self.project = quote(str(config.project_id), safe="")

    def _headers(self) -> dict[str, str]:
        return {"PRIVATE-TOKEN": self.config.token,
                "Content-Type": "application/json"}

    def create(self, manifest: dict) -> ProviderHandle:
        url = f"{self.base}/api/v4/projects/{self.project}/issues"
        data = _http_json(
            "POST", url, headers=self._headers(), body={
                "title": _issue_title(manifest),
                "description": _issue_body(manifest),
                "labels": "orcsy-approval",
            }, verify=self.config.verify_tls,
            timeout=self.config.timeout_seconds)
        iid = data.get("iid")
        if not iid:
            raise ProviderError("GitLab 创建 issue 未返回 iid")
        return ProviderHandle(
            external_id=str(iid), url=str(data.get("web_url") or ""),
            state={"digest": manifest.get("digest", ""),
                   "request_id": manifest.get("request_id", "")})

    def poll(self, handle: ProviderHandle) -> ProviderDecision | None:
        url = (f"{self.base}/api/v4/projects/{self.project}/issues/"
               f"{handle.external_id}/notes?per_page=100&sort=asc&order_by=created_at")
        notes = _http_json("GET", url, headers=self._headers(),
                           verify=self.config.verify_tls,
                           timeout=self.config.timeout_seconds)
        if not isinstance(notes, list):
            raise ProviderError("GitLab notes 返回格式异常")
        approvers: list[ApproverDecision] = []
        for note in notes:
            if note.get("system"):
                continue
            decision, _ = _parse_approval_command(note.get("body") or "")
            if not decision:
                continue
            author = note.get("author") or {}
            username = str(author.get("username") or "")
            if not username:
                continue
            approvers.append(ApproverDecision(
                provider="gitlab", id=username,
                name=str(author.get("name") or username),
                decision=decision, at=str(note.get("created_at") or "")))
        return ProviderDecision(
            status="pending", approvers=approvers,
            digest=str(handle.state.get("digest") or ""),
            external_id=handle.external_id)

    def verify_callback(self, headers: dict[str, str],
                        raw_body: bytes) -> ProviderDecision:
        h = _headers_lower(headers)
        token = h.get("x-gitlab-token", "")
        if not self.config.callback_secret or not hmac.compare_digest(
                token, self.config.callback_secret):
            raise ProviderError("GitLab Webhook token 不匹配")
        try:
            payload = json.loads(raw_body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise ProviderError("GitLab Webhook 不是有效 JSON") from exc
        if payload.get("object_kind") != "note":
            raise ProviderError("仅支持 GitLab note Webhook")
        attrs = payload.get("object_attributes") or {}
        issue = payload.get("issue") or {}
        decision, _ = _parse_approval_command(attrs.get("note") or "")
        user = payload.get("user") or {}
        approvers = []
        if decision and user.get("username"):
            approvers.append(ApproverDecision(
                provider="gitlab", id=str(user.get("username")),
                name=str(user.get("name") or user.get("username")),
                decision=decision, at=str(attrs.get("created_at") or "")))
        return ProviderDecision(
            status="pending", approvers=approvers,
            external_id=str(issue.get("iid") or ""))

    def cancel(self, handle: ProviderHandle) -> None:
        try:
            _http_json(
                "PUT",
                f"{self.base}/api/v4/projects/{self.project}/issues/"
                f"{handle.external_id}",
                headers=self._headers(), body={"state_event": "close"},
                verify=self.config.verify_tls,
                timeout=self.config.timeout_seconds)
        except ProviderError:
            return


class GitHubProvider:
    """GitHub 审批工单（issue + 评论），不推送代码分支。"""

    type = "github"

    def __init__(self, name: str, config: ApprovalProviderConfig):
        self.name = name
        self.config = config
        self.base = (config.base_url or "https://api.github.com").rstrip("/")
        if not config.repo or "/" not in config.repo:
            raise ProviderError(f"GitHub Provider {name} 的 repo 必须是 owner/name")
        self.repo = config.repo

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.config.token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "Content-Type": "application/json",
        }

    def create(self, manifest: dict) -> ProviderHandle:
        url = f"{self.base}/repos/{self.repo}/issues"
        data = _http_json(
            "POST", url, headers=self._headers(), body={
                "title": _issue_title(manifest),
                "body": _issue_body(manifest),
                "labels": ["orcsy-approval"],
            }, verify=self.config.verify_tls,
            timeout=self.config.timeout_seconds)
        number = data.get("number")
        if not number:
            raise ProviderError("GitHub 创建 issue 未返回 number")
        return ProviderHandle(
            external_id=str(number), url=str(data.get("html_url") or ""),
            state={"digest": manifest.get("digest", ""),
                   "request_id": manifest.get("request_id", "")})

    def poll(self, handle: ProviderHandle) -> ProviderDecision | None:
        url = (f"{self.base}/repos/{self.repo}/issues/"
               f"{handle.external_id}/comments?per_page=100")
        notes = _http_json("GET", url, headers=self._headers(),
                           verify=self.config.verify_tls,
                           timeout=self.config.timeout_seconds)
        if not isinstance(notes, list):
            raise ProviderError("GitHub comments 返回格式异常")
        approvers: list[ApproverDecision] = []
        for note in notes:
            decision, _ = _parse_approval_command(note.get("body") or "")
            if not decision:
                continue
            user = note.get("user") or {}
            login = str(user.get("login") or "")
            if not login:
                continue
            approvers.append(ApproverDecision(
                provider="github", id=login,
                name=str(user.get("name") or login),
                decision=decision, at=str(note.get("created_at") or "")))
        return ProviderDecision(
            status="pending", approvers=approvers,
            digest=str(handle.state.get("digest") or ""),
            external_id=handle.external_id)

    def verify_callback(self, headers: dict[str, str],
                        raw_body: bytes) -> ProviderDecision:
        h = _headers_lower(headers)
        sig = h.get("x-hub-signature-256", "")
        if not self.config.callback_secret or not sig.startswith("sha256="):
            raise ProviderError("GitHub Webhook 缺少签名")
        expected = hmac.new(
            self.config.callback_secret.encode("utf-8"), raw_body,
            hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig[7:].lower(), expected):
            raise ProviderError("GitHub Webhook 签名不匹配")
        try:
            payload = json.loads(raw_body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise ProviderError("GitHub Webhook 不是有效 JSON") from exc
        if payload.get("action") != "created":
            raise ProviderError("仅支持 GitHub issue_comment created")
        comment = payload.get("comment") or {}
        issue = payload.get("issue") or {}
        decision, _ = _parse_approval_command(comment.get("body") or "")
        user = comment.get("user") or {}
        approvers = []
        if decision and user.get("login"):
            approvers.append(ApproverDecision(
                provider="github", id=str(user.get("login")),
                name=str(user.get("login")), decision=decision,
                at=str(comment.get("created_at") or "")))
        return ProviderDecision(
            status="pending", approvers=approvers,
            external_id=str(issue.get("number") or ""))

    def cancel(self, handle: ProviderHandle) -> None:
        try:
            _http_json(
                "PATCH",
                f"{self.base}/repos/{self.repo}/issues/{handle.external_id}",
                headers=self._headers(), body={"state": "closed"},
                verify=self.config.verify_tls,
                timeout=self.config.timeout_seconds)
        except ProviderError:
            return


def _headers_lower(headers: dict[str, str]) -> dict[str, str]:
    return {str(k).lower(): str(v) for k, v in (headers or {}).items()}


def verify_callback_signature(secret: str, headers: dict[str, str],
                              raw_body: bytes,
                              window: int = CALLBACK_WINDOW_SECONDS) -> None:
    """校验统一回调签名；失败抛 ProviderError。"""
    if not secret:
        raise ProviderError("Provider 未配置 callback_secret，拒绝回调")
    h = _headers_lower(headers)
    sig = str(h.get(CALLBACK_SIGNATURE_HEADER.lower()) or "").strip()
    ts = str(h.get(CALLBACK_TIMESTAMP_HEADER.lower()) or "").strip()
    if not sig.startswith("sha256=") or not ts:
        raise ProviderError("回调缺少签名或时间戳")
    try:
        timestamp = int(ts)
    except ValueError as exc:
        raise ProviderError("回调时间戳不是整数") from exc
    if abs(time.time() - timestamp) > window:
        raise ProviderError("回调时间戳超出允许窗口")
    expected = hmac.new(
        secret.encode("utf-8"), ts.encode("ascii") + b"." + raw_body,
        hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig[7:].lower(), expected):
        raise ProviderError("回调签名不匹配")


def sign_callback(secret: str, raw_body: bytes,
                  timestamp: int | None = None) -> tuple[dict[str, str], bytes]:
    """生成统一回调头（测试/本地自测用）。"""
    ts = str(int(time.time() if timestamp is None else timestamp))
    digest = hmac.new(
        secret.encode("utf-8"), ts.encode("ascii") + b"." + raw_body,
        hashlib.sha256).hexdigest()
    return {CALLBACK_SIGNATURE_HEADER: f"sha256={digest}",
            CALLBACK_TIMESTAMP_HEADER: ts}, raw_body


class FakeProvider:
    """本地假 Provider：create 后由测试/本地自测注入决定。"""

    type = "fake"

    def __init__(self, name: str, config: ApprovalProviderConfig | None = None):
        self.name = name
        self.config = config or ApprovalProviderConfig(type="fake")
        self._decisions: dict[str, ProviderDecision] = {}

    def create(self, manifest: dict) -> ProviderHandle:
        external_id = f"fake-{manifest.get('request_id') or int(time.time())}"
        return ProviderHandle(external_id=external_id,
                              url=f"fake://{self.name}/{external_id}")

    def set_decision(self, external_id: str, decision: ProviderDecision) -> None:
        self._decisions[external_id] = decision

    def poll(self, handle: ProviderHandle) -> ProviderDecision | None:
        return self._decisions.get(handle.external_id)

    def verify_callback(self, headers: dict[str, str],
                        raw_body: bytes) -> ProviderDecision:
        verify_callback_signature(self.config.callback_secret, headers, raw_body)
        try:
            payload = json.loads(raw_body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise ProviderError("假 Provider 回调不是有效 JSON") from exc
        return decision_from_payload(payload)

    def cancel(self, handle: ProviderHandle) -> None:
        self._decisions.pop(handle.external_id, None)


class FeishuProvider:
    """飞书审批实例（轮询）；Webhook 暂不启用，避免依赖事件订阅配置。"""

    type = "feishu"

    def __init__(self, name: str, config: ApprovalProviderConfig):
        self.name = name
        self.config = config
        self.base = (config.base_url or "https://open.feishu.cn").rstrip("/")
        self._token = ""
        self._token_expires = 0.0

    def _access_token(self) -> str:
        if self._token and time.time() < self._token_expires:
            return self._token
        data = _http_json(
            "POST",
            f"{self.base}/open-apis/auth/v3/tenant_access_token/internal",
            headers={"Content-Type": "application/json"},
            body={"app_id": self.config.app_id,
                  "app_secret": self.config.app_secret},
            verify=self.config.verify_tls,
            timeout=self.config.timeout_seconds)
        token = str(data.get("tenant_access_token") or "")
        if not token:
            raise ProviderError("飞书未返回 tenant_access_token")
        self._token = token
        self._token_expires = time.time() + max(
            60, int(data.get("expire") or 7200) - 120)
        return token

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._access_token()}",
                "Content-Type": "application/json"}

    def create(self, manifest: dict) -> ProviderHandle:
        summary = (f"{manifest.get('project')}/{manifest.get('task_id')} · "
                   f"{manifest.get('risk', {}).get('level')} · "
                   f"digest {manifest.get('digest', '')[:16]}")
        form = "{}"
        if self.config.form_widget_id:
            form = json.dumps({self.config.form_widget_id: summary},
                              ensure_ascii=False)
        data = _http_json(
            "POST", f"{self.base}/open-apis/approval/v4/instances",
            headers=self._headers(), body={
                "approval_code": self.config.approval_code,
                "user_id": self.config.originator_user_id,
                "form": form,
                "uuid": manifest.get("request_id", ""),
            }, verify=self.config.verify_tls,
            timeout=self.config.timeout_seconds)
        payload = data.get("data") if isinstance(data.get("data"), dict) else data
        code = str(payload.get("instance_code") or "")
        if not code:
            raise ProviderError("飞书创建审批未返回 instance_code")
        return ProviderHandle(
            external_id=code, url=str(payload.get("instance_url") or ""),
            state={"digest": manifest.get("digest", ""),
                   "request_id": manifest.get("request_id", "")})

    def poll(self, handle: ProviderHandle) -> ProviderDecision | None:
        data = _http_json(
            "GET",
            f"{self.base}/open-apis/approval/v4/instances/{handle.external_id}",
            headers=self._headers(), verify=self.config.verify_tls,
            timeout=self.config.timeout_seconds)
        inst = data.get("data") if isinstance(data.get("data"), dict) else data
        tasks = inst.get("task_list") or inst.get("tasks") or []
        approvers: list[ApproverDecision] = []
        for task in tasks:
            status = str(task.get("status") or "").upper()
            if status not in ("APPROVED", "REJECTED"):
                continue
            uid = str(task.get("user_id") or task.get("open_id") or "")
            if not uid:
                continue
            approvers.append(ApproverDecision(
                provider="feishu", id=uid,
                name=str(task.get("user_name") or uid),
                decision="approved" if status == "APPROVED" else "rejected",
                at=str(task.get("operate_time") or task.get("update_time") or "")))
        return ProviderDecision(
            status="pending", approvers=approvers,
            digest=str(handle.state.get("digest") or ""),
            external_id=handle.external_id)

    def verify_callback(self, headers: dict[str, str],
                        raw_body: bytes) -> ProviderDecision:
        raise ProviderError("飞书审批回调暂未启用，请使用轮询")

    def cancel(self, handle: ProviderHandle) -> None:
        return


class DingTalkProvider:
    """钉钉审批实例（轮询）；Webhook 暂不启用。"""

    type = "dingtalk"

    def __init__(self, name: str, config: ApprovalProviderConfig):
        self.name = name
        self.config = config
        self.base = (config.base_url or "https://oapi.dingtalk.com").rstrip("/")
        self._token = ""
        self._token_expires = 0.0

    def _access_token(self) -> str:
        if self._token and time.time() < self._token_expires:
            return self._token
        url = (f"{self.base}/gettoken?appkey={quote(self.config.app_key)}"
               f"&appsecret={quote(self.config.app_secret)}")
        data = _http_json("GET", url, headers={},
                          verify=self.config.verify_tls,
                          timeout=self.config.timeout_seconds)
        token = str(data.get("access_token") or "")
        if not token:
            raise ProviderError("钉钉未返回 access_token")
        self._token = token
        self._token_expires = time.time() + 3600
        return token

    def create(self, manifest: dict) -> ProviderHandle:
        data = _http_json(
            "POST",
            f"{self.base}/topapi/processinstance/create"
            f"?access_token={quote(self._access_token())}",
            headers={"Content-Type": "application/json"}, body={
                "process_code": self.config.process_code,
                "originator_user_id": self.config.originator_user_id,
                "dept_id": int(self.config.dept_id),
                "form_component_values": [],
            }, verify=self.config.verify_tls,
            timeout=self.config.timeout_seconds)
        code = str(data.get("process_instance_id") or "")
        if not code:
            raise ProviderError("钉钉创建审批未返回 process_instance_id")
        return ProviderHandle(
            external_id=code, url="",
            state={"digest": manifest.get("digest", ""),
                   "request_id": manifest.get("request_id", "")})

    def poll(self, handle: ProviderHandle) -> ProviderDecision | None:
        data = _http_json(
            "POST",
            f"{self.base}/topapi/processinstance/get"
            f"?access_token={quote(self._access_token())}",
            headers={"Content-Type": "application/json"},
            body={"process_instance_id": handle.external_id},
            verify=self.config.verify_tls,
            timeout=self.config.timeout_seconds)
        inst = data.get("process_instance") or {}
        records = inst.get("operation_records") or []
        approvers: list[ApproverDecision] = []
        for rec in records:
            result = str(rec.get("result") or "").upper()
            if result not in ("AGREE", "REFUSE"):
                continue
            uid = str(rec.get("userid") or rec.get("user_id") or "")
            if not uid:
                continue
            approvers.append(ApproverDecision(
                provider="dingtalk", id=uid,
                name=str(rec.get("user_name") or uid),
                decision="approved" if result == "AGREE" else "rejected",
                at=str(rec.get("date") or rec.get("create_time") or "")))
        return ProviderDecision(
            status="pending", approvers=approvers,
            digest=str(handle.state.get("digest") or ""),
            external_id=handle.external_id)

    def verify_callback(self, headers: dict[str, str],
                        raw_body: bytes) -> ProviderDecision:
        raise ProviderError("钉钉审批回调暂未启用，请使用轮询")

    def cancel(self, handle: ProviderHandle) -> None:
        return


class UnimplementedProvider:
    """真实适配器尚未接入时的占位：任何操作都 fail closed。"""

    def __init__(self, name: str, config: ApprovalProviderConfig,
                 ptype: str):
        self.name = name
        self.config = config
        self.type = ptype

    def create(self, manifest: dict) -> ProviderHandle:
        raise ProviderError(
            f"Provider {self.name}（{self.type}）适配器尚未实现；"
            "请在 2B/2C 接入后再启用 quorum")

    def poll(self, handle: ProviderHandle) -> ProviderDecision | None:
        raise ProviderError("Provider 适配器尚未实现")

    def verify_callback(self, headers: dict[str, str],
                        raw_body: bytes) -> ProviderDecision:
        raise ProviderError("Provider 适配器尚未实现")

    def cancel(self, handle: ProviderHandle) -> None:
        return


def decision_from_payload(payload: dict) -> ProviderDecision:
    """把统一回调 JSON 归一化为 ProviderDecision。"""
    if not isinstance(payload, dict):
        raise ProviderError("回调 payload 必须是 JSON 对象")
    status = str(payload.get("decision") or payload.get("status") or "").lower()
    if status not in ("approved", "rejected", "pending"):
        raise ProviderError(f"未知审批状态：{status or '空'}")
    approvers: list[ApproverDecision] = []
    raw_items = payload.get("approvers") or []
    if not isinstance(raw_items, list):
        raise ProviderError("approvers 必须是数组")
    for item in raw_items:
        if not isinstance(item, dict):
            raise ProviderError("approvers 条目必须是对象")
        decision = str(item.get("decision") or "approved").lower()
        if decision not in ("approved", "rejected"):
            raise ProviderError(f"approver decision 非法：{decision}")
        approvers.append(ApproverDecision(
            provider=str(item.get("provider") or ""),
            id=str(item.get("id") or ""),
            name=str(item.get("name") or ""),
            decision=decision,
            at=str(item.get("at") or ""),
        ))
    return ProviderDecision(
        status=status, approvers=approvers,
        comment=str(payload.get("comment") or ""),
        digest=str(payload.get("digest") or ""),
        external_id=str(payload.get("external_id") or ""),
        request_id=str(payload.get("request_id") or ""),
    )


def build_provider(name: str, config: ApprovalProviderConfig):
    if config.type == "fake":
        return FakeProvider(name, config)
    if config.type == "gitlab":
        return GitLabProvider(name, config)
    if config.type == "github":
        return GitHubProvider(name, config)
    if config.type == "feishu":
        return FeishuProvider(name, config)
    if config.type == "dingtalk":
        return DingTalkProvider(name, config)
    return UnimplementedProvider(name, config, config.type)


def resolve_email(approvers: dict[str, dict[str, str]], provider_type: str,
                  identity: str) -> str | None:
    """Provider 身份 → email；未登记返回 None。"""
    wanted = str(identity or "").casefold()
    if not wanted:
        return None
    for email, ids in approvers.items():
        if str(ids.get(provider_type) or "").casefold() == wanted:
            return email
    return None
