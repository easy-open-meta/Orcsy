"""本地风险评估：为合入前人工审批提供可解释的等级、原因与内容摘要。

Phase 0/1 只做确定性规则，不调用模型：
- 路径规则决定大部分等级（自更新/认证/出网/计费/迁移/依赖/发布等）；
- 少量内容信号用于识别「同一条路径下的敏感改动」（如 config.py 的 schema 迁移、
  web.py 的认证分支）；
- 项目可通过 extra_high_risk_paths 追加高风险 glob；
- 无法评估时 fail closed：按 high 处理，交给人工审批。

风险评估不改变代码，也不决定是否合入；是否设置审批门由配置策略决定。
"""

from __future__ import annotations

import fnmatch
import hashlib
from dataclasses import dataclass, field

# 高风险路径：按前缀/glob 匹配（git 路径统一使用 /）
_HIGH_PATH_RULES: tuple[tuple[str, str], ...] = (
    ("orcsy/updater.py", "自更新逻辑"),
    ("orcsy/auth.py", "认证与密码"),
    ("orcsy/masking.py", "数据脱敏"),
    ("orcsy/egress.py", "出网白名单"),
    ("orcsy/tools_net.py", "网络请求工具"),
    ("orcsy/budget.py", "预算硬闸"),
    ("orcsy/usage.py", "用量计费"),
    ("orcsy/storage.py", "存储凭据"),
    ("orcsy/approvals.py", "人工审批"),
    ("orcsy/risk.py", "风险审批"),
    ("orcsy/risk_approval.py", "风险审批"),
    ("build/**", "构建与发布"),
    ("installer.iss", "安装器"),
    (".github/workflows/**", "CI 发布流水线"),
    ("deploy/**", "部署配置"),
    ("Dockerfile", "容器镜像"),
    ("docker-compose.yml", "容器编排"),
    ("orcsy.spec", "打包配置"),
    ("uv.lock", "依赖锁文件"),
    ("package-lock.json", "依赖锁文件"),
    ("pnpm-lock.yaml", "依赖锁文件"),
    ("yarn.lock", "依赖锁文件"),
    ("poetry.lock", "依赖锁文件"),
    ("dashboard/package-lock.json", "前端依赖锁文件"),
)

# 内容信号：只有对应文件在改动列表中时才检查 diff 文本
_HIGH_CONTENT_RULES: tuple[tuple[str, tuple[str, ...], str], ...] = (
    ("orcsy/config.py", ("CONFIG_VERSION", "_CONFIG_MIGRATIONS", "_migrate_"),
     "配置 schema 迁移"),
    ("orcsy/web.py", ("auth_password", "api_keys", "AUTH_COOKIE",
                      "token_valid", "_handle_login"), "Web 认证/审批"),
)

_MEDIUM_PATH_RULES: tuple[tuple[str, str], ...] = (
    ("orcsy/**", "后端代码"),
    ("dashboard/src/**", "前端代码"),
    ("config.example.yaml", "配置示例"),
    ("config.yaml", "运行配置"),
    ("docker/**", "容器配置"),
    ("pyproject.toml", "Python 工程配置"),
    ("dashboard/package.json", "前端工程配置"),
)

_LOW_PATH_RULES: tuple[tuple[str, str], ...] = (
    ("docs/**", "文档"),
    ("README*", "README"),
    ("CHANGELOG.md", "变更日志"),
    ("tests/**", "测试"),
    ("tasks/**", "任务文件"),
    ("website/**", "官网"),
    ("promo/**", "宣传素材"),
    ("licenses/**", "许可证"),
    ("LICENSE", "许可证"),
)

_LEVEL_RANK = {"low": 0, "medium": 1, "high": 2}


