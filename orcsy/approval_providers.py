"""兼容出口（保留给旧导入）：实现已拆分到 :mod:`orcsy.providers`。

新代码请使用::

    from orcsy.providers import build_provider, ProviderError

本模块仅做 re-export，包含旧测试/外部代码直接引用的下划线 helper
（``_http_json`` / ``_is_loopback_url`` 等）。
"""

from __future__ import annotations

from .providers import *  # noqa: F401,F403
from .providers import (  # noqa: F401 — 兼容旧测试/外部导入
    _headers_lower, _http_json, _is_loopback_url, _issue_body, _issue_title,
    _parse_approval_command, build_provider, decision_from_payload,
    resolve_email,
)
