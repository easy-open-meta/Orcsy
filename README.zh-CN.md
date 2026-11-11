# Orcsy

[English](README.md) | 简体中文

📖 **[用户指南（Wiki）](docs/用户指南.md)** —— 面向普通使用者的安装部署、功能说明与逐步操作教程。

> Orcsy 是一个接入任意大模型 API 的 Agent 任务编排系统：把任务写成 Markdown，定时调度器扫描每个受管项目的 `tasks/` 目录，按 P0 → P4 优先级派发给该项目专属 Agent，Agent 在独立 git worktree 里真实地读写代码、跑测试，完成后自动提交合并、回写任务状态——全程 Web 看板可视化掌控，无人值守闭环运转。

![version](https://img.shields.io/badge/version-0.23.0-blue) ![python](https://img.shields.io/badge/python-3.11%2B-green) ![platform](https://img.shields.io/badge/platform-Windows%20%7C%20Linux%20%7C%20Docker-lightgrey) ![license](https://img.shields.io/badge/license-Apache--2.0-orange)

```
┌────────────────────────────────── Orcsy ────────────────────────────────┐
│                          定时调度器（scheduler）                         │
│     每隔 interval_seconds 驱动一轮扫描（可串行或多 Agent 并行）           │
└──────┬────────────────────────┬────────────────────────┬───────────────┘
       ▼                        ▼                        ▼
┌─────────────┐          ┌─────────────┐          ┌─────────────┐
│ Agent: app1 │          │ Agent: app2 │          │ Agent: appN │  每项目一个 Agent
│ 读 AGENT.md │          │ 读 AGENT.md │          │ 读 AGENT.md │
│ 扫 tasks/   │          │ 扫 tasks/   │          │ 扫 tasks/   │
│ 排优先级    │          │ 排优先级    │          │ 排优先级    │
│ worktree执行│          │ worktree执行│          │ worktree执行│
└──────┬──────┘          └──────┬──────┘          └──────┬──────┘
       ▼                        ▼                        ▼
   大模型 API（OpenAI 兼容 / Anthropic 协议，任意厂商）
       │
       └── Web 看板 http://127.0.0.1:5116（状态总览 / 任务增删改 / 手动触发 / 日志）
```

## ✨ 特性

- **多模型 Agent 项目小组（Project Team）** —— 自由编排 **👑 项目经理 / ⚙️ 后端 / 🎨 前端 / 🧪 测试 QA** 进驻项目：PM 只读摸底、拆解 DAG 子任务（`depends_on_refs`）或直接转派；前后端在任务隔离的 Worktree 沙盒中专端开发，Prompt 自动透传上游接口契约；QA 拉起本地服务与浏览器做端到端联调，缺陷经 `report_bug` 派发修复任务并自动回归；依赖解锁后同周期链式接棒（`chain_within_cycle`），单个需求一个周期完整闭环。
- **🔍 独立审查 Agent（合并前把关）** —— 任务完成先落 WIP 提交，由**另一个不同模型**只读审查（对照验收标准抽查、可实际跑测试），通过才合并归档；不通过带结构化问题清单自动返工（次数可调），杜绝「自己审自己」。
- **🛡️ 数据脱敏（密钥不出本地）** —— Agent 读到的 API 密钥、数据库密码**发送模型厂商前在本地掩除**：处理只在内存进行（不写磁盘、不进日志、任务结束即清除）；高置信规则开箱即用（sk-/ghp_/JWT/PEM 私钥块/Bearer/URL token/连接串密码段），模型只见 `[REDACTED:api_key:…]` 占位符，写回文件自动还原原文；键值对增强规则与自定义正则按需开启。
- **🌐 本地调试与浏览器自动化** —— 纯 Python CDP 直连本地 Chrome / Edge（零 driver 依赖）：页面结构化快照、按 ref 点击输入、截图与 Console/Network 监听；配合 `dev_server_*` 后台服务起停与 `http_request` 本地接口调试（主机白名单约束），构成 Web/API 联调闭环。
- **任意大模型** —— OpenAI 兼容协议（DeepSeek / GLM / Kimi / 通义 / Ollama / vLLM…）与 Anthropic 协议均原生 function calling，改 `base_url` + `model` 即可切换。
- **Markdown 即任务** —— 每个任务一个 `.md` 文件：frontmatter 写元信息、正文写需求与验收标准，纯文本可版本化、可人工编辑。
- **每项目一个专属 Agent / 小组** —— 独立的 `AGENT.md` 行为准则、任务清单、worktree 与对话历史；多 Agent 并行；支持项目级专属模型绑定。
- **🛠️ 工具能力与三级审批** —— 文件工具禁锢在项目 worktree 内、90+ 默认命令白名单（9 大生态分组，看板可视化开关）、三档访问权限（变更前询问 / 自主模式 / 完全访问，敏感操作挂起等看板审批）；缺工具链时按内置 `env-setup` 技能静默安装（国内无代理网络自动切镜像直下）。
- **并发子代理探索** —— PM 与开发角色可一次派生 2~4 个**只读子代理**并行摸底不同模块，单轮拿汇总报告，串行探索耗时成倍压缩。
- **技能库（Skill）** —— 导入本地目录 / 看板新建 / 一键扫描 ZCode / Claude Code / Codex 等工具的用户级技能批量导入；提示词只注入「名称+描述」索引，全文按需 `read_skill`，不撑爆上下文。
- **仓库 Issues 集成** —— 绑定 GitHub / Gitee / GitLab / Gitea 仓库（含自托管），open Issue 一键转任务（标签映射优先级、归档去重、单向导入）。
- **Web 可视化看板** —— 状态总览、任务增删改与拖拽排序、**人工把关提交页**（文件清单 + diff 对比，一键确认 commit/push）、模型连通测试、运行日志与模型思考查看；托盘常驻、开机自启、自更新（零 UAC / 系统目录一次提权）、任务结束提醒、按角色用量台账与 12 周热力图。
- **开箱即用的项目接入** —— 新建/导入项目只新增 `tasks/` 与 `AGENT.md`；自动探测技术栈预填运行与测试命令。

## 🚀 快速开始

### 方式一：Windows 安装包（推荐）

1. 运行 `build/Setup/orcsy-setup-<版本>-windows-amd64.exe`，按向导安装；
2. 可勾选「初始化示例任务」在数据目录放置示例模板；**config.yaml 无需手动初始化**——首次启动时自动生成在数据目录 `%APPDATA%\Orcsy`（Roaming，本地实体，不受 OneDrive 干扰）；
3. 打开看板，在「项目与设置」页填写 `llm.api_key`、`base_url`、`model` 并新建项目（也可直接编辑 `%APPDATA%\Orcsy\config.yaml`，`projects[].path` 用**绝对路径**）；
4. 从开始菜单/桌面打开「任务编排系统」→ 浏览器自动打开看板；
5. 在看板上新建任务（或往项目 `tasks/` 目录里放任务文件），点「立即扫描一轮」或等定时调度。

安装包自带卸载器；卸载时可选是否删除用户数据（`%APPDATA%\Orcsy`）。

### 方式二：源码运行

```bash
# 1. 安装（本机需有 uv；首次运行自动准备 Python 3.11+ 环境）
git clone <repo-url> orcsy && cd orcsy
uv sync

# 2. 初始化运行目录（生成 config.yaml 与模板）
uv run orcsy init --dir run

# 3. 编辑 run/config.yaml：填 api_key、base_url、model，把 projects 指向你的项目
#    并在每个受管项目里放 AGENT.md 与 tasks/*.md（可从模板复制）

# 4. 校验配置与任务清单
uv run orcsy validate --config run/config.yaml

# 5. 启动 Web 看板（含定时调度，自动打开浏览器）
uv run orcsy web --config run/config.yaml

# 也可以纯 CLI 持续调度（无看板）
uv run orcsy run --config run/config.yaml

# 试运行一轮（不进入持续调度）
uv run orcsy run --config run/config.yaml --once
```

也可以用外部调度器触发单轮（Windows 计划任务 / cron）：配置 `scheduler.run_once: true` 或命令行 `--once`。

### 方式三：Docker 部署（服务器 / Linux / NAS）

```bash
# 1. 构建并启动（数据落在 ./orcsy-data，受管仓库放 ./projects）
docker compose up -d --build

# 2. 打开看板 http://localhost:5116，在「项目与设置」页填写 llm.api_key，
#    并把项目路径指向容器内挂载点 /projects/<项目名>
```

详见下方「Docker 部署」章节。

### 方式四：Linux 原生部署（systemd 服务）

```bash
# 1. 上传/克隆源码到服务器,在仓库根目录执行
sudo bash deploy/linux/install.sh

# 2. 打开看板 http://<服务器IP>:5116,填写 llm.api_key 并新建项目
```

详见下方「Linux 原生部署」章节。

## 🐳 Docker 部署

镜像基于 Python 3.12 slim，多阶段 uv 构建、按 `uv.lock` 可复现；内置 `git` 与 tini（PID 1 转发信号），应用进程以非 root 用户（orcsy）运行，宿主挂载卷权限由入口脚本自动修正。容器内自动关闭托盘与自动开浏览器（`--no-tray --no-browser`），核心能力（看板 + 定时调度 + Agent 执行）与桌面版一致。

> ⚠️ **容器以 `--host 0.0.0.0` 对外监听，必须设置登录密码。** 首次启动会自动生成 `/data/config.yaml` 模板，随后因「非回环绑定 + 空 `web.auth_password`」拒绝启动（日志里有一条可行动提示）。编辑 `./orcsy-data/config.yaml`，把 `web.auth_password` 设为强密码，再 `docker compose restart` 即可；之后也可在看板设置页改密码。

### 数据与挂载

| 宿主目录 | 容器内 | 内容 |
|---|---|---|
| `./orcsy-data` | `/data` | `config.yaml`、`skills/`、`logs/`（由环境变量 `ORCSY_DATA_DIR` 指定） |
| `./projects` | `/projects` | 受管 git 仓库 |

**受管项目路径必须写容器内路径**：编辑 `./orcsy-data/config.yaml`（或直接在看板「项目与设置」页操作），`projects[].path` 填 `/projects/<项目名>`：

```yaml
projects:
  - name: my-app
    path: /projects/my-app   # 对应宿主 ./projects/my-app（需是 git 仓库）
```

### Git 推送凭据

- **HTTPS**：把 token 写进远端地址，如 `https://<用户名>:<token>@github.com/user/repo.git`（`git remote set-url` 或看板里改）；
- **SSH**：按 `docker-compose.yml` 里的注释挂载 `~/.ssh`（只读）并设置 `GIT_SSH_COMMAND`；
- 提交身份默认 `Orcsy Agent <orcsy@localhost>`，可用环境变量 `ORCSY_GIT_NAME` / `ORCSY_GIT_EMAIL` 覆盖。

### 一次性执行（外部调度）

不想长驻容器时，可用外部 cron 触发单轮：

```bash
docker run --rm -v "$PWD/orcsy-data:/data" -v "$PWD/projects:/projects" \
  orcsy:latest run --once
```

### 扩展工具链

精简镜像只含 Python + git。受管项目若需 node/npm 等命令，新建 `Dockerfile.custom` 叠加安装：

```dockerfile
FROM orcsy:latest
USER root
RUN apt-get update && apt-get install -y --no-install-recommends nodejs npm \
    && rm -rf /var/lib/apt/lists/*
USER orcsy
```

```bash
docker build -f Dockerfile.custom -t orcsy:custom .
# docker-compose.yml 里 image 改为 orcsy:custom
```

## 🐧 Linux 原生部署（systemd，非 Docker）

以系统级 systemd 服务常驻：专用用户运行、开机自启、崩溃自动拉起。布局：

| 路径 | 内容 |
|---|---|
| `/opt/orcsy` | 程序与虚拟环境（`.venv`） |
| `/var/lib/orcsy` | 数据目录：`config.yaml`、`skills/`、`logs/`（由 `ORCSY_DATA_DIR` 指定） |
| `/srv/orcsy-projects` | 受管 git 仓库（`projects[].path` 写 `/srv/orcsy-projects/<项目名>`） |

### 安装

```bash
# 上传/克隆源码后,在仓库根目录执行(需要 root;自动装 uv、建用户、装服务并启动)
sudo bash deploy/linux/install.sh
```

打开 `http://<服务器IP>:5116`,在看板「项目与设置」页填写 `llm.api_key` 并新建/导入项目(路径填 `/srv/orcsy-projects/<项目名>`,需是 git 仓库)。

服务以 `--host 0.0.0.0` 监听,因此安装脚本首装时会生成 `/var/lib/orcsy/config.yaml` 并写入随机登录密码(安装输出里打印;可用 `ORCSY_AUTH_PASSWORD` 环境变量指定)。已有配置文件不会被覆盖——若其中的 `web.host` 非回环而 `web.auth_password` 为空,服务会拒绝启动,补上密码后 `systemctl restart orcsy` 即可。

### 服务管理与日志

```bash
systemctl status orcsy            # 运行状态
sudo systemctl restart orcsy      # 改完 /var/lib/orcsy/config.yaml 后重启生效
journalctl -u orcsy -f            # 跟踪服务日志(文件日志在 /var/lib/orcsy/logs/)
```

不想用 systemd 时也可手动前台运行（数据目录默认 `~/.orcsy`）：

```bash
uv sync --no-dev && uv run orcsy web --host 0.0.0.0 --no-tray --no-browser
# 或纯 CLI 供外部 cron 调度:uv run orcsy run --host 0.0.0.0 --once
```

### Git 推送凭据

与 Docker 部署一致（HTTPS token 或 SSH key），提交身份默认 `Orcsy Agent <orcsy@localhost>`，可用环境变量 `ORCSY_GIT_NAME` / `ORCSY_GIT_EMAIL` 覆盖（安装时导出，或直接编辑 `/var/lib/orcsy/.gitconfig`）。

### 卸载

```bash
sudo bash deploy/linux/uninstall.sh
# 数据目录 /var/lib/orcsy 与 /srv/orcsy-projects 默认保留,按脚本输出提示手动清理
```

## 🔐 看板登录鉴权（外网部署必读）

看板默认只监听 `127.0.0.1` 且无鉴权（本机使用零打扰）。以 Docker / systemd / `--host 0.0.0.0` 等方式对外暴露时**必须设置登录密码**——host 非回环且密码为空时程序会直接拒绝启动、看板也会拒绝保存该配置（看板可浏览服务器目录、写入任务、启停 Agent）。密钥类信息（LLM API Key、更新源 token、项目仓库 token）在 `/api/state` 中只返回掩码，输入新值才会替换、留空即保持原值：

```yaml
web:
  host: 0.0.0.0
  port: 5116
  auth_password: "你的强密码"   # 非空即开启鉴权；留空 = 关闭
```

也可以在登录后于看板「设置 → 应用」里设置/修改/清除密码（保存后立即生效）。

行为说明：

- 开启后除登录页外均需登录；密码错误延迟惩罚防爆破；会话 Cookie（HttpOnly，7 天）存于内存，重启需重新登录；改密即踢全部旧会话。
- 密码以 **PBKDF2-HMAC-SHA256 (100,000 次加盐哈希)** 密文存储；忘记密码时在服务器执行 `orcsy reset-password` 快捷重置（`--clear` 清空）。
- 鉴权不替代 TLS：公网部署请再套一层反向代理（nginx/caddy）上 HTTPS。

### 数据脱敏（敏感信息不出本地）

Agent 干活时会读工作区文件、执行命令——里面的 API 密钥、数据库密码默认**发送模型厂商前在本地掩除**：处理只在内存中进行（不写磁盘、不进日志、任务结束即清除）；高置信规则默认生效（sk-/ghp_/JWT/PEM 私钥块/Bearer/URL token/连接串密码段等），模型看到 `[REDACTED:api_key:xxxx]` 占位符，写回文件时自动还原原文，finish 结论保持脱敏形态（密钥不落任务文件）。键值对增强规则（`api_key=xxx`）与自定义正则可在设置页「🛡 数据脱敏」或 config.yaml `masking` 节调整。

## 🧩 核心概念

### 任务文件

每个任务一个 Markdown 文件，放在项目的 `tasks/` 目录，文件名即任务 ID。YAML frontmatter 写元信息，正文写需求/现象/验收标准：

```markdown
---
title: 修复登录页崩溃
priority: P1        # P0 最高，P4 最低；数字越小越先做
status: open        # open / in_progress / done / blocked / cancelled
labels: [bug, login]
assignee:           # 留空任意 Agent 可领；填写 Agent 名则只有该 Agent 处理
---

## 现象
……

## 验收标准
- [ ] …
```

- 系统每轮扫描 `status: open` 的任务，**按 P0 → P4 优先级排序**后依次执行；
- 执行前状态置为 `in_progress`，结束后回写 `done` / `blocked` 并附「Agent 处理结果」小节（摘要、提交号、变更文件、受阻原因）；`done` 的任务自动归档到 `tasks/done/`；
- 文件格式错误（缺 title、priority 非法等）会以 HTML 注释形式标注在文件头，等人工修复。

### AGENT.md 开发指引

每个项目根目录一份 `AGENT.md`（可通过 `guide_file` 配置改用其它文件名）。Agent 执行任务时把它注入系统提示词，作为该项目的最高行为准则。看板新建/导入项目时自动生成英文模板，并自动探测技术栈填入 Stack / Run / Test；模板见 `orcsy/templates/AGENT.md`。

### 多 Agent

- **一个项目一个 Agent**：`config.yaml` 的 `projects` 列表里每项就是一个独立 Agent，各有指引、任务清单、worktree 和对话历史；
- `scheduler.max_parallel_agents > 1` 时多个项目的 Agent **并行**工作（线程池）；
- 同一项目内可用 `assignee` 把不同任务指派给不同 Agent 名（配合多份配置分进程跑）。

### 大模型接入

OpenAI 兼容协议覆盖绝大多数厂商，改 `base_url` + `model` 即可：

| 厂商 | base_url | model 示例 |
|---|---|---|
| DeepSeek | `https://api.deepseek.com/v1` | `deepseek-chat` |
| GLM 智谱 | `https://open.bigmodel.cn/api/paas/v4` | `glm-4-plus` |
| Kimi | `https://api.moonshot.cn/v1` | `moonshot-v1-32k` |
| OpenAI | `https://api.openai.com/v1` | `gpt-4o` |
| Ollama 本地 | `http://127.0.0.1:11434/v1` | `qwen2.5-coder:14b` |

也支持 Anthropic 协议（`provider: anthropic`）。设置页「请求侧开启模型思考」可让 Anthropic 返回 thinking 块（Qwen/GLM 等兼容服务同理），思考内容会写入运行日志、看板可展开查看。

### Agent 工具循环

Agent 执行任务时通过一组**禁锢在项目内的工具**真实地工作：

| 工具 | 作用 |
|---|---|
| `list_dir` / `find_files` / `grep` | 列结构 / 按名查找 / 正则搜索（自动忽略 .git/node_modules 等） |
| `read_file` / `write_file` / `edit_file` / `multi_edit` | 读（超长截断）/ 写 / 精确替换（old_string 须唯一）/ 同文件多处替换（全有或全无） |
| `delete_file` / `move_file` | 删除（删目录须 `recursive: true`）/ 移动重命名（已存在须 `overwrite: true`） |
| `run_command` | 执行白名单内命令（python/node/git/包管理器等，用于跑测试与构建）；缺工具链时按 `env-setup` 技能静默安装；新装工具立即可用（现读用户 PATH + 常见安装目录解析） |
| `web_fetch` | 抓取公网 URL 正文（仅公网地址，内网/回环/云元数据一律拒绝） |
| `browser_*` / `dev_server_*` / `http_request` | 本地调试三件套（见上文特性），默认关闭按需开启 |
| `read_skill` | 读取技能库全文 |
| `finish` | 结构化结论（completed / blocked / cannot_do + 摘要 + 提交信息） |

安全边界：

- 文件工具禁锢在项目根（worktree）内，绝对路径与 `../` 穿越一律拒绝；
- 命令白名单（`projects[].allowed_commands` 可整表替换），非白名单命令直接拒绝；
- `web_fetch` 是唯一联网工具，仅 http/https 且拒绝一切内网目标；
- 工具循环有轮数上限（`projects[].max_agent_turns`，默认 40，-1 不限轮数但仍有防死循环护栏）；
- git 提交/合并/推送由系统执行，Agent 无权操作版本控制。

所有工具（`finish` 恒可用除外）均可按名或按组开关，全局默认 + 项目级覆盖，看板可视化编辑，保存立即生效（schema 下发与 dispatch 层双层拦截）。本地调试组（browser / dev_server / http_request）默认关闭。

### 访问权限模式（人工审批）

| 模式 | 语义 |
|---|---|
| `ask_on_change` | 变更前询问：一切写/改/删/移/执行命令操作挂起等批准 |
| `autonomous` | 自主模式：仅高风险操作（删/移/执行命令）询问 |
| `full_access` | 完全访问（默认，不询问） |

只读工具与 finish 永不询问；审批挂起零副作用，看板顶栏「🔔 待审批」逐条批准/拒绝（可勾选本任务内始终允许），超时自动拒绝并以错误文本反馈给模型。全局默认 + `projects[].permission_mode` 项目级覆盖，`permissions.timeout_minutes` 控制审批超时。

### Git 集成

受管项目需是 git 仓库。任务执行时：Agent 全部改动发生在独立 worktree（`.worktree-arranger/`，分支 `arranger/<项目名>`）；报告 `completed` 后系统按其 commit message 提交并 `--no-ff` 合并回主分支（配置 `origin` 则自动推送）；报告 `blocked` / `cannot_do` 或合并冲突时丢弃/保留分支、任务标 `blocked` 等人工处理。

## 🖥 Web 看板

`orcsy web` 启动后访问 [http://127.0.0.1:5116](http://127.0.0.1:5116)：状态总览、项目卡片、任务增删改与拖拽排序、手动触发扫描、执行中任务强制停止、用量台账与热力图、运行日志（模型思考可折叠），页面 5 秒自动刷新。

自定义端口/地址：命令行 `--port / --host` > `config.yaml` 的 `web.port / web.host`。端口被其它程序占用且未显式指定时自动换口并提示；显式指定被占用则报错退出。

```bash
orcsy web --port 8080          # 冲突时换这个端口
orcsy web --host 0.0.0.0       # 允许局域网访问（注意设置登录密码）
```

## ⚙️ 配置参考

完整配置见仓库根目录的 `config.example.yaml`（每项带中文注释；与 `orcsy/__main__.py` 的 `CONFIG_EXAMPLE` 常量保持一致）。安装版数据目录默认 `%APPDATA%\Orcsy`，环境变量 `ORCSY_DATA_DIR` 整体改址。关键项：

| 配置 | 说明 |
|---|---|
| `llm.*` | provider（`openai` 兼容 / `anthropic`）、base_url / model / api_key、enable_thinking（请求侧开启模型思考） |
| `scheduler.interval_seconds` / `max_parallel_agents` | 扫描间隔（默认 300 秒）/ 并行 Agent 数 |
| `web.host / port / open_browser` | 看板监听地址 / 端口（默认 5116）/ 自动开浏览器 |
| `logging.retention_days` / `max_total_mb` | 日志保留天数（默认 7）/ 总占用上限 MB（默认 200），任一命中即从最旧清理 |
| `tools` / `commands` | 全局工具开关与命令白名单开关（`{名: bool}`，未列出默认启用/放行；`finish` 不可禁用） |
| `env.vars` | 注入 Agent 子进程的环境变量：国内拉依赖失败时配 `GOPROXY` / `HTTP_PROXY` / `PIP_INDEX_URL` 等（不允许覆盖 PATH 等系统变量） |
| `permissions.mode` / `timeout_minutes` | 访问权限模式（ask_on_change / autonomous / full_access）/ 审批超时分钟（默认 30） |
| `review.*` | 任务审查（Review Agent）：`llm` 绑定审查模型（**必须与任务模型不同**，默认 `~` = 审查未启用）、`max_turns` 审查轮数、`max_review_rounds` 返工次数（默认 2）、`allow_test_commands` 跑测试验证、`squash_merge` 返工通过后收敛提交；项目级 `review_enabled` / `review_llm` / `review_max_rounds` 可覆盖 |
| `masking.*` | 数据脱敏：`enabled`（默认开）/ `enhanced_rules` 键值对增强规则（默认关）/ `custom_patterns` 自定义正则，详见「看板登录鉴权」章节的数据脱敏小节 |
| `notify.sound` / `system` | 任务结束提醒：内置提示音 / 托盘气泡通知（默认全开，手动停止不提醒） |
| `projects[].path` / `tasks_dir` / `guide_file` | 项目根目录（建议绝对路径）/ 任务目录（默认 `tasks`）/ 指引文件（默认 `AGENT.md`） |
| `projects[].max_tasks_per_cycle` / `max_agent_turns` | 每轮任务数（默认 1）/ 单任务轮数上限（默认 40，**-1 不设上限**） |
| `projects[].llm` | 项目专属模型：部分覆盖全局 `llm`（未写字段继承），只有绑定模型处理该项目任务 |
| `projects[].repo` | 远程仓库绑定 `{url, token?, provider?, api_base?}`；绑定后可拉取 Issues 转任务 |
| `projects[].tools` / `allowed_commands` / `permission_mode` | 项目级工具开关 / 命令白名单（显式列表整表替换默认）/ 权限模式覆盖；缺省跟随全局 |
| `projects[].allowed_branch` / `team` | 指定工作分支 / 多模型项目小组编排（引用全局 `agent_profiles` 预设池） |
| `update.*` | 自更新源仓库（默认官方 Gitee 源）/ 私有令牌 / 启动自动检查 |
| frontmatter `order` | 拖拽排序序号（优先级内决定看板显示与领取顺序；删除该行回到自动排序） |
| frontmatter `requeue_seq` / `review_pending` / `review_round` | 系统自动维护，无需手填：重入队优先领取 / 待审查状态 / 累计返工轮数 |

## 📊 日志与观测

- 控制台 + `logs/orcsy.log`（滚动，5MB × 5 份），每轮调度、每个任务的开始/结束、提交号、受阻原因均有记录；
- 任务文件本身也是"报表"：每个执行过的任务都带「Agent 处理结果」小节；
- 校验命令 `validate` 可在跑之前确认配置、任务格式、指引文件是否就位。

## 🛠 从源码重新打包

```bash
# 一键打包（版本号 -> 前端构建 -> PyInstaller 双 exe -> Inno Setup 安装包 + zip 更新包）
build.bat

# 发布 Gitee Release：一条命令同时上传三个附件（首次安装 exe + 自更新 zip
# + 同名 zip.sha256 校验文件；缺 sha256 时在线更新会拒绝安装）；
# token 用 --token / 环境变量 GITEE_TOKEN / 配置 update.token 三选一：
uv run python build/release.py

# 版本规则：功能大变更 → 次版本 +1（0.13.0）；功能补丁 → 修订号 +1（0.12.3）
# 版本唯一来源 orcsy/__init__.py 的 __version__，打包前改它即可

# 或手动分步：
# 1. 打包 exe（需要 dev 依赖：uv sync --dev；产物 dist/*.exe）
uv run pyinstaller orcsy.spec --noconfirm

# 2. 编译安装包（需要 Inno Setup 6，产物 build/Setup/*.exe）
cd build && ISCC.exe installer.iss
```

## 📁 项目结构

```
orcsy/
  __main__.py      CLI（init / validate / run / web）
  config.py        配置模型与加载
  scheduler.py     定时调度（串行 / 并行多 Agent，线程安全，供 Web 触发）
  agent.py         项目 Agent：扫描→排序→领取→执行→回写
  executor.py      单任务执行：worktree、Agent 循环、提交合并
  task_model.py    任务文件解析与优先级排序
  llm.py           大模型客户端（OpenAI 兼容 / Anthropic）与 Agent 工具循环
  tools.py         Agent 工具集（路径禁锢、命令白名单、finish 协议）
  prompts.py       系统提示词构建（注入 AGENT.md 指引）
  probe.py         项目技术栈静态探测（自动填充 AGENT.md）
  templates.py     内置模板读取与 AGENT.md 生成（唯一文案来源）
  web.py           Web 看板（HTTP 服务 + JSON API + 内嵌单页 UI）
  git_ops.py       git 操作封装
  tray.py          托盘常驻与开机自启
  logging_setup.py 日志
  templates/       AGENT.md 与任务模板
dashboard/         Web 看板前端工程（Vue 3 + Naive UI，构建产物编译进 exe）
build/
  build.py         一键构建流水线（版本号单源化 → PyInstaller → Inno Setup + zip 更新包）
  release.py       发布 Gitee Release（首次安装 exe 与自更新 zip 一并上传）
  installer.iss    Inno Setup 安装包脚本
  icon.ico/.png    应用图标
  Setup/           编译产物 orcsy-setup-*.exe 与 orcsy-update-*.zip
tests/             端到端自检（模拟 LLM / 真实 HTTP）
orcsy.spec PyInstaller 打包配置（产物 dist/*.exe）
Dockerfile         容器镜像（多阶段 uv 构建，git + Python 精简基础）
docker/            容器入口脚本 entrypoint.sh（git 环境初始化）
docker-compose.yml 开箱即用的编排示例（/data 与 /projects 挂载）
deploy/linux/      Linux 原生部署（systemd unit 与安装/卸载脚本）
```

## 🔒 安全与边界

- 含 `api_key` 的 `config.yaml` 已在 `.gitignore`，不要提交或分享；安装版数据目录为 `%APPDATA%\Orcsy`（配置、技能库、日志都在这里），不在程序目录；
- 看板默认只监听 `127.0.0.1`；对外暴露必须设置登录密码（见「看板登录鉴权」章节），密钥类信息在 API 中只返回掩码；
- 敏感信息出站前本地脱敏（见「数据脱敏」小节）；文件工具禁锢在项目内、命令白名单、`web_fetch` 拒绝内网目标；
- Agent 改动限定在受管项目内，合并冲突转 blocked 不强推；最终 `git commit` / `push` 由人工把关；
- `blocked` / `cannot_do` 任务写明原因，看板「重新入队」即可重新进队列；
- 默认指引要求：涉及依赖、CI、密钥的变更一律 blocked 等人工确认（可在 AGENT.md 调整）。

## 📄 许可证

[Apache-2.0](LICENSE)
