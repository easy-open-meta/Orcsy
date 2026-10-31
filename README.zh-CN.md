# Orcsy

[English](README.md) | 简体中文

> Orcsy 是一个接入任意大模型 API 的 Agent 任务编排系统：定时扫描各项目 `tasks/` 目录下的 Markdown 任务，按优先级自动派发给专属 Agent 完成开发与修复，并自动提交 Git、回写任务状态，让整个研发流程无人值守闭环运转。

![version](https://img.shields.io/badge/version-0.12.2-blue) ![python](https://img.shields.io/badge/python-3.11%2B-green) ![platform](https://img.shields.io/badge/platform-Windows%20%7C%20Linux%20%7C%20Docker-lightgrey) ![license](https://img.shields.io/badge/license-Apache--2.0-orange)

把任务写成 Markdown，剩下的交给 Orcsy：定时调度器扫描每个受管项目的 `tasks/` 目录，按 P0 → P4 优先级派发给该项目专属 Agent，Agent 在独立 git worktree 里真实地读写代码、跑测试，完成后自动提交合并、回写任务状态，全程提供 Web 可视化看板与运行日志。

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

- **任意大模型** —— OpenAI 兼容协议（DeepSeek / GLM / Kimi / 通义 / Ollama / vLLM…）与 Anthropic 协议均原生 function calling，改 `base_url` + `model` 即可切换；可选开启模型思考内容查看。
- **Markdown 即任务** —— 每个任务一个 `.md` 文件，frontmatter 写元信息、正文写需求与验收标准，纯文本可版本化、可人工编辑。
- **每项目一个专属 Agent** —— 独立的 `AGENT.md` 行为准则、任务清单、git worktree 与对话历史；支持多 Agent 并行；支持绑定**专属模型**（部分覆盖全局 llm 配置，未写字段自动继承），只有绑定的模型处理该项目的任务。
- **真实执行，安全禁锢** —— 文件工具锁定在项目 worktree 内、命令白名单、轮数上限；git 提交/合并/推送由系统执行，Agent 无权操作版本控制。
- **开箱即用的项目接入** —— 新建/导入现有项目只新增 `tasks/` 与 `AGENT.md`，不动项目其它文件；自动探测技术栈（Node/Python/Rust/Go/.NET/Java/C++…）预填运行与测试命令，也可手动指定。
- **技能库（Skill）** —— 支持导入本地技能目录（含 SKILL.md）、看板新建技能，或一键扫描常见 AI 工具（ZCode / 通用 Agent / Claude Code / Codex / OpenCode 等）的用户级技能并批量勾选导入（同名去重）；技能可全局生效或绑定单个项目；删除技能只清技能库中的副本——外部导入的原样本仍在各工具目录里，重新扫描即可找回（自建技能删除后不可恢复）；技能统一存放在 Orcsy 数据目录（`%APPDATA%\Orcsy\skills\`），**绝不写入项目目录**；Agent 系统提示词只注入「名称+描述」索引，执行时按需经 `read_skill` 工具读取全文，技能再多也不撑爆上下文。
- **Web 可视化看板** —— 状态总览、任务增删改、一键重新入队 blocked、手动触发扫描、模型列表拉取与一键连通性测试（发送极短对话验证密钥/模型可用）、运行日志从新到旧展示（模型思考内容可折叠展开）；托盘常驻、开机自启、Windows 安装包开箱即用。

## 🚀 快速开始

### 方式一：Windows 安装包（推荐）

1. 运行 `build/Setup/orcsy-setup-<版本>.exe`，按向导安装；
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
| `list_dir` | 列出项目文件结构（自动忽略 .git/node_modules 等） |
| `read_file` | 读文件（超长自动截断） |
| `write_file` | 创建/整体重写文件 |
| `edit_file` | 精确文本替换（old_string 须唯一匹配） |
| `grep` | 正则搜索代码 |
| `run_command` | 执行白名单内的命令（python/pytest/node/cargo/go/dotnet/git/uv 等），用于跑测试与构建 |
| `finish` | 给出结构化结论（completed / blocked / cannot_do + 摘要 + 提交信息） |

安全边界：

- 文件工具被禁锢在项目根（worktree）内，绝对路径与 `../` 穿越一律拒绝；
- 命令工具有白名单（`projects[].allowed_commands` 可自定义），非白名单命令直接拒绝；
- 工具循环有轮数上限（`projects[].max_agent_turns`，默认 40），超时强制收尾；
- git 提交/合并/推送由系统执行，Agent 无权操作版本控制。

### Git 集成

受管项目需要是 git 仓库。执行任务时：

1. 在项目下创建独立 worktree（`.worktree-arranger/`，分支 `arranger/<项目名>`），Agent 的全部改动都发生在 worktree 里，不污染主工作区；
2. Agent 报告 `completed` 后，系统在 worktree 内按 Agent 给的 commit message 提交，再 `--no-ff` 合并回主分支；配置了 `origin` 远端则自动推送；
3. Agent 报告 `blocked` / `cannot_do` 时丢弃 worktree 改动，任务状态置 `blocked`，等人工处理；
4. 合并失败（冲突等）时保留 `arranger/<项目名>` 分支供人工介入，任务标 `blocked`。

## 🖥 Web 看板

`orcsy web` 启动后访问 [http://127.0.0.1:5116](http://127.0.0.1:5116)：

- **总览**：模型接入信息、调度状态（空闲/执行中、已完成周期、上次扫描、各项目本轮处理数）；
- **项目卡片**：git 状态、AGENT.md 是否就位、任务列表（优先级色标、状态、描述）、格式错误标注、已归档任务；一键打开项目文件夹、移除项目（仅移出配置，不删文件）；
- **任务管理**：新建/编辑/删除任务（写入 `tasks/*.md`）、重新入队 blocked 任务、标记完成；
- **手动触发**：点「立即扫描一轮」后台执行一轮调度（与定时调度互斥，不会重复领取）；
- **运行日志**：按从新到旧展示，模型思考内容默认折叠、可展开查看；页面每 5 秒自动刷新。

配置项 `[web] host / port / open_browser` 可改监听地址、端口与是否自动开浏览器。

**自定义端口**（优先级从高到低）：命令行 `--port 8080`（监听地址同理 `--host`）> `config.yaml` 的 `web.port`。端口冲突时：

- 占用者是**另一个本系统实例**（`/api/state` 可访问）：提示已有看板在运行后退出，避免误开两个；
- 占用者是**其它程序**且用配置端口启动：自动换一个空闲端口并在控制台提示；
- **显式指定** `--port` 而它被其它程序占用：报错退出（尊重显式选择，不悄悄换口）。

```bash
orcsy web --port 8080          # 冲突时换这个端口
orcsy web --host 0.0.0.0       # 允许局域网访问（注意自行加鉴权）
```

## ⚙️ 配置参考

见仓库根目录的 `config.example.yaml`（与 `orcsy/__main__.py` 中的 `CONFIG_EXAMPLE` 常量保持一致，`init` 命令即由它生成 `config.yaml`；改动常量后需同步模板文件，`tests/test_config_example.py` 会校验）。安装版数据目录默认 `%APPDATA%\Orcsy`，可用环境变量 `ORCSY_DATA_DIR` 整体改址。关键项：

| 配置 | 说明 |
|---|---|
| `llm.provider` | `openai`（兼容协议）或 `anthropic` |
| `llm.base_url` / `llm.model` / `llm.api_key` | 模型服务地址、模型名、密钥 |
| `llm.enable_thinking` | 请求侧开启模型思考（Anthropic 必须；多数兼容服务默认返回） |
| `scheduler.interval_seconds` | 扫描间隔，默认 300 秒 |
| `scheduler.max_parallel_agents` | 并行 Agent 数 |
| `web.host / web.port / web.open_browser` | Web 看板监听地址 / 端口（默认 5116）/ 自动开浏览器 |
| `logging.retention_days` | 日志保留天数（默认 7，0 关闭）：活动日志写满 N 天归档进 `logs/archive/`，归档与大小滚动的旧日志超过 N 天自动清理；看板「调度与看板」页可改，保存立即执行 |
| `projects[].path` | 项目根目录（建议绝对路径） |
| `projects[].tasks_dir` | 任务清单目录，默认 `tasks` |
| `projects[].guide_file` | 开发指引文件，默认 `AGENT.md` |
| `projects[].max_tasks_per_cycle` | 每轮最多执行的任务数，默认 1 |
| `projects[].max_agent_turns` | 单个任务工具循环轮数上限，默认 40 |
| `projects[].allowed_commands` | Agent 命令白名单，缺省用内置默认 |
| `projects[].allowed_branch` | 只在该分支上工作，缺省用当前分支 |
| `projects[].llm` | 项目专属模型绑定：部分覆盖全局 `llm`（provider / base_url / api_key / model / temperature 等任意字段，未写字段继承全局）；只有绑定的模型处理该项目的任务，看板项目对话框可可视化编辑 |

## 📊 日志与观测

- 控制台 + `logs/orcsy.log`（滚动，5MB × 5 份），每轮调度、每个任务的开始/结束、提交号、受阻原因均有记录；
- 任务文件本身也是"报表"：每个执行过的任务都带「Agent 处理结果」小节；
- 校验命令 `validate` 可在跑之前确认配置、任务格式、指引文件是否就位。

## 🛠 从源码重新打包

```bash
# 一键打包（版本号 -> 前端构建 -> PyInstaller 双 exe -> Inno Setup 安装包）
build.bat

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
  build.py         一键构建流水线（版本号单源化 → PyInstaller → Inno Setup）
  installer.iss    Inno Setup 安装包脚本
  icon.ico/.png    应用图标
  Setup/           编译产物 orcsy-setup-*.exe
tests/             端到端自检（模拟 LLM / 真实 HTTP）
orcsy.spec PyInstaller 打包配置（产物 dist/*.exe）
Dockerfile         容器镜像（多阶段 uv 构建，git + Python 精简基础）
docker/            容器入口脚本 entrypoint.sh（git 环境初始化）
docker-compose.yml 开箱即用的编排示例（/data 与 /projects 挂载）
deploy/linux/      Linux 原生部署（systemd unit 与安装/卸载脚本）
```

## 🔒 安全与边界

- 含 `api_key` 的 `config.yaml` 已在 `.gitignore`，不要提交或分享；安装版数据目录为 `%APPDATA%\Orcsy`（配置、技能库、日志都在这里，本地实体不受 OneDrive 干扰），不在程序目录；0.8.x 及更早版本放在「文档」目录的配置会在新版首次启动时自动迁移过来（原文件保留作备份）；
- Web 看板默认只监听 `127.0.0.1`，不对局域网开放；如需远程访问自行改 `web.host` 并自行加鉴权（当前版本无登录功能）；
- Agent 的改动范围限定在受管项目内；合并冲突会转为 blocked 而不是强推；
- `blocked` / `cannot_do` 的任务会写明原因，等人工确认后在看板点「重新入队」（或把 status 改回 open）即可重新进入队列；
- 默认指引要求：涉及依赖、CI、密钥的变更一律 blocked 等人工确认（可在 AGENT.md 调整）。

## 📄 许可证

[Apache-2.0](LICENSE)
