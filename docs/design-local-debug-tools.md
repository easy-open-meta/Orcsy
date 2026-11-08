# 本地调试工具（浏览器 / 本地 API / 开发服务）开发方案

> 状态：**v3 终审定案**（未实施）
> 需求原文：**添加调用本地浏览器操作工具，方便 Agent 进行测试任务时，对 web、api 项目进行本地调试操作。**
>
> **修订记录：**
> - v1：初稿（仅 Chromium / CDP）。
> - v2（评审一）：①默认关闭——定案；②视觉输入（原 M5）本期不做，仅保留接口；③`allowed_hosts` 支持项目级追加——定案（只增不减 + 硬禁止清单，见 §3.4/§3.5）；④曾考虑双协议支持 Firefox。
> - v3（评审二定案）：**Firefox 移出范围**——用户确认目前本地不具备 Firefox 测试条件，**本期仅聚焦 Chrome / Chromium 系（系统 Google Chrome / Microsoft Edge，两者同属 Chromium 内核，完全共用成熟的 CDP 协议）**。架构全面精简回归自研 CDP 单协议直连，彻底消除跨协议差异与环境缺失风险；总工作量收敛至 14 人日。

---

## 1. 需求解读

「调用本地浏览器」只是手段。Agent 要在测试任务里真正调试 web / api 项目，需要一条完整闭环：

```
启动被测服务 → 等待就绪 → 操作（浏览器点页面 / 发 HTTP 请求）→ 观察结果（页面状态、控制台、网络、响应）→ 判断 → 收尾停服务
```

现有工具只覆盖「写代码 + 跑一次性命令」，这条闭环有三处缺口：

| # | 缺口 | 现状（代码依据） | 需要的能力 |
|---|------|------------------|------------|
| G1 | **没有浏览器** | 无任何浏览器相关代码 | 打开页面、读页面、点击输入、截图、读控制台与网络 |
| G2 | **访问不了本机服务** | `web_fetch` 的 SSRF 防护拒绝回环/私网地址（`tools.py:729 _assert_public_url`），是硬边界，且只发 GET、只回文本 | 受控放行回环地址的 HTTP 请求（任意方法、带 body/header、会话 cookie） |
| G3 | **起不了常驻服务** | `run_command` 阻塞等待、超时或结束即杀进程树（`tools.py:580`、`_wait_with_stop`）；dev server 永不退出，只能超时被杀 | 后台启动、就绪探测、读日志、停止，且任务结束必清理 |

因此本期交付三组工具（而不只是浏览器）：

| 工具组 | 工具数量 | 用途 | 对应缺口 |
|--------|:--------:|------|----------|
| `browser` | 10 个 `browser_*` | 驱动本机 Chrome / Edge（Chromium 系）调试 web 前端 | G1 |
| `http_request` | 1 个 | 调试本地/内网 API，行为受主机白名单约束 | G2 |
| `dev_server` | 3 个 `dev_server_*` | 后台起停被测服务 | G3 |

### 1.1 范围与边界

- **做**：本机 **Chromium 系浏览器（Google Chrome / Microsoft Edge）**；被测服务默认在**本机**（回环地址），可按配置追加局域网测试主机（§3.4）；Agent 在 worktree 里改完代码后验证。
- **不做**：Firefox（本地无测试环境，定案移出）、Safari/WebKit、远程浏览器农场、移动端模拟、录屏、公网站点的自动化（`web_fetch` 已覆盖读公网文档）、**截图视觉输入**（本期推迟，见 §3.6）。
- **默认关闭**（opt-in，评审一定案）：新工具会起进程、碰本机网络，存量用户升级后行为必须与旧版完全一致，需显式开启（见 §3.4）。

---

## 2. 现状梳理（改动锚点）

