"""审批 Provider 统一出口。

新代码请使用::

    from orcsy.providers import build_provider, ProviderError

各厂商实现位于同级模块（``gitlab`` / ``github`` / ``feishu`` / ``dingtalk`` /
``fake``），公共契约与回调签名在 :mod:`orcsy.providers.base`。旧路径
``orcsy.approval_providers`` 保留为兼容 shim。
"""

from __future__ import annotations

from .base import (CALLBACK_SIGNATURE_HEADER, CALLBACK_TIMESTAMP_HEADER,  # noqa: F401
                   CALLBACK_WINDOW_SECONDS, ApprovalProvider, ApproverDecision,
                   ProviderDecision, ProviderError, ProviderHandle,
                   _headers_lower, _http_json, _is_loopback_url,
                   decision_from_payload, sign_callback,
                   verify_callback_signature)
from .dingtalk import DingTalkProvider
from .fake import FakeProvider
from .feishu import FeishuProvider
from .github import GitHubProvider
from .gitlab import GitLabProvider
from .issue import _issue_body, _issue_title, _parse_approval_command  # noqa: F401
from .registry import (PROVIDER_TYPES, UnimplementedProvider, build_provider,
                       resolve_email)

__all__ = [
    "CALLBACK_SIGNATURE_HEADER",
    "CALLBACK_TIMESTAMP_HEADER",
    "CALLBACK_WINDOW_SECONDS",
    "PROVIDER_TYPES",
    "ApprovalProvider",
    "ApproverDecision",
    "DingTalkProvider",
    "FakeProvider",
    "FeishuProvider",
    "GitHubProvider",
    "GitLabProvider",
    "ProviderDecision",
    "ProviderError",
    "ProviderHandle",
    "UnimplementedProvider",
    "build_provider",
    "decision_from_payload",
    "resolve_email",
    "sign_callback",
    "verify_callback_signature",
]
