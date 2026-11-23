"""通用存储 provider：本地目录 / WebDAV / S3 兼容。

1.0 稳定化 S2（配置备份）与项目归档（评估⑤）共用同一套 provider：备份/归档
只面向 ``StorageProvider`` 的 put/get/list/delete，具体落地按 target 类型分派。

- 远程请求（WebDAV/S3）一律先过 `egress.check`（修复历史 WebDAV 直连绕过出网
  白名单的问题）。
- S3 用 stdlib hmac/hashlib 实现 AWS SigV4，无第三方依赖；支持 path-style 与
  virtual-hosted 两种寻址（大多数 S3 兼容空间用 path-style）。
- 凭据由调用方明文传入；看板只做 APU 掩码（mask_secret）导出，磁盘保持明文
  （与现有 llm.api_key 一致），本模块不负责脱敏。
"""

from __future__ import annotations

import hashlib
import hmac
import pathlib
import urllib.parse
from datetime import datetime, timezone
from typing import Protocol

import httpx

AWS_UNSIGNED = "UNSIGNED-PAYLOAD"
_SIGV4 = "AWS4-HMAC-SHA256"


class StorageError(Exception):
    """存储 provider 通用异常（鉴权/网络/协议/参数错误）。"""


class StorageProvider(Protocol):
    """最小存储接口：上传/下载必现，list/delete 由实现按需提供。"""

    kind: str

    def put(self, key: str, data: bytes,
            *, content_type: str = "application/octet-stream") -> str: ...

    def get(self, key: str) -> bytes: ...

    def list(self, prefix: str = "") -> list[str]: ...

    def delete(self, key: str) -> None: ...


# --------------------------------------------------------------------------
# 工厂
# --------------------------------------------------------------------------
def build_provider(target,
                   *, base_dir: pathlib.Path | None = None) -> StorageProvider:
    """按 target（鸭类型，兼容 config.StorageTargetConfig）构建 provider。

    ``dir`` 为相对路径时以 base_dir（配置目录）为锚点解析。
    """
    kind = str(getattr(target, "type", "") or "").strip().lower()
    if kind in ("", "local"):
        d = str(getattr(target, "dir", "") or "").strip()
        p = pathlib.Path(d if d else "backups")
        if not p.is_absolute() and base_dir is not None:
            p = base_dir / p
        return LocalProvider(p)
    if kind == "webdav":
        return WebDAVProvider(
            url=str(getattr(target, "url", "") or ""),
            username=str(getattr(target, "username", "") or ""),
            password=str(getattr(target, "password", "") or ""),
        )
    if kind == "s3":
        return S3Provider(
            endpoint=str(getattr(target, "endpoint", "") or "").strip(),
            bucket=str(getattr(target, "bucket", "") or ""),
            region=str(getattr(target, "region", "") or "us-east-1"),
            access_key=str(getattr(target, "access_key", "") or ""),
            secret_key=str(getattr(target, "secret_key", "") or ""),
            session_token=str(getattr(target, "session_token", "") or "") or None,
        )
    raise StorageError(f"未知存储类型：{kind!r}")


# --------------------------------------------------------------------------
# 本地目录
# --------------------------------------------------------------------------
class LocalProvider:
    kind = "local"

    def __init__(self, root: pathlib.Path) -> None:
        self.root = pathlib.Path(root)

    def _safe(self, key: str) -> pathlib.Path:
        p = pathlib.Path(key)
        if p.is_absolute() or ".." in p.parts:
            raise StorageError(f"非法本地键：{key!r}")
        return self.root / p

    def put(self, key: str, data: bytes,
            *, content_type: str = "application/octet-stream") -> str:
        dest = self._safe(key)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
        try:
            dest.chmod(0o600)
        except OSError:
            pass
        return str(dest)

    def get(self, key: str) -> bytes:
        return self._safe(key).read_bytes()

    def list(self, prefix: str = "") -> list[str]:
        base = self._safe(prefix) if prefix else self.root
        if not base.exists():
            return []
        out: list[str] = []
        for p in base.rglob("*"):
            if p.is_file():
                out.append(str(p.relative_to(self.root)).replace("\\", "/"))
        return out

    def delete(self, key: str) -> None:
        try:
            self._safe(key).unlink()
        except FileNotFoundError:
            pass