| 区域 | 位置 | 与本需求的关系 |
|------|------|----------------|
| 工具注册表 | `tools.py:1015 TOOLS`、`:1295 TOGGLEABLE_TOOLS`、`:1348 dispatch_tool` | 新工具注册点；`dispatch_tool` 已串好「开关拦截 → 审批门 → 执行」 |
| 工具上下文 | `tools.py:294 ToolContext` | 需新增会话槽（`session`）承载浏览器与后台服务句柄 |
| 子进程治理 | `_CHILD_PROCS`、`_kill_process_tree`（`:541-577`）、`kill_running_children`（退出清理，`__main__.py:580`） | 后台服务、浏览器进程直接复用，不另造一套 |
| 命令白名单 | `run_command` 内联校验（`:586-589`） | `dev_server_start` 必须过同一道闸门，需抽成公共函数 |
| 工具开关 | `ToolsConfig`/`resolve_tools`（`config.py:148/434`）；`executor.py:153 _enabled_tools`；`web.py:518 tools_catalog` | 全局+项目两级开关**直接复用**，项目级「只对某个 web 项目开启」零额外开发 |
| 访问权限审批 | `approvals.py:37 MUTATING_TOOLS`、`:50 RISKY_TOOLS`、`requires_approval(mode, tool)` | 新工具必须归类，否则在 `ask_on_change/autonomous` 下形同绕过审批 |
| 任务执行 | `executor.py:263 _agent_loop`（构造 `ToolContext`，`:293`） | 会话的创建点与关闭点（finally） |
| 强制停止 | `task_control.py` + `stop_event` | 浏览器等待、服务就绪等待都要按切片轮询 stop_event |
| 工具循环 | `llm.py:371 run_task_loop`，重复调用检测 `_REPEAT_CALL_WARN` | 轮询类调用（snapshot/wait/logs）会被误判为「打转」，需豁免 |
| 工具结果 | `llm.py:366 _tool_result` 只接受 `str` | 决定了截图无法直接回给模型（见 §3.6） |
| 项目探测 | `probe.py`（已能探出 `npm run dev`、`pnpm start` 等 run 命令） | 可作为提示词里「怎么起服务」的默认来源 |
| 依赖 | `pyproject.toml`：仅 httpx / pillow / pystray / PyYAML | 项目刻意轻依赖，影响技术选型（§3.1） |
| 分发 | PyInstaller 双 exe（`orcsy.spec`）、Docker slim 镜像、Linux systemd | 浏览器不能被打进包；Docker 默认无浏览器 |

---

## 3. 总体设计

### 3.1 技术选型：自研 CDP 驱动，直连系统 Chromium（Chrome / Edge）

为什么选择「自研轻量 CDP 直连」方案：

1. **协议统一且极度成熟**：Chrome 与 Edge 均为 Chromium 内核，对 Chrome DevTools Protocol (CDP) 的支持完全一致，零跨协议适配包袱；
2. **零外部 driver 依赖**：Chrome / Edge 自带 `--remote-debugging-port`，启动后直出 WebSocket 调试端点，无需下载或维护任何 `chromedriver` 二进制；
3. **开箱即用保障**：Windows 10/11 默认必带 Microsoft Edge（本机实测存在 156.0.4314.50），开发者电脑通常也装有 Google Chrome；
4. **轻量与可控**：仅需引入纯 Python 依赖 `websockets>=12`，体积增加可忽略；启动参数、随机端口、临时 profile、网络拦截及超时信号可做到 100% 宿主内代码可控。

| 方案 | 优点 | 缺点 | 结论 |
|------|------|------|------|
| **A. 自研 CDP 驱动（推荐）**：`subprocess` 拉起系统 Chrome/Edge（`--remote-debugging-port=0` + 独立临时 profile），用 `websockets` 同步客户端直连 | 新增 1 个纯 Python 库（`websockets`）；零外部 driver、零下载；生命周期与停止信号完全受控；Windows 自带 Edge 保底 | 需封装常用 DOM 交互与等待脚本（约 750 行） | **采用** |
| B. Playwright（`channel="chrome"/"msedge"`） | API 现成 | 打包自带 node driver（单平台 35MB+），大幅膨胀发布包体积；PyInstaller 打包路径易出 bug | 否决 |
| C. Selenium | 成熟 | 依赖 `chromedriver` 且版本必须与浏览器严格匹配，易引发自动化运维灾难 | 否决 |
| D. 接 chrome-devtools-mcp 等 | 现成工具 | 引入外部 Node 运行时与额外子进程 IPC 通信开销 | 否决 |

> 新增依赖：`websockets>=12`（纯 Python，`websockets.sync.client`）。需同步 `pyproject.toml`、`uv.lock` 及 PyInstaller spec 配置。

### 3.2 总体架构

