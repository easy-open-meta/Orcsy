"""Provider 注册表：类型 → 实现类，以及统一构造入口。

新增厂商只需：实现 ``base.ApprovalProvider`` 协议 → 在本模块 ``_PROVIDERS``
登记一行。``config.APPROVAL_PROVIDER_TYPES`` 与 ``PROVIDER_TYPES`` 的一致性
由 ``tests/test_approval_adapters.py`` 守护。
"""

from __future__ import annotations

from ..config import ApprovalProviderConfig
from .base import ProviderDecision, ProviderError, ProviderHandle
from .dingtalk import DingTalkProvider
from .fake import FakeProvider
from .feishu import FeishuProvider
from .github import GitHubProvider
from .gitlab import GitLabProvider

PROVIDER_TYPES = ("gitlab", "github", "feishu", "dingtalk", "fake")

_PROVIDERS: dict[str, type] = {
    "gitlab": GitLabProvider,
    "github": GitHubProvider,
    "feishu": FeishuProvider,
    "dingtalk": DingTalkProvider,
    "fake": FakeProvider,
}


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


def build_provider(name: str, config: ApprovalProviderConfig):
    """按 ``config.type`` 实例化 Provider；未知类型返回 fail-closed 占位。"""
    cls = _PROVIDERS.get(str(getattr(config, "type", "") or "").lower())
    if cls is None:
        return UnimplementedProvider(name, config, config.type)
    return cls(name, config)


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


__all__ = [
    "PROVIDER_TYPES",
    "UnimplementedProvider",
    "build_provider",
    "resolve_email",
]