# --------------------------------------------------------------------------
# WebDAV（PUT/GET/DELETE；PROPFIND 列目录不实现，远端裁剪需调用侧自行枚举）
# --------------------------------------------------------------------------
class WebDAVProvider:
    kind = "webdav"

    def __init__(self, url: str, username: str = "", password: str = "") -> None:
        url = (url or "").strip()
        if not url.startswith(("http://", "https://")):
            raise StorageError("webdav.url 必须以 http(s):// 开头")
        self.url = url.rstrip("/")
        self.auth = (username, password) if username else None

    def _url(self, key: str) -> str:
        return f"{self.url}/{key.lstrip('/')}"

    def put(self, key: str, data: bytes,
            *, content_type: str = "application/gzip") -> str:
        url = self._url(key)
        _egress_check(url)
        resp = httpx.put(url, content=data,
                         headers={"Content-Type": content_type},
                         auth=self.auth, timeout=120)
        resp.raise_for_status()
        return url

    def get(self, key: str) -> bytes:
        url = self._url(key)
        _egress_check(url)
        resp = httpx.get(url, auth=self.auth, timeout=120)
        resp.raise_for_status()
        return resp.content

    def list(self, prefix: str = "") -> list[str]:
        """PROPFIND Depth 1 列出直接子项名称（相对 base，已 URL 解码）。"""
        import xml.etree.ElementTree as ET

        url = self.url.rstrip("/") + "/"
        _egress_check(url)
        resp = httpx.request("PROPFIND", url, auth=self.auth,
                             headers={"Depth": "1"}, timeout=30)
        resp.raise_for_status()
        ns = {"d": "DAV:"}
        root = ET.fromstring(resp.content)
        base = urllib.parse.urlsplit(self.url).path.rstrip("/")
        out: list[str] = []
        for href in root.findall(".//d:href", ns):
            raw = (href.text or "").strip()
            p = urllib.parse.urlsplit(raw).path
            if p == base or not p.startswith(base + "/"):
                continue
            rel = p[len(base) + 1:].strip("/")
            if rel.startswith(prefix):
                out.append(urllib.parse.unquote(rel))
        return out

    def delete(self, key: str) -> None:
        url = self._url(key)
        _egress_check(url)
        resp = httpx.delete(url, auth=self.auth, timeout=120)
        resp.raise_for_status()


def _egress_check(url: str) -> None:
    from . import egress
    egress.check(url, purpose="backup")