```
TaskExecutor._agent_loop()                                   executor.py:263
  ├─ [新增] session = DebugSession(cfg, task, stop_event)    ← 每任务一个，惰性初始化
  ├─ ToolContext(..., session=session)
  ├─ run_task_loop → dispatch_tool → tools_debug.* 
  │       ├─ browser_*      → session.browser()  → CdpClient → Chrome / Edge 子进程
  │       ├─ http_request   → session.http()     → httpx.Client（带 cookie jar、主机白名单）
  │       └─ dev_server_*   → session.services() → 后台进程 + 日志环形缓冲
  └─ finally: session.close()   ← 杀浏览器、停全部服务、删临时 profile（正常/异常/强停三路径都走）
```

模块划分（保持单一职责，避免继续膨胀 1377 行的 `tools.py`）：

| 模块文件 | 职责 | 估算行数 |
|----------|------|----------|
| `orcsy/debug_session.py` | `DebugSession`：持有浏览器句柄、服务表、HTTP 客户端，统一关闭；任务级工件目录管理 | ~150 |
| `orcsy/browser/discover.py` | Chrome / Edge 可执行文件自动发现、拉起、临时 profile 管理、`DevToolsActivePort` 端口发现 | ~180 |
| `orcsy/browser/cdp.py` | CDP 同步客户端：请求/响应配对、后台事件分发线程、超时控制、stop_event 响应、顶层导航白名单拦截 | ~320 |
| `orcsy/browser/page_script.py` | 页面注入脚本：DOM 遍历与紧凑文本树生成（打 `data-orcsy-ref`）、包围盒计算与交互定位 | ~150 |
| `orcsy/browser/hosts.py` | 主机白名单合并规则、格式校验、DNS 解析后 IP 过滤与硬禁区拦截（浏览器与 http_request 共用） | ~100 |
| `orcsy/devserver.py` | 后台服务管理：端口自动分配（`{PORT}` 替换）、就绪探针、日志环形缓冲、进程树终止 | ~250 |
| `orcsy/tools_debug.py` | 14 个工具函数实现与 JSON Schema 定义，启动时挂入 `TOOLS` | ~450 |

### 3.3 工具清单与行为

#### 3.3.1 browser 组（10 个工具）

页面交互采用**紧凑文本快照 + 元素 ref 引用**机制：
`browser_snapshot` 通过注入轻量 JS 脚本遍历当前视口可见 DOM，为可交互元素生成唯一标识 `data-orcsy-ref`，返回类似 `[e12] button "提交"`、`[e13] textbox "密码" value=""` 的结构化文本树。后续操作均以 `ref` 寻址，稳定高效且对纯文本大模型友好。

| 工具 | 参数 | 说明 |
|------|------|------|
| `browser_navigate` | `url`, `wait_until?`, `browser?` | 首次调用时按需拉起 Chrome/Edge；仅放行生效白名单主机（§3.5） |
| `browser_snapshot` | `ref?`, `max_chars?` | 获取页面标题与元素快照树；支持局部子树查询；默认上限 6000 字符 |
| `browser_click` | `ref` 或 `selector`, `double?` | 元素滚动入视野 → 计算坐标中心 → CDP `Input.dispatchMouseEvent` 点击；点击后等待网络静默或页面响应（≤3s） |
| `browser_type` | `ref`, `text`, `clear?`, `submit?` | 元素聚焦 → CDP `Input.insertText` 输入；支持回车或特殊键组合 |
| `browser_wait` | `text?`, `selector?`, `url_contains?`, `timeout_seconds?` | 轮询等待条件达成；按 0.2s 切片响应看板强停信号；超时返回当前页面快照摘要 |
| `browser_screenshot` | `ref?`, `full_page?` | CDP `Page.captureScreenshot`，PNG 落盘到工件目录，**返回路径+尺寸，不内联图片**（§3.6） |
| `browser_console` | `level?`, `clear?` | 获取 CDP `Runtime.consoleAPICalled` / `Log.entryAdded` 收集的控制台日志与未捕获异常（环形缓冲 200 条） |
| `browser_network` | `failed_only?`, `url_contains?`, `reqid?` | 获取 CDP `Network.*` 捕获的 HTTP 请求记录；支持根据 `reqid` 取回响应体前 4KB（`Network.getResponseBody`） |
| `browser_eval` | `expression` | CDP `Runtime.evaluate` 执行 JavaScript 表达式并序列化返回结果；用于读取 `localStorage`、前端状态等 |
| `browser_close` | — | 主动关闭浏览器实例；任务结束时也会被强制自动关闭 |

