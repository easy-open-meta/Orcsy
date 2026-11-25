# 存储目标与备份/归档设计（Storage Targets & Backup/Archive）

> 状态：**已实施（随 0.33.0 发布）**｜2026-11-23
> 目标：统一「配置备份（S2）与项目任务归档（评估⑤）」的存储底座，首期支持
> local / WebDAV / S3 兼容，远程请求过出网白名单、凭据三态脱敏。
> 关联：[`roadmap.md`](roadmap.md) §7/§8、[`design-egress.md`](design-egress.md)、
> [`design-data-masking.md`](design-data-masking.md)。

## 1. 结论与范围

把 1.0 稳定化 S2 的「单扁平 WebDAV 配置备份」升级为**多目标存储 provider 底座**，
供配置备份与项目归档共用；同时补齐路线图评估⑤「项目任务快照归档」。

- **配置备份**：`config.yaml + sessions.json` → tar.gz（chmod 600），推送/下载经
  provider；升级前自动备份本地 + 推送 `backup.target`（失败不阻止升级）。
- **项目归档**：单项目 `tasks/*.md`（含 `done/`）+ `AGENT.md` + 项目记忆文档（`ARCHITECTURE.md` / `PROJECT_NOTES.md` / `LONG_TERM_MEMORY.md`）（可选 `runs/<项目>`）
  → tar.gz，推送到 `archive.target`。定位「快照分发 / 合规审计」，**单向**
  （回灌恢复后续再设计），避免与 Git 远端双轨。

首期目标类型：`local`（本地目录）、`webdav`（PUT/Basic Auth）、`s3`（AWS SigV4，
兼容 MinIO 及腾讯云 COS / 阿里云 OSS 等的 S3 兼容接入点）。**OneDrive / SharePoint
走 Microsoft Graph，是另一套 provider，后置（P3）**；远程 Harness / OS 级强沙箱
不在本项范围。

## 2. 架构

```
配置/归档调用方
  ├─ orcsy/backup.py   → create_backup / restore_backup / push_backup_archive / download_backup
  ├─ orcsy/archive.py  → archive_project / prune_archives / push_archive
  └─ orcsy/storage.py  → StorageProvider 接口 + build_provider 工厂
                           ├─ LocalProvider   (文件系统)
                           ├─ WebDAVProvider  (httpx + Basic Auth)
                           └─ S3Provider      (httpx + stdlib SigV4)
        └─ 所有远程请求 → egress.check(url, purpose="backup")
```

- `StorageProvider` 最小接口：`put / get / list / delete`。首期 `list` 未实现
  （WebDAV PROPFIND / S3 ListObjects 需解析 XML），远端保留数裁剪由备份侧跳过，
  本地 `prune_backups` / `prune_archives` 仍生效。
- **S3 不再引入第三方依赖**：SigV4 用 stdlib `hmac` + `hashlib`（canonical request
  → string-to-sign → HMAC 签名链）。寻址：endpoint 给定时 path-style；空则
  virtual-host `{bucket}.s3.{region}.amazonaws.com`；带点桶名回退 path-style（TLS
  证书）。
- **egress**：`_collect_urls`（`egress.py`）新增派生 WebDAV `url` 主机、S3
  `endpoint`/bucket 主机，首次备份即被放行；未覆盖的自定义主机仍可 `extra_hosts`
  显式放行。

## 3. 配置 schema（CONFIG_VERSION 1→2）

```yaml
storage_targets:            # 目标注册表（local 为内置隐含，无需声明）
  my-webdav:
    type: webdav
    url: https://dav.example.com/orcsy/
    username: ""            # 留空 = 匿名
    password: ""
  my-s3:
    type: s3
    endpoint: https://s3.example.com   # 空 = AWS
    bucket: orcsy-backups
    region: us-east-1
    access_key: ""
    secret_key: ""
    # session_token: ""                # 可选
backup:
  enabled: false            # 升级前自动备份是否也推送远端（本地始终留一份）
  target: my-s3             # storage_targets 目标名；local = 内置
  keep_last: 10
archive:
  enabled: false
  target: local
  include_runs: false       # 是否附带 runs/<项目>
  keep_last: 10
```

数据类：`StorageTargetConfig`（config.py）、`BackupConfig`（重塑，`target` 取代
`target_type/url/username/password/dir`）、`ArchiveConfig`（新增）。`AppConfig`
新增 `storage_targets` / `archive` 字段。

**迁移 1→2**（`_CONFIG_MIGRATIONS[1]` = `_migrate_v1_backup`，幂等）：旧扁平
`backup.target_type == "webdav"` 且含 `url` → 合成 `storage_targets["webdav"]` +
`backup.target="webdav"`；`target_type == "local"` 且 `dir` 非默认 → 合成
`storage_targets["local"]`。旧 `backup` 扁平字段在迁移后移除。

## 4. 安全模型

- **凭据三态**：看板导出用 `mask_secret`（`web.py`），storage_targets 的
  webdav `password` / s3 `access_key` / `secret_key` / `session_token` 与
  `llm.api_key` 同规则——键缺失=保持、空串=清除、非空=换新。磁盘仍明文（与现有
  `llm.api_key` 一致，不做差异化改动）。
- **远程请求过 egress**：WebDAV/S3 provider 统一调 `egress.check(url, purpose="backup")`，
  修复历史 `push_backup_webdav` 直连绕过。
- **目标引用校验**：`load_config` / `save_settings` 均 `resolve_storage_target` 校验
  `backup.target` / `archive.target` 可解析（`local` 内置），坏引用拒启动/拒保存。
- 边界：外部存储进程**不受** Orcsy 内置文件工具沙箱 / 命令白名单 / 审批 / 出网
  白名单以外的 OS 级隔离约束；本项目标是「备份出口」，不承诺强沙箱。

## 5. 入口

- CLI：`backup-config [--target] [--dir]`、`restore-config [--target] <归档>`、
  `archive-project <项目> [--target] [--include-runs]`。
- API：`POST /api/backup`（手动配置备份）、`POST /api/archive`（手动项目归档，body
  `{project}`）；`GET /api/state` 暴露 `storage_targets` / `backup_cfg` / `archive_cfg`
  （密钥只出掩码）。
- 看板：设置 → 数据与合规 → 备份与归档（存储目标增删改 + 备份/归档目标与保留份数）。

## 6. 验收与测试

- `tests/test_storage.py`：SigV4 `canonical_request` 对拍 AWS 官方样例哈希
  `7344ae5b…`；Local 往返 + 路径穿越拒绝；S3 寻址；工厂分派。
- `tests/test_archive.py`：归档内容（含 `done/`、排除 `.worktree-arranger`）、裁剪。
- `tests/test_backup.py`：备份-恢复往返、裁剪、路径穿越拒绝、WebDAV 推送 mock、
  `storage_targets/backup/archive` 读写往返。
- `tests/test_config_roundtrip.py`：`check_v1_backup_migration`（扁平 webdav →
  storage_targets，幂等）；样本覆盖 `storage_targets`/`archive` 顶层键。
- 手工：`backup-config --target my-s3` 指向 MinIO；`archive-project demo --target my-webdav`；
  旧 `backup.target_type=webdav` 样本 load→迁移→save 不丢凭据。