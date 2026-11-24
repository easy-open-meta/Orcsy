# 看板 UI 云下发（HTML OTA）设计（复评稿）

> 状态：复评建议，**尚未实施**｜2026-11-23；本稿只更新方案，不改运行代码。
> 目标：在后台兼容的前提下独立下发可恢复的单文件看板页面；用户选择启用后才能检查与应用。
> 关联：[`plan-v1-trusted-pipeline.md`](plan-v1-trusted-pipeline.md)、`orcsy/updater.py`、
> `orcsy/web.py` 的 `_load_page`、`orcsy/config.py` 的 `UpdateConfig`、
> [`design-release-quality-gaps.md`](design-release-quality-gaps.md) 的发布分级评估。

## 1. 复评结论与范围

**保留“可选 UI 热修”方向，但重新设计发现、兼容与激活机制。推荐独立的 UI Release 查询、带后端兼容区间的清单、先暂存后人工激活、失败自动回退内置页。** 现有完整应用更新仍走原链路；不要把 UI-only Release 发布为普通 `/releases/latest` 再交给 `check_update()` 解析。若目前只需偶发修界面、且不能维护独立索引/回退演练，优先随常规安装包发布，避免引入第二条可执行 JS 发布通道。

- 当前 `web._load_page()` 只读取编译进包的 `PAGE`；附录 A 的磁盘覆盖代码已撤回，不能把它写成现有能力。
- 首期仅允许单文件、无外链的看板 HTML/JS/CSS，默认关闭；启用、下载和激活分步，当前页不热替换，确认后刷新生效。
- 外部页面与后端同源，能使用已登录浏览器的 API 权限；`sha256` 只校验取回的字节和清单相符，**不能认证发布者或隔离恶意 JS**。当前 Windows 包未签名，UI OTA 不因此获得额外发布者身份保证。

## 2. 现状与为什么要调整

- **页面**：`web._html()` 通过 `_load_page()` 返回 `orcsy.dashboard_html.PAGE`，当前无覆盖路径；`/` 已 `Cache-Control: no-store`。Vite 的 `viteSingleFile()` 与内联字体让独立 HTML 资产可行，但仍应在构建时扫描 `src`/`href`、动态导入、远程字体等外部依赖。
- **更新发现冲突**：`updater.check_update()` 只取 GitHub/Gitee 的 `/releases/latest`，随后要求有 `orcsy-update-*.zip`；仅含 `dashboard.json + dashboard.html` 的 Release 会让现有“检查应用更新”报缺 ZIP。新 UI Release 若成为最新 stable，还会遮挡完整安装包更新。必须独立查询、过滤 UI 版本与兼容范围，且不改变现有 `check_update()` 的输出/异常语义。
- **版本比较**：现有 Python 函数为 `parse_version()` / `version_newer()`，它按数字段比较、忽略非数字段，**不是完整 semver**；原稿所称 `cmpVersion` / `fmtVer` 是前端工具语义，不能作为服务端版本协议。首期应只接受明确的 `MAJOR.MINOR.PATCH` 及必要的、约定好的预发布字段，拒绝未知格式，禁止无授权降级。
- **Service Worker**：当前 `sw.js` 只缓存 `/offline.html`，导航请求 `fetch()` 网络优先、失败回退离线页；不是 cache-first 页面缓存。验收仍需覆盖已打开旧标签、离线页以及浏览器 HTTP 缓存，但无需先重写 SW 才能开展设计。
- **现有信任链**：`update.repo` 可为用户自配 GitHub/Gitee，更新 ZIP 校验优先用 API digest 或同名 `.sha256`；GitHub 私有附件可使用 Bearer，Gitee token 可能拼进查询串。UI 资产应复用仓库与授权来源，但清单/页面下载都要限大小、去敏日志、验证来源与最终跳转，不能把校验摘要称为签名。

## 3. 方案选择与发布格式

| 路径 | 优点 | 关键限制 | 结论 |
| --- | --- | --- | --- |
| 只发完整安装包 | 无第二条 JS 发布链，兼容与回退随应用版本走 | 小修复也要重建/升级应用 | 低频改动的默认选择 |
| 最新应用 Release 附 `dashboard.json + HTML` | 沿用单次查询 | UI-only Release 缺 ZIP 会破坏现有 `check_update()`，UI 版本不能脱离应用 tag | 不采用原稿设计 |
| 独立 UI Release + 专用索引 | UI 可独立迭代；完整包 `/releases/latest` 不受影响 | 需维护索引、兼容与回退策略 | **采用，作为可选能力** |
| 远程网页/配置脚本注入 | 无本地安装步骤 | 同源会话安全与离线能力退化，依赖远端始终可用 | 不采用 |