- **多标签页控制**：首版固定为单标签页会话，`window.open` 与 `target="_blank"` 自动重定向在当前标签页打开，防止会话分支漂移。

#### 3.3.2 http_request（1 个工具）

```
http_request(method, url, headers?, json?|body?, timeout_seconds?, follow_redirects?)
→ "[HTTP 201 · application/json · 38ms]\n<美化格式的 JSON / 截断文本>"
```

- **与 `web_fetch` 边界互补、严格区分**：
  - `web_fetch`：专用于公网资料爬取，强制拒绝本地/内网私网地址；
  - `http_request`：专用于本地服务/开发环境接口调试，**仅放行白名单内的主机**。
- **重定向安全防护**：关闭客户端自动重定向，每一跳重定向均由系统重新校验目标是否在白名单内，防止跳转渗透到受保护网络。
- **状态维持**：任务会话内共享 `httpx.Client` cookie jar，可顺畅测试「登录鉴权 → 携带 Cookie 调接口」业务流程，任务结束后自动注销。
- **方法支持**：放行 `GET / POST / PUT / PATCH / DELETE / HEAD / OPTIONS`。

#### 3.3.3 dev_server 组（3 个工具）

| 工具 | 参数 | 说明 |
|------|------|------|
| `dev_server_start` | `name`, `command[]`, `cwd?`, `ready_url?`, `ready_timeout_seconds?`, `env?` | 后台启动服务；命令及参数支持 `{PORT}` 占位符自动注入分配的空闲端口；支持轮询 `ready_url` 直至服务健康就绪 |
| `dev_server_logs` | `name`, `tail?`, `since_last?` | 读取 stdout/stderr 环形缓冲区日志（默认尾部 80 行）；报告进程存活状态与退出码 |
| `dev_server_stop` | `name?` | 终止指定或全部已启动的后台服务进程树 |

- **端口自动分配**：避免与开发者主机本地已运行服务或其他并行任务冲突。启动前动态探测分配系统空闲端口并替换 `{PORT}`，同时通过环境变量 `PORT` 注入子进程。
- **工作目录禁锢**：`cwd` 默认限定在当前任务 worktree（非 git 项目为项目根），且严禁越界访问项目根目录外路径。
- **命令安全审查**：启动命令首参数必须符合项目生效的 `allowed_commands` 白名单。
- **服务上限**：单任务最多允许并发拉起 3 个后台服务（`max_services`）。

### 3.4 配置与开关

#### 工具开关：复用现有 `tools` 机制 + 「默认关闭（定案）」

新增 3 个工具分组开关，**默认全部关闭**，可通过全局或项目级配置按需激活：

```yaml
tools:
  browser: true        # browser_* 10 个工具
  dev_server: true     # dev_server_* 3 个工具
  http_request: true   # 本地 API 调试工具

projects:
  - name: my-web
    tools:             # 仅针对特定的 web/api 项目开启
      browser: true
      dev_server: true
      http_request: true
```

- **工具分组（`TOOL_GROUPS`）**：在 `tools.py` 中定义 `browser` 与 `dev_server` 分组。看板与配置解析支持按组一键开关，避免出现工具残缺集。
- **默认关闭机制（`DEFAULT_OFF_TOOLS`）**：新工具默认归入非活跃集合，未配置时不影响历史存量任务，不会额外占用提示词 token。

#### 全局调试设置：新增 `local_debug` 节

```yaml
local_debug:
  browser: auto                   # auto | chrome | edge（默认 auto：优先 Chrome，若无则回退 Edge）
  browser_executable: ""          # 手动指定浏览器可执行文件绝对路径（留空则自动探测）
  headless: true                  # 是否以无头模式运行（false 则在桌面弹出窗口供围观）
  allowed_hosts: [localhost, 127.0.0.1, "::1"]   # 全局主机白名单
  navigation_timeout_seconds: 30  # 页面导航及等待超时（秒）
  service_ready_timeout_seconds: 60  # 开发服务就绪探针超时（秒）
  max_services: 3                 # 单任务最大后台服务数
  artifacts_retention_days: 7     # 截图工件保留天数

projects:
  - name: my-web
    debug_allowed_hosts:          # 项目级追加白名单（评审一定案：只增不减）
      - 192.168.1.50
      - dev.local
```