# --------------------------------------------------------------------------
# S3（SigV4，stdlib hmac/hashlib）
# --------------------------------------------------------------------------
class S3Provider:
    kind = "s3"

    def __init__(self, *, endpoint: str, bucket: str, region: str = "us-east-1",
                 access_key: str = "", secret_key: str = "",
                 session_token: str | None = None) -> None:
        if not bucket:
            raise StorageError("s3.bucket 不能为空")
        if not access_key or not secret_key:
            raise StorageError("s3.access_key / secret_key 不能为空")
        self.endpoint = endpoint.rstrip("/") if endpoint else ""
        self.bucket = bucket
        self.region = region or "us-east-1"
        self.access_key = access_key
        self.secret_key = secret_key
        self.session_token = session_token
        # endpoint 给定 = path-style（MinIO 等）；空 = AWS virtual-host
        self.path_style = bool(self.endpoint)

    def _object_url(self, key: str) -> str:
        # 带点的桶走 virtual-host 会破坏 TLS 证书，AWS 亦要求 path-style
        use_path_style = self.path_style or "." in self.bucket
        snippet = urllib.parse.quote(key, safe="/")
        if use_path_style:
            base = self.endpoint or f"https://s3.{self.region}.amazonaws.com"
            return f"{base}/{urllib.parse.quote(self.bucket, safe='')}/{snippet}"
        return f"https://{self.bucket}.s3.{self.region}.amazonaws.com/{snippet}"

    def _request(self, method: str, key: str, body: bytes = b"",
                 *, content_type: str = "application/octet-stream") -> httpx.Response:
        url = self._object_url(key)
        _egress_check(url)
        headers = {"Content-Type": content_type}
        headers = sign_request(
            method=method, url=url, body=body, headers=headers,
            region=self.region, service="s3",
            access_key=self.access_key, secret_key=self.secret_key,
            session_token=self.session_token,
        )
        resp = httpx.request(method, url, content=body, headers=headers, timeout=120)
        resp.raise_for_status()
        return resp

    def put(self, key: str, data: bytes,
            *, content_type: str = "application/octet-stream") -> str:
        self._request("PUT", key, data, content_type=content_type)
        return self._object_url(key)

    def get(self, key: str) -> bytes:
        return self._request("GET", key).content

    def list(self, prefix: str = "") -> list[str]:
        """ListObjectsV2 列出对象键（已 URL 解码；单页 ≤1000，归档场景够用）。"""
        import xml.etree.ElementTree as ET

        if self.path_style or "." in self.bucket:
            base = self.endpoint or f"https://s3.{self.region}.amazonaws.com"
            url = f"{base}/{urllib.parse.quote(self.bucket, safe='')}"
        else:
            url = f"https://{self.bucket}.s3.{self.region}.amazonaws.com"
        params = {"list-type": "2"}
        if prefix:
            params["prefix"] = prefix
        url = url + "?" + urllib.parse.urlencode(params)
        _egress_check(url)
        headers = sign_request(method="GET", url=url, body=b"", headers={},
                               region=self.region, service="s3",
                               access_key=self.access_key, secret_key=self.secret_key,
                               session_token=self.session_token)
        resp = httpx.request("GET", url, headers=headers, timeout=60)
        resp.raise_for_status()
        ns = {"s": "http://s3.amazonaws.com/doc/2006-03-01/"}
        root = ET.fromstring(resp.content)
        return [urllib.parse.unquote(k.text) for k in root.findall(".//s:Key", ns) if k.text]

    def delete(self, key: str) -> None:
        self._request("DELETE", key)


# --------------------------------------------------------------------------
# SigV4（暴露内部函数便于测试；无第三方依赖）
# --------------------------------------------------------------------------
def _uri_encode(path: str) -> str:
    return urllib.parse.quote(path, safe="/")


def canonical_request(method: str, url: str, headers: dict[str, str],
                      payload_hash: str) -> str:
    """构造 AWS SigV4 规范请求（用于测试对拍官方样例）。"""
    parsed = urllib.parse.urlsplit(url)
    path = parsed.path or "/"
    query = sorted(urllib.parse.parse_qsl(parsed.query, keep_blank_values=True))
    query_str = urllib.parse.urlencode([(k, v) for k, v in query], quote_via=urllib.parse.quote)
    header_names = sorted(k.strip().lower() for k in headers)
    header_lines = [
        f"{name}:{' '.join(str(headers[name]).strip().split())}"
        for name in header_names
    ]
    signed = ";".join(header_names)
    return "\n".join([
        method.upper(),
        _uri_encode(path),
        query_str,
        *header_lines,
        "",
        signed,
        payload_hash,
    ])


def _signing_key(secret: str, date: str, region: str, service: str) -> bytes:
    k_date = hmac.new(("AWS4" + secret).encode(), date.encode(), hashlib.sha256).digest()
    k_region = hmac.new(k_date, region.encode(), hashlib.sha256).digest()
    k_service = hmac.new(k_region, service.encode(), hashlib.sha256).digest()
    return hmac.new(k_service, b"aws4_request", hashlib.sha256).digest()