**发布约定（推荐）**：UI-only Release 为预发布，tag 如 `ui-v1.2.3`，与完整应用 `Vx.y.z` 区分；GitHub 的 `/releases/latest` 不返回预发布，但 **Gitee/自托管 Gitea 是否同样排除，须实测确认**。现有 `.github/workflows/release.yml` 按 `main` 新应用版本自动产完整包；UI 发行须用独立、手动触发的工作流，不能仅把 UI 附件塞进现有构建并误触发完整发布。两平台按 tag 查询/列 Release 的能力不同，实现前分别验证真实 API，不直接复用 `/releases/latest`。可优先用仓库中的固定 UI 索引（经已配置仓库的明确版本化资产/固定端点读取）指向 UI Release；只有当索引更新是原子操作且能在两个平台一致验证时启用自动发现。**若无法安全地维持独立索引，首期退化为用户手动输入明确 UI tag 并点击检查，不自动扫最新 Release。** UI 清单与 HTML 均作为该 UI Release 的附件，附件名固定，不允许清单指定任意 URL。若任何平台会把预发布 UI Release 当作 `/releases/latest`，必须先改造完整包查询为按 `V` tag 与 ZIP 过滤，再开放该平台 UI-only 发布。

建议清单字段：

```json
{
  "schema": 1,
  "ui_version": "1.2.3",
  "build_id": "<构建产物摘要或提交标识>",
  "asset": "dashboard.html",
  "sha256": "<64 位十六进制>",
  "min_app_version": "0.33.0",
  "max_app_version_exclusive": "0.34.0",
  "api_contract": 1
}
```

`ui_version` 仅表示 UI 发行序号；选择当前后台是否可用，须同时满足应用版本区间与明确的 API 契约版本。`api_contract` 是**提议新增**的稳定契约字段，不是当前已实现值；在它落实前只按精确、经测试的应用版本范围放行，不以“同一 minor 大概兼容”替代验证。发行时先用对应后台版本运行前端回归/可访问性烟测，再上传不可变附件及摘要，禁止覆盖同一 UI tag 的已发布字节。完整应用升级后若当前覆盖不再兼容，应自动使用内置页并提示可检查新 UI。

## 4. 客户端：下载、激活与恢复

1. `update.dashboard_ota` 默认关闭；用户启用时说明“页面脚本可访问本机看板会话”。UI 检查单独调用，不修改 `AppState.update_state` 中应用更新的 `phase`、URL 或凭据字段。
2. 从配置的 `update.repo` 查询独立 UI 索引或用户指定 tag，固定选择该 Release 的 `dashboard.json` 与 `dashboard.html`。限定清单/HTML 下载大小和读取时间；检查版本格式、字段类型、应用兼容区间、附件名与 URL 主机/跳转；重用已有更新下载的凭据处理/egress，不把 token 写入 URL 日志或状态文件。
3. 下载到 `<数据目录>/dashboard-ota/staged/`，流式校验 HTML 的 SHA256，并核对单文件资源约束；不允许任意路径、软链接或 HTML 文件自行决定读取位置。清单损坏、未提供摘要、不兼容、下载失败一律保持当前页面，记录明确错误，且不影响完整包更新。
4. 默认**人工确认应用**：在认证后的设置页展示 UI 版本、兼容后台范围、来源和当前版，用户点击后原子切换 `<数据目录>/dashboard-ota/active.json` 指针（指向受管目录内的已验证内容），写审计并提示刷新。暂存不改变 `/`；关闭自动检查也不意外撤销已激活页，禁用 OTA 时立即服务内置页。
5. 读取时优先解析 active 指针；文件缺失、摘要变化、不兼容或读失败时只服务内置 `PAGE`，标记失效原因。不要用附录 A 的“任意 `ORCSY_DASHBOARD_HTML` 或 config 同级文件优先”作为远程交付路径：那会绕过验证、兼容门禁与用户确认。若保留开发者本地覆盖，应为独立调试模式并明确权限与优先级。
6. 保留一份上次已验证的兼容 UI 作为回退候选；提供无需加载 OTA 前端的 `orcsy reset-dashboard` CLI，清除 active 指针恢复内置页。设置页可附“恢复内置界面”按钮；若 OTA 页本身坏掉，CLI 仍可救援。回退旧 UI 前重新校验当前后端兼容性；普通重置不删除下载缓存，便于诊断。

同版本重复检查幂等，不覆盖活跃文件；版本降级要人工明确选择并再次验证兼容。独立 UI 发布绝不运行后端迁移，也不能替代完整应用升级。当前页面只在刷新后切换；长时间打开的旧标签应有兼容响应或明确刷新提示，不能假定新页面能撤销旧标签已执行的请求。

## 5. 安全与信任边界

- UI 页面在 `/` 下同源运行；它能向本机 API 发请求，也可读取前端保存的会话 token。启用 OTA 的风险接近信任发布源执行代码，**SHA256/同源清单无法防御控制仓库或 Release 的人**。当前没有独立签名/固定公钥；在未建立验证根之前，不写“发布者已认证”，不默认向所有用户自动应用。
- `update.repo` 是用户可配置的发布源，所有 UI 检查仅限这一来源；Release 元数据和附件 URL 要验证并限制重定向。已有 `egress.allow_runtime_host` 对 CDN 动态放行只应覆盖被验证的附件下载，不能接受清单里任意地址为新放行源。
- 限制文件大小、下载时间、响应类型、日志和错误消息长度；token、下载查询串不出日志、不入 `state`、不随 `/api/state` 回显。审计记录来源、tag、摘要、兼容判断、激活/回退时间和操作者，不记录凭据或整页内容。
- 应用不会因为一张有合法摘要的 HTML 而授予额外权限；禁用时完全停止自动拉取，下载失败、离线或磁盘只读时保持内置/已验证页面。未来如要求抗发布源被盗，须另设独立信任锚（离线固定公钥签名及轮换流程），不因个人自签 Windows 安装包而视为已解决。