- **主机白名单合并与防御规则**：
  1. **只增不减**：项目生效集合为 `global.allowed_hosts ∪ project.debug_allowed_hosts`，项目配置无权移除全局安全白名单；
  2. **格式强校验**：仅允许裸主机名或 IP 字面量，拒绝通配符 `*`、CIDR 网段、带端口或协议的字符串；
  3. **硬禁区不可突破**：云厂商元数据服务（`169.254.169.254` 等链路本地地址 `169.254.0.0/16`、`fe80::/10`）、广播地址、未指定地址（`0.0.0.0`）**即使人为写进白名单也会在解析后被拦截**；
  4. **DNS 重绑定防御**：解析到私网的目标如果未被显式列出，一律直接拒绝连接。

### 3.5 安全设计

| 威胁场景 | 防御落地措施 |
|----------|--------------|
| 污染开发者日常浏览器数据与登录态 | **每次运行分配独立的全新临时 profile 目录**（`--user-data-dir=<临时目录>`），任务完成或异常中断后立即清理销毁，绝不访问宿主用户 Profile |
| 浏览器跳转恶意外部网址或本地文件 | 顶层导航在网络请求前进行 CDP 拦截（`Fetch.requestPaused`）：协议严格限定为 `http/https`；**严禁访问 `file:`、`chrome:`、`about:`（`about:blank` 除外）、`devtools:`、`view-source:` 等内部协议**；目标主机必须符合生效白名单 |
| Agent 探测未授权内网基础设施 | 默认仅限回环地址；项目级白名单变更仅能由开发者在配置中显式指定；DNS 解析后进行逐 IP 过滤，云元数据等敏感网段强制拉黑 |
| 调试端口遭遇本地劫持 | 启动时强制绑定 `127.0.0.1` 配合随机动态端口（`--remote-debugging-port=0`），通过读取 Profile 目录内的 `DevToolsActivePort` 取得真实连接凭据 |
| 后台服务与浏览器进程残留孤儿化 | **三层防护机制**：① `DebugSession.close()`（`TaskExecutor` 的 `finally` 块强制回收）；② 进程登记入 `_CHILD_PROCS`，由程序退出钩子 `kill_running_children` 清理；③ 启动阶段扫描清理残留历史临时目录 |
| 执行命令逃逸 | `dev_server_start` 严格受制于项目命令白名单 `allowed_commands` 与工作目录项目根禁锢 |
| 访问权限控制被穿透 | 严格遵守系统访问权限审批体系（见下表） |
| 敏感凭证泄露 | `http_request` 的敏感请求头与 Body 不记入持久日志；任务进度广播自动将密码等敏感字段打码脱敏 |
| 截图工件污染 Git 仓库 | 截图等工件统一下发至 Orcsy 配置运行目录 `artifacts/<项目>/<任务ID>/`，绝不写入项目 worktree |

**访问权限审批归类（`approvals.py`）**：

| 工具名称 | `ask_on_change` | `autonomous` | 判定理由 |
|----------|:---:|:---:|----------|
| `browser_snapshot / screenshot / console / network / wait / close` | 否 | 否 | 只读观察操作 |
| `browser_navigate` | 否 | 否 | 目标受白名单强约束 |
| `browser_click / type / eval` | **需审批** | 否 | 会改变被测系统的运行时数据状态 |
| `http_request` | 非 GET/HEAD **需审批** | 否 | 写操作（POST/PUT/DELETE）可能修改接口数据 |
| `dev_server_start` | **需审批** | **需审批** | 后台起进程，等同于 `run_command`，归入 `RISKY_TOOLS` |
| `dev_server_logs / stop` | 否 | 否 | 读取日志 / 资源清理 |

### 3.6 截图与视觉：本期不做视觉输入（评审一定案）

- **观察基石**：以 `browser_snapshot` 文本树作为 Agent 的核心感知输入，完整覆盖元素标签、文案、表单值与可点击状态；
- **截图定位**：`browser_screenshot` 仅负责生成并保存 PNG 图像至工件存储，供开发者在看板前端查验实测留证。工具返回文案明确提示：「*截图已保存，当前模型仅支持文本分析，请结合 browser_snapshot 了解页面*」；
- **向前兼容**：`BrowserBackend.screenshot()` 预留字节输出接口，未来可无缝对接支持图像输入的多模态模型（`llm.vision`）。

### 3.7 与现有机制的联动