def sign_request(*, method: str, url: str, body: bytes, headers: dict[str, str],
                 region: str, service: str, access_key: str, secret_key: str,
                 session_token: str | None = None,
                 now: datetime | None = None) -> dict[str, str]:
    """为一次 S3 请求附上 SigV4 鉴权头，返回应发送的完整头部字典。"""
    if now is None:
        now = datetime.now(timezone.utc)
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    date = now.strftime("%Y%m%d")
    payload_hash = hashlib.sha256(body or b"").hexdigest()

    parsed = urllib.parse.urlsplit(url)
    hostname = (parsed.hostname or "").lower()
    port = parsed.port
    default_port = (parsed.scheme == "https" and port == 443) \
        or (parsed.scheme == "http" and port == 80)
    host = hostname if (port is None or default_port) else f"{hostname}:{port}"

    sign_headers: dict[str, str] = {
        "host": host,
        "x-amz-content-sha256": payload_hash,
        "x-amz-date": amz_date,
    }
    if session_token:
        sign_headers["x-amz-security-token"] = session_token
    for name, value in headers.items():
        low = name.strip().lower()
        if low.startswith("x-amz-") or low == "content-type":
            sign_headers[low] = value

    canon = canonical_request(method, url, sign_headers, payload_hash)
    scope = f"{date}/{region}/{service}/aws4_request"
    string_to_sign = "\n".join([
        _SIGV4, amz_date, scope, hashlib.sha256(canon.encode()).hexdigest(),
    ])
    key = _signing_key(secret_key, date, region, service)
    signature = hmac.new(key, string_to_sign.encode(), hashlib.sha256).hexdigest()

    signed_names = ";".join(sorted(sign_headers))
    auth = (f"{_SIGV4} Credential={access_key}/{scope}, "
            f"SignedHeaders={signed_names}, Signature={signature}")

    out = dict(headers)
    out.update(sign_headers)
    out["Authorization"] = auth
    return out


def probe_provider(provider: StorageProvider) -> dict:
    """「测试连接」：返回 {'ok', 'status', 'message', 'children'}。

    WebDAV 会 PROPFIND Depth 1 把子目录/文件列出来，方便用户发现真实挂载路径；
    local / s3 只给出可达性结论。
    """
    if provider.kind == "local":
        try:
            provider.root.mkdir(parents=True, exist_ok=True)  # type: ignore[attr-defined]
            return {"ok": True, "status": 0, "message": "本地目录可用",
                    "children": provider.list()}
        except OSError as e:
            return {"ok": False, "status": 0, "message": str(e), "children": []}
    if provider.kind == "webdav":
        return _probe_webdav(provider)  # type: ignore[arg-type]
    if provider.kind == "s3":
        return _probe_s3(provider)  # type: ignore[arg-type]
    return {"ok": False, "status": 0, "message": f"未知类型 {provider.kind}", "children": []}


def _probe_webdav(provider: WebDAVProvider) -> dict:
    try:
        children = provider.list()
    except httpx.HTTPStatusError as e:
        hint = {401: "鉴权失败（用户名/密码不对）",
                403: "无权限访问该目录",
                404: "路径不存在",
                405: "目标不是 WebDAV 目录",
                409: "目录不存在"}.get(e.response.status_code, f"HTTP {e.response.status_code}")
        return {"ok": False, "status": e.response.status_code, "message": hint, "children": []}
    except httpx.HTTPError as e:
        return {"ok": False, "status": 0, "message": f"无法连接：{e}", "children": []}
    except Exception as e:  # noqa: BLE001 — egress 拒绝/解析失败
        return {"ok": False, "status": 0, "message": str(e), "children": []}
    return {"ok": True, "status": 207, "message": "WebDAV 目录可访问",
            "children": sorted(set(children))}


def _probe_s3(provider: S3Provider) -> dict:
    try:
        provider._request("HEAD", "")
        return {"ok": True, "status": 0, "message": "S3 目标可达", "children": []}
    except httpx.HTTPError as e:
        return {"ok": False, "status": 0, "message": f"无法连接：{e}", "children": []}


def prune_remote(provider: StorageProvider, *, prefix: str, keep_last: int) -> int:
    """删除远端旧对象，只保留按名字排序最新的 keep_last 个。

    归档文件名含固定宽度 `YYYYmmdd-HHMMSS`，字典序 = 时间序。list/delete 失败
    静默跳过（保留数裁剪失败不阻断主流程）。返回删除数量。
    """
    if keep_last <= 0:
        keep_last = 1
    try:
        names = [n for n in provider.list(prefix) if n.startswith(prefix)]
    except Exception:  # noqa: BLE001
        return 0
    names.sort()
    deleted = 0
    for old in names[:-keep_last] if len(names) > keep_last else []:
        try:
            provider.delete(old)
            deleted += 1
        except Exception:  # noqa: BLE001
            pass
    return deleted


__all__ = [
    "StorageError", "StorageProvider", "LocalProvider", "WebDAVProvider",
    "S3Provider", "build_provider", "sign_request", "canonical_request",
    "probe_provider", "prune_remote",
]