@dataclass
class RiskAssessment:
    """一次合入候选的风险结论（可解释、可审计、可绑定审批）。"""

    level: str                      # low / medium / high
    reasons: list[str] = field(default_factory=list)
    files: list[str] = field(default_factory=list)
    additions: int = 0
    deletions: int = 0
    branch_sha: str = ""
    diff_hash: str = ""
    digest: str = ""

    @property
    def is_high(self) -> bool:
        return self.level == "high"

    def as_dict(self) -> dict:
        return {
            "level": self.level,
            "reasons": list(self.reasons),
            "files": list(self.files),
            "additions": int(self.additions),
            "deletions": int(self.deletions),
            "branch_sha": self.branch_sha,
            "diff_hash": self.diff_hash,
            "digest": self.digest,
        }


def _norm(path: str) -> str:
    text = str(path or "").replace("\\", "/")
    while text.startswith("./"):
        text = text[2:]
    return text.lstrip("/")


def _matches(path: str, pattern: str) -> bool:
    if pattern.endswith("/**"):
        prefix = pattern[:-3].rstrip("/")
        return path == prefix or path.startswith(prefix + "/")
    return fnmatch.fnmatchcase(path, pattern)


def _classify_file(path: str, diff: str) -> tuple[str, list[str]]:
    """返回 (单文件等级, 原因)。"""
    reasons: list[str] = []
    for pattern, label in _HIGH_PATH_RULES:
        if _matches(path, pattern):
            reasons.append(f"{label}：{path}")
    for target, needles, label in _HIGH_CONTENT_RULES:
        if path == target and any(n in diff for n in needles):
            reasons.append(f"{label}：{path}")
    if reasons:
        return "high", reasons
    for pattern, label in _MEDIUM_PATH_RULES:
        if _matches(path, pattern):
            return "medium", [f"{label}：{path}"]
    for pattern, label in _LOW_PATH_RULES:
        if _matches(path, pattern):
            return "low", [f"{label}：{path}"]
    return "medium", [f"未分类路径：{path}"]


def assess_risk(*, files: list[str], diff: str, branch_sha: str = "",
                additions: int = 0, deletions: int = 0,
                extra_high_risk_paths: list[str] | None = None) -> RiskAssessment:
    """按路径 + 内容规则评估一次改动；结果稳定、无副作用。"""
    normalized = sorted({_norm(p) for p in files if _norm(p)})
    extra = [_norm(p) for p in (extra_high_risk_paths or []) if _norm(p)]
    diff_text = str(diff or "")

    level = "low"
    reasons: list[str] = []
    for path in normalized:
        file_level, file_reasons = _classify_file(path, diff_text)
        if _LEVEL_RANK[file_level] > _LEVEL_RANK[level]:
            level = file_level
        for reason in file_reasons:
            if reason not in reasons:
                reasons.append(reason)
        for pattern in extra:
            if _matches(path, pattern):
                if _LEVEL_RANK["high"] > _LEVEL_RANK[level]:
                    level = "high"
                reason = f"项目自定义高风险路径：{path}（{pattern}）"
                if reason not in reasons:
                    reasons.append(reason)

    if not normalized:
        level = "low"
        reasons = ["无文件改动"]

    diff_hash = hashlib.sha256(diff_text.encode("utf-8", errors="replace")).hexdigest()
    digest_basis = "\n".join([
        str(branch_sha or ""),
        diff_hash,
        level,
        "|".join(reasons),
    ])
    digest = hashlib.sha256(digest_basis.encode("utf-8", errors="replace")).hexdigest()
    return RiskAssessment(
        level=level, reasons=reasons, files=normalized,
        additions=max(0, int(additions)), deletions=max(0, int(deletions)),
        branch_sha=str(branch_sha or ""), diff_hash=diff_hash, digest=digest,
    )


def failed_assessment(error: str) -> RiskAssessment:
    """评估失败：fail closed，交人工审批并保留可解释原因。"""
    reason = f"风险评估失败：{error or '未知错误'}"
    diff_hash = hashlib.sha256(reason.encode("utf-8")).hexdigest()
    digest = hashlib.sha256(f"\n{diff_hash}\nhigh\n{reason}".encode("utf-8")).hexdigest()
    return RiskAssessment(level="high", reasons=[reason], files=[],
                          diff_hash=diff_hash, digest=digest)