1. **循环打转检测豁免**：在 `llm.py` 中将 `browser_snapshot / browser_wait / browser_console / browser_network / dev_server_logs` 加入重复调用容忍白名单，避免轮询等待时触发 `_REPEAT_CALL_WARN` 警告；
2. **系统提示词动态挂载**：当项目开启调试工具时，`prompts.py` 动态注入「本地调试指引」节，指导 Agent 按规范顺序工作：`dev_server_start` 获取端口 → 导航页面 → `snapshot` 观察 → 交互测试 → 排查日志 → `dev_server_stop` 清理；
3. **AGENT.md 规范扩展**：在项目开发指南模板中追加「本地调试与测试服务」小节，指导项目注明默认启动命令、健康检查路由及模拟测试账号。

### 3.8 浏览器发现逻辑

`browser.discover.find_browser(cfg)` 按顺序检索：
1. 若配置了 `browser_executable`，以配置为准；
2. 若 `browser: chrome` 或 `auto`：
   - **Windows**：注册表 `HKLM/HKCU\Software\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe` 及常见 Program Files 安装路径；
   - **macOS**：`/Applications/Google Chrome.app/Contents/MacOS/Google Chrome`；
   - **Linux**：`which google-chrome chromium chromium-browser`。
3. 若 `browser: edge` 或 `auto` 且未找到 Chrome：
   - **Windows**：`%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe`；
   - **macOS**：`/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge`；
   - **Linux**：`which microsoft-edge`。

- 找不到浏览器时，工具向 Agent 返回清晰可行动的引导错误信息，告知缺少相应浏览器环境，建议通过 `finish(action=blocked)` 说明原因，避免盲目重试。

---

## 4. 详细改动清单

### 4.1 后端 `orcsy/`

| 文件 | 变更说明 |
|------|----------|
| `tools.py` | 抽取 `_check_command_allowed`；`ToolContext` 挂载 `session` 与 `debug_cfg`；扩展 `TOOL_GROUPS` 与 `DEFAULT_OFF_TOOLS`；引入 `tools_debug` 延迟注册 |
| `tools_debug.py`（新） | 14 个本地调试工具的定义、参数校验与调度实现 |
| `debug_session.py`（新） | `DebugSession` 容器类：惰性管理浏览器 CDP 客户端、服务子进程表与 HTTP 客户端，保障幂等清理 |
| `browser/discover.py`（新） | Chrome / Edge 探测、拉起、独立临时 Profile 管理与 `DevToolsActivePort` 解析 |
| `browser/cdp.py`（新） | 纯 Python CDP 同步客户端：WebSocket 握手、请求/响应配对、事件派发线程、前置网络拦截（`Fetch` 域） |
| `browser/page_script.py`（新） | DOM 遍历快照树构建与交互定位脚本 |
| `browser/hosts.py`（新） | 白名单合并、格式校验与 DNS 解析后 IP 过滤引擎 |
| `devserver.py`（新） | 后台开发服务管理器：端口动态探测与注入、日志环形缓冲（2000 行）、就绪探测探针 |
| `config.py` | 新增 `LocalDebugConfig`；`ProjectConfig` 增加 `debug_allowed_hosts`；更新配置解析、校验与合并规则 |
| `executor.py` | `TaskExecutor` 生命周期中注入与管理 `DebugSession`，确保任何退出分支均触发资源清理 |
| `approvals.py` | 扩展 `requires_approval` 支持按 HTTP 方法区分审批；将 `dev_server_start` 纳入 `RISKY_TOOLS` |
| `llm.py` | 为调试轮询类工具补充防打转警告豁免名单 |
| `prompts.py` | 动态拼接本地调试工作规范；提示词工具清单同步 |
| `web.py` | `tools_catalog` 分组化展示；项目保存接口同步 `debug_allowed_hosts`；提供 `POST /api/debug/browser-test` 接口；提供 `GET /api/artifact` 静态工件查验接口 |
| `__main__.py` | 模板配置项同步；进程启动与退出时增加遗留调试临时目录清理逻辑 |

### 4.2 看板 `dashboard/src/`

| 文件 | 变更说明 |
|------|----------|
| `views/SettingsView.vue` | 工具开关增加「本地调试工具」卡片；提供浏览器模式（auto/chrome/edge）、全局白名单配置及「测试浏览器」连通性按钮 |
| `components/Dialogs.vue` | 项目设置弹窗中提供工具覆盖开关，并支持项目级追加白名单标签输入（只增不减提示与前端格式校验） |
| `views/DashboardView.vue` | 确保 `toggleProject` 完整回传 `debug_allowed_hosts`，防止配置被冲刷 |
| `api.ts` / `store.ts` | 声明调试配置与项目追加白名单字段类型；完善对话框数据透传 |