## 6. 配置、状态与兼容矩阵

| 项 | 默认/位置 | 语义 |
| --- | --- | --- |
| `update.dashboard_ota` | `false` | 用户显式启用 UI 检查；关闭后只服务内置页 |
| `update.dashboard_auto_apply` | 首期不提供 | 先以认证设置页人工确认，避免静默执行新 JS；有数据证明回退可靠后再评估 |
| `dashboard-ota/active.json` | 数据目录 | 原子指针、版本/摘要/兼容区间，无 token；文件内容存受管不可变目录 |
| `dashboard-ota/staged/` | 数据目录 | 未激活候选；容量/份数上限，不能由外部路径覆盖 |

`UpdateConfig`、配置存取、`settings_service`、`/api/state` 和前端设置须同批往返测试；既有 `repo/token/auto_check` 三态语义保持。应用版本变更时重新计算兼容性，而不是仅比较 `ui_version`。API 兼容字段未落地前，只允许 CI 验证过的应用版本精确匹配或封闭区间；后台新功能不能以 UI OTA 方式伪造能力。S3 的稳定 API 契约仍须补完整错误码验证。

## 7. 验收和实施顺序

1. **先验证发布协议**：用 GitHub/Gitee 假 Release 和真实只读 API 查询确认 UI tag/索引可发现，且 UI-only 发布不改变 `check_update()` 找完整包的结果；核验私有仓、同版本、预发布、索引损坏、回退与离线情形。若 Gitee latest 可能返回 UI-only Release，先改造完整包发现逻辑，否则该平台禁止 UI-only 发布；若索引机制不稳定，按 §3 手动 tag 兜底。
2. **先做本地覆盖引擎**：仅管理受管目录，受控 active 指针、原子写与内置页回退；对损坏/越界/不兼容做单测，保持现有 `_load_page()` 默认行为。CLI 重置优先于设置页按钮落地。
3. **再接下载与 UI**：独立于应用更新的状态与接口；清单、摘要、大小、来源、兼容检查；默认人工激活、审计与回退，旧版配置不丢字段。
4. **做真实页面验收**：安装版/源码版、浏览器和 PWA，含两个标签页、离线、刷新前后、Service Worker 控制页面、浏览器缓存与后台升级。当前 SW 仅离线页缓存，导航 network-first；无需将其错误描述为 cache-first。通过后才考虑自动应用或常规发版集成。

**量级修订**：原估算未包含独立发现协议、兼容矩阵、故障回退和双平台验证，不能再用“0.5～1 周后端 + 0.5 周 e2e”承诺交付；先用第 1 步技术验证估算实施排期。此方案仍是 P2 可选能力，不阻塞 1.0；签名暂缓，发布者认证能力不计为已完成。

---

## 附录 A：已撤回的运行时覆盖草案（仅供对照，不按此实施）

> 以下旧代码没有清单校验、兼容控制和受管激活状态；任何配置目录同级文件或环境变量均可绕过 UI OTA 的验证流程。未来若需本地调试覆盖，应独立于远程下发设计，不应复制这段作为生产实现。

`orcsy/web.py` 三处改动：

1) `_html()` 计算配置目录并透传：

```python
    def _html(self) -> None:
        base_dir = None
        try:
            if self.state.config.source_path is not None:
                base_dir = self.state.config.source_path.parent
        except Exception:  # noqa: BLE001
            base_dir = None
        body = _load_page(base_dir).encode("utf-8")
```

2) `_load_page()` 优先读 override：

```python
def _load_page(base_dir: Path | None = None) -> str:
    """看板页面：优先磁盘 override（env `ORCSY_DASHBOARD_HTML` 或 config 同级
    `dashboard.html`），都无则回退到编译进包的 PAGE。

    override 每次请求都重读——改文件后浏览器刷新即可生效，免重新构建安装包/重启。
    """
    path = _dashboard_override_path(base_dir)
    if path is not None:
        try:
            return path.read_text(encoding="utf-8")
        except OSError:
            pass
    global _PAGE_CACHE
    if _PAGE_CACHE is None:
        from .dashboard_html import PAGE

        _PAGE_CACHE = PAGE
    return _PAGE_CACHE
```

3) override 候选路径：

```python
def _dashboard_override_path(base_dir: Path | None) -> Path | None:
    env = os.environ.get("ORCSY_DASHBOARD_HTML")
    cands: list[Path] = []
    if env:
        cands.append(Path(env))
    if base_dir is not None:
        cands.append(base_dir / "dashboard.html")
    for c in cands:
        if c.is_file():
            return c
    return None
```