### 4.3 打包与部署

- `pyproject.toml` / `uv.lock`：新增 `websockets>=12`；更新 `orcsy.spec` 验证二进制打包；
- `Dockerfile`：增加 `ARG WITH_CHROMIUM=0`，支持按需安装 `chromium` 软件包；
- 文档更新：`README(.zh-CN).md` 与 `docs/用户指南.md` 增加调试工具使用指南。

---

## 5. 测试计划（沿用 `tests/` 纯脚本风格）

| 测试脚本 | 覆盖核心逻辑 | 依赖本机浏览器 |
|----------|--------------|:--------------:|
| `test_http_request_guard.py` | 白名单放行与拒绝拦截；回环地址校验；跨跳重定向阻断；Cookie Jar 保持；非法格式防御 | 否 |
| `test_devserver.py` | `{PORT}` 占位替换；就绪状态探测；日志环形缓冲；命令白名单防御；进程树安全清理 | 否 |
| `test_debug_session.py` | `DebugSession` 资源释放幂等性；异常中断与强停信号下的进程清理；临时目录销毁 | 否 |
| `test_local_debug_config.py` | 全局与项目级白名单合并规则（只增不减）；元数据等硬禁区防御；配置保存与加载 Roundtrip | 否 |
| `test_cdp_protocol.py` | 搭建本地 Mock WebSocket 服务模拟 CDP，验证请求配对、事件派发、超时与断线容错 | 否 |
| `test_tools_toggle.py`（更新） | 工具分组映射；`DEFAULT_OFF_TOOLS` 默认禁用行为验证；项目级覆盖 | 否 |
| `test_approvals.py`（更新） | 14 个新工具的访问权限模式审批分类与判定断言 | 否 |
| `e2e_browser.py` | 启动本地测试 Web 服务，通过真实 Chrome/Edge 执行导航、快照读取、点击输入、Console 监听与截图断言（若无浏览器则自动跳过） | **是** |
| `e2e_debug_task.py` | 脚本化 `FakeLLMClient` 完整演练「起服务 → 浏览器测试 → API 验证 → 停服务 → 任务交付」完整链路 | 否 |
| 既有回归测试 | `e2e_fake_llm.py`、`test_config_example.py` 等，确保默认关闭态下历史功能完全零回退 | 否 |

---

## 6. 关键决策与备选方案

| # | 决策项 | 最终选型 | 备选方案 | 采纳理由 |
|---|--------|----------|----------|----------|
| D1 | 浏览器驱动技术 | **自研 CDP 客户端直连系统 Chrome/Edge** | Playwright / Selenium / 双协议 | 统一于 Chromium 内核，零外部 driver 依赖，打包体积最小；Firefox 移出范围彻底消除了 BiDi 双协议的不确定性 |
| D2 | 工具粒度 | **14 个高内聚原子工具 + 3 个分组开关** | 单一万能工具 | 明确模型调用语义，降低幻觉率；分组配置简化用户操作 |
| D3 | 默认开关状态 | **默认关闭，需显式开启** | 默认开启 | 杜绝不必要的 token 消耗与未预期网络端口暴露，存量项目行为完全一致 |
| D4 | 页面感知手段 | **紧凑 DOM 文本快照 + 元素 ref 标记** | 多模态截图直接输入 | 兼容纯文本大模型（DeepSeek 等）；截图仅作留证，节省上下文窗口 |
| D5 | API 调试工具 | **独立新增 `http_request`** | 放宽现存 `web_fetch` | 坚守 `web_fetch` 的公网防 SSRF 安全红线，互不污染职责边界 |
| D6 | 服务生命周期 | **专属 `dev_server_*` 机制** | 依赖 `run_command` 加 `&` | 规避 Windows 控制台句柄悬挂死锁；原生实现端口冲突自愈与安全回收 |
| D7 | 浏览器隔离 | **任务独占的全新临时 Profile** | 共享常驻 Profile | 杜绝会话泄漏与锁争用，任务结束立刻清理，安全可靠 |
| D8 | 导航拦截边界 | **请求发出前前置拦截顶层导航** | 允许任意导航 / 拦截所有子资源 | 杜绝 Agent 越权访问非白名单网站，同时不影响页面正常加载外部依赖 CDN |
| D9 | 截图工件存储 | **集中落盘于 Orcsy 配置运行目录** | 保存至项目 worktree 内部 | 彻底避免未暂存的测试截图误入 Git 历史记录污染代码库 |
| D11| 主机白名单层级 | **全局 ∪ 项目追加（只增不减，硬禁区恒拒）** | 允许项目覆盖替换 | 兼顾局域网测试环境诉求，同时守住全局安全底线 |
| D12| 引擎选择策略 | **支持 `auto \| chrome \| edge`** | 仅支持固定路径 | Windows 默认自带 Edge 保底可用，开发者安装 Chrome 亦能无缝使用 |

---

## 7. 风险与缓解

| 潜在风险 | 缓解与防范手段 |
|----------|----------------|
| 浏览器或开发服务孤儿进程残留 | 三层兜底清理机制（Session 退出、进程级 atexit 钩子、系统启动扫描），测试用例强制断言清理效果 |
| Windows 文件句柄占用导致临时 Profile 目录清理失败 | 进程终止后加入带重试退避的删除逻辑；失败记警告并交由下次系统启动时清理，不阻断主流程 |
| 页面大量 DOM 导致快照超出上下文 | `browser_snapshot` 设置 6000 字符硬截断，支持传 `ref` 进行局部子树查看，旧结果由现有 `_trim_context` 机制自动裁剪 |
| 误删被测系统业务数据 | 敏感操作归入 `ask_on_change` 审批；模板提示词严禁配置生产环境地址 |
| 调试服务端口竞态冲突 | 系统动态挑选可用端口注入 `{PORT}`，遇偶发冲突自动重试一次 |

---

## 8. 迁移与兼容

- **向下兼容**：存量配置未声明 `local_debug` 或未开启对应 `tools` 开关时，系统行为与旧版本 100% 保持一致；
- **降级兼容**：若新版配置中开启了对应工具后回退至旧版本，旧版 `validate_tools_map` 会明确提示未知工具并拒绝启动，移除相应键即可恢复；未知的顶层节会被旧版安全忽略。

---

## 9. 里程碑与工作量

| 里程碑 | 核心建设内容 | 估算工时 |
|--------|--------------|:--------:|
| **M0 技术验证（spike）** | 验证系统 Edge/Chrome 的 `--remote-debugging-port=0` 启动、`DevToolsActivePort` 读取、CDP WebSocket 基础握手与进程杀灭链路，打包验证 `websockets` 库 | 0.5 人日 |
| **M1 调试基座** | `DebugSession` 基础容器；命令白名单解耦；`LocalDebugConfig` 配置与项目白名单解析（`hosts.py`）；落地并测通 `dev_server_*` 与 `http_request` 核心逻辑及单测 | 3.5 人日 |
| **M2 浏览器引擎** | `browser/` 模块：发现拉起、CDP 客户端连接管理、页面脚本注入（快照与 ref 定位）、前置导航拦截；实现 10 个 `browser_*` 工具；Mock CDP 单测与真实浏览器 E2E 测试 | 4.5 人日 |
| **M3 调度与控制** | 工具分组扩展；`DEFAULT_OFF_TOOLS` 生效；审批分类扩展；系统提示词与防打转豁免联动；Web 接口、配置保存链路及看板前端调试设置交互；完整任务演练 | 3.5 人日 |
| **M4 体验与收尾** | 看板截图工件查验与展示；AGENT.md 模板更新；README 与用户指南补充；容器支持说明；全量回归测试验证 | 2.0 人日 |

**总计工作量**：**14 人日**（Firefox 移出范围后，较 v2 规划缩减 5 人日，技术风险完全消除）。

---

## 10. 评审结论与定案记录

本方案经过两轮需求评估与评审，所有关键设计已全部敲定：

1. **默认状态**：新工具默认全部关闭，按需激活；
2. **视觉策略**：本期不引入多模态视觉输入，文本快照为主，截图仅供人工查验；
3. **白名单体系**：支持项目级追加主机，实行只增不减与敏感网段强制拉黑规则；
4. **浏览器范围**：**仅支持 Chrome / Edge（Chromium 系）**，通过统一 CDP 协议直连系统浏览器，不依赖外部 Driver，Firefox 移出范围。

方案定案，可按 M0 → M1 → M2 → M3 → M4 阶段推进落地。
