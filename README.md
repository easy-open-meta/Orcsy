# Orcsy

English | [简体中文](README.zh-CN.md)

> Orcsy is an Agent task-orchestration system that plugs into any LLM API: it periodically scans each project's `tasks/` directory for Markdown tasks, dispatches them by priority to a dedicated Agent that does the development and fixes, then automatically commits to Git and writes the task status back — an unattended, closed-loop development pipeline.

![version](https://img.shields.io/badge/version-0.15.2-blue) ![python](https://img.shields.io/badge/python-3.11%2B-green) ![platform](https://img.shields.io/badge/platform-Windows%20%7C%20Linux%20%7C%20Docker-lightgrey) ![license](https://img.shields.io/badge/license-Apache--2.0-orange)

Write tasks as Markdown and let Orcsy handle the rest: a scheduler scans each managed project's `tasks/` directory, dispatches tasks by priority (P0 → P4) to that project's dedicated Agent, the Agent reads and writes real code and runs tests inside an isolated git worktree, and on completion Orcsy commits, merges and writes the status back — with a web dashboard and run logs throughout.

```
┌────────────────────────────────── Orcsy ────────────────────────────────┐
│                         Scheduler (periodic)                            │
│   Drives one scan round every interval_seconds (serial or multi-Agent)  │
└──────┬────────────────────────┬────────────────────────┬───────────────┘
       ▼                        ▼                        ▼
┌─────────────┐          ┌─────────────┐          ┌─────────────┐
│ Agent: app1 │          │ Agent: app2 │          │ Agent: appN │  One Agent per project
│read AGENT.md│          │read AGENT.md│          │read AGENT.md│
│ scan tasks/ │          │ scan tasks/ │          │ scan tasks/ │
│ prioritize  │          │ prioritize  │          │ prioritize  │
│run worktree │          │run worktree │          │run worktree │
└──────┬──────┘          └──────┬──────┘          └──────┬──────┘
       ▼                        ▼                        ▼
   LLM API (OpenAI-compatible / Anthropic protocol, any vendor)
       │
       └── Web dashboard http://127.0.0.1:5116 (overview / tasks / trigger / logs)
```

## ✨ Features

- **Any LLM** — OpenAI-compatible protocol (DeepSeek / GLM / Kimi / Qwen / Ollama / vLLM…) and the Anthropic protocol, both with native function calling; switch providers by changing `base_url` + `model`. Optional model-thinking display.
- **Markdown as tasks** — one `.md` file per task: frontmatter for metadata, body for requirements and acceptance criteria. Plain text, versionable, human-editable.
- **One dedicated Agent per project** — its own `AGENT.md` guidelines, task list, git worktree and conversation history; multi-Agent parallelism supported; each project can bind a **dedicated model** (partial override of the global `llm` config, unset fields inherited) — only the bound model handles that project's tasks.
- **Repo Issues integration** — bind a GitHub / Gitee / GitLab / Gitea repo (self-hosted included) to a project and pull its open Issues into the task queue with one click: priority defaults to P2, P0~P4 labels override; tasks carry source annotations with archive-aware dedup; one-way import, Issue status is not written back.
- **Self-update** — check/download new versions from a Gitee Release (auto-check on startup, toggleable), one-click silent install with automatic restart (Windows installed edition; Docker/Linux upgrade via their own means).
- **Real execution, safely sandboxed** — file tools locked inside the project worktree, command whitelist, turn limits; git commit/merge/push is performed by the system, the Agent cannot touch version control.
- **Zero-friction project onboarding** — creating/importing a project only adds `tasks/` and `AGENT.md`, nothing else in the repo is touched; tech stack auto-detection (Node/Python/Rust/Go/.NET/Java/C++…) pre-fills run and test commands, or set them manually.
- **Skill library** — import local skill directories (with SKILL.md), create skills in the dashboard, or one-click scan user-level skills from common AI tools (ZCode / generic Agents / Claude Code / Codex / OpenCode etc.) and batch-import with dedup; skills can be global or bound to a single project; deleting a skill only removes the copy in the library — externally imported originals stay in their tool directories and can be re-scanned (custom-built skills are unrecoverable once deleted); skills live in the Orcsy data directory (`%APPDATA%\Orcsy\skills\`) and are **never written into project directories**; the Agent system prompt only receives a "name + description" index, full content is read on demand via the `read_skill` tool, so the skill count never bloats the context.
- **Web dashboard** — status overview, task CRUD, one-click requeue of blocked tasks, manual scan trigger, model list fetching and one-click connectivity test (sends a tiny chat to verify key/model), run logs newest-first (model thinking content is collapsible); tray icon, autostart on boot, Windows installer out of the box.

## 🚀 Quick Start

### Option 1: Windows installer (recommended)

1. Run `build/Setup/orcsy-setup-<version>.exe` and follow the wizard;
2. Optionally check "Initialize sample tasks" to place sample templates in the data directory; **no manual config.yaml setup needed** — it is auto-generated on first launch at `%APPDATA%\Orcsy` (Roaming; local files, unaffected by OneDrive);
3. Open the dashboard, fill in `llm.api_key`, `base_url` and `model` on the "Projects & Settings" page and create a project (or edit `%APPDATA%\Orcsy\config.yaml` directly; `projects[].path` must be **absolute**);
4. Launch "Task Orchestration System" from the Start menu / desktop → the dashboard opens in your browser;
5. Create tasks on the dashboard (or drop task files into the project's `tasks/` directory), then click "Scan now" or wait for the scheduler.

The installer ships with an uninstaller; during uninstall you may choose whether to delete user data (`%APPDATA%\Orcsy`).

### Option 2: Run from source

```bash
# 1. Install (uv required; Python 3.11+ is prepared automatically on first run)
git clone <repo-url> orcsy && cd orcsy
uv sync

# 2. Initialize the run directory (generates config.yaml and templates)
uv run orcsy init --dir run

# 3. Edit run/config.yaml: fill api_key, base_url, model, point projects at your repos,
#    and place AGENT.md plus tasks/*.md in each managed project (copy from templates)

# 4. Validate config and task lists
uv run orcsy validate --config run/config.yaml

# 5. Start the web dashboard (scheduler included, browser opens automatically)
uv run orcsy web --config run/config.yaml

# Or run pure-CLI scheduling continuously (no dashboard)
uv run orcsy run --config run/config.yaml

# Single trial round (no continuous scheduling)
uv run orcsy run --config run/config.yaml --once
```

You can also trigger single rounds from an external scheduler (Windows Task Scheduler / cron): set `scheduler.run_once: true` or pass `--once`.

### Option 3: Docker (server / Linux / NAS)

```bash
# 1. Build and start (data lands in ./orcsy-data, managed repos go in ./projects)
docker compose up -d --build

# 2. Open http://localhost:5116, fill llm.api_key on the "Projects & Settings" page,
#    and point project paths at the in-container mount /projects/<project name>
```

See the "Docker Deployment" section below.

### Option 4: Native Linux deployment (systemd service)

```bash
# 1. Upload/clone the source to the server, then from the repo root run:
sudo bash deploy/linux/install.sh

# 2. Open http://<server IP>:5116, fill llm.api_key and create projects
```

See the "Native Linux Deployment" section below.

## 🐳 Docker Deployment

The image is based on Python 3.12 slim, built in multi-stage uv steps and reproducible from `uv.lock`; it ships `git` and tini (PID 1 signal forwarding), and the app process runs as a non-root user (`orcsy`) with bind-mount permissions fixed automatically by the entrypoint. Tray and browser-opening are disabled in containers (`--no-tray --no-browser`); core capabilities (dashboard + scheduler + Agent execution) are identical to the desktop edition.

### Data & mounts

| Host directory | In container | Contents |
|---|---|---|
| `./orcsy-data` | `/data` | `config.yaml`, `skills/`, `logs/` (via the `ORCSY_DATA_DIR` env var) |
| `./projects` | `/projects` | Managed git repositories |

**Managed project paths must be in-container paths**: edit `./orcsy-data/config.yaml` (or do it on the dashboard's "Projects & Settings" page) and set `projects[].path` to `/projects/<project name>`:

```yaml
projects:
  - name: my-app
    path: /projects/my-app   # maps to host ./projects/my-app (must be a git repo)
```

### Git push credentials

- **HTTPS**: embed a token in the remote URL, e.g. `https://<user>:<token>@github.com/user/repo.git` (via `git remote set-url` or the dashboard);
- **SSH**: mount `~/.ssh` (read-only) per the comments in `docker-compose.yml` and set `GIT_SSH_COMMAND`;
- Commit identity defaults to `Orcsy Agent <orcsy@localhost>`; override with the `ORCSY_GIT_NAME` / `ORCSY_GIT_EMAIL` env vars.

### One-shot runs (external scheduling)

Instead of a long-running container, trigger single rounds from external cron:

```bash
docker run --rm -v "$PWD/orcsy-data:/data" -v "$PWD/projects:/projects" \
  orcsy:latest run --once
```

### Extending the toolchain

The slim image contains only Python + git. If your managed projects need node/npm etc., stack them in a `Dockerfile.custom`:

```dockerfile
FROM orcsy:latest
USER root
RUN apt-get update && apt-get install -y --no-install-recommends nodejs npm \
    && rm -rf /var/lib/apt/lists/*
USER orcsy
```

```bash
docker build -f Dockerfile.custom -t orcsy:custom .
# change image to orcsy:custom in docker-compose.yml
```

## 🐧 Native Linux Deployment (systemd, no Docker)

Runs as a system-level systemd service: dedicated user, autostart on boot, automatic restart on crash. Layout:

| Path | Contents |
|---|---|
| `/opt/orcsy` | Program and virtualenv (`.venv`) |
| `/var/lib/orcsy` | Data directory: `config.yaml`, `skills/`, `logs/` (via `ORCSY_DATA_DIR`) |
| `/srv/orcsy-projects` | Managed git repos (`projects[].path` = `/srv/orcsy-projects/<project name>`) |

### Install

```bash
# After uploading/cloning the source, run from the repo root (needs root;
# installs uv, creates the user, installs and starts the service)
sudo bash deploy/linux/install.sh
```

Open `http://<server IP>:5116`, fill in `llm.api_key` on the "Projects & Settings" page, and create/import projects (path `/srv/orcsy-projects/<project name>`, must be a git repo).

### Service management & logs

```bash
systemctl status orcsy            # run status
sudo systemctl restart orcsy      # apply changes after editing /var/lib/orcsy/config.yaml
journalctl -u orcsy -f            # follow service logs (file logs at /var/lib/orcsy/logs/)
```

To run manually in the foreground instead (data directory defaults to `~/.orcsy`):

```bash
uv sync --no-dev && uv run orcsy web --host 0.0.0.0 --no-tray --no-browser
# or pure CLI for external cron: uv run orcsy run --host 0.0.0.0 --once
```

### Git push credentials

Same as Docker deployment (HTTPS token or SSH key); commit identity defaults to `Orcsy Agent <orcsy@localhost>`, overridable via `ORCSY_GIT_NAME` / `ORCSY_GIT_EMAIL` (export at install time, or edit `/var/lib/orcsy/.gitconfig` directly).

### Uninstall

```bash
sudo bash deploy/linux/uninstall.sh
# /var/lib/orcsy and /srv/orcsy-projects are kept by default; follow the script's
# output to remove them manually if desired
```

## 🧩 Core Concepts

### Task files

One Markdown file per task, placed in the project's `tasks/` directory; the filename is the task ID. YAML frontmatter holds metadata, the body holds the requirement/symptoms/acceptance criteria:

```markdown
---
title: Fix login page crash
priority: P1        # P0 highest, P4 lowest; lower number runs first
status: open        # open / in_progress / done / blocked / cancelled
labels: [bug, login]
assignee:           # empty = any Agent may claim it; a name restricts to that Agent
---

## Symptom
…

## Acceptance criteria
- [ ] …
```

- Each round the system scans tasks with `status: open`, sorts them **by priority P0 → P4** and executes in order;
- Status is set to `in_progress` before execution; afterwards it is written back as `done` / `blocked` with an "Agent result" section (summary, commit, changed files, blocking reason); `done` tasks are archived to `tasks/done/`;
- Format errors (missing title, invalid priority, etc.) are annotated as HTML comments at the top of the file, waiting for manual fixes.

### AGENT.md development guidelines

One `AGENT.md` per project root (rename via the `guide_file` option). It is injected into the system prompt when the Agent executes tasks, acting as the project's highest rule of conduct. Creating/importing a project from the dashboard auto-generates an English template with auto-detected Stack / Run / Test filled in; template at `orcsy/templates/AGENT.md`.

### Multiple Agents

- **One Agent per project**: every entry in the `projects` list in `config.yaml` is an independent Agent with its own guidelines, task list, worktree and conversation history;
- With `scheduler.max_parallel_agents > 1`, Agents of multiple projects run **in parallel** (thread pool);
- Within one project, `assignee` can route different tasks to different Agent names (paired with multiple configs in separate processes).

### LLM integration

The OpenAI-compatible protocol covers most vendors — just change `base_url` + `model`:

| Vendor | base_url | model example |
|---|---|---|
| DeepSeek | `https://api.deepseek.com/v1` | `deepseek-chat` |
| GLM (Zhipu) | `https://open.bigmodel.cn/api/paas/v4` | `glm-4-plus` |
| Kimi (Moonshot) | `https://api.moonshot.cn/v1` | `moonshot-v1-32k` |
| OpenAI | `https://api.openai.com/v1` | `gpt-4o` |
| Ollama (local) | `http://127.0.0.1:11434/v1` | `qwen2.5-coder:14b` |

The Anthropic protocol is supported too (`provider: anthropic`). "Enable model thinking on request" in settings makes Anthropic return thinking blocks (same for compatible services like Qwen/GLM); thinking content is written to run logs and expandable in the dashboard.

### Agent tool loop

While executing a task the Agent works through a set of tools **confined to the project**:

| Tool | Purpose |
|---|---|
| `list_dir` | List project file structure (auto-ignores .git/node_modules etc.) |
| `read_file` | Read a file (over-long content is truncated) |
| `write_file` | Create / wholesale-rewrite a file |
| `edit_file` | Exact text replacement (old_string must match uniquely) |
| `grep` | Regex code search |
| `run_command` | Run whitelisted commands (python/pytest/node/cargo/go/dotnet/git/uv etc.) for tests and builds |
| `finish` | Structured conclusion (completed / blocked / cannot_do + summary + commit message) |

Safety boundaries:

- File tools are confined to the project root (worktree); absolute paths and `../` traversal are always rejected;
- The command tool is whitelisted (customizable via `projects[].allowed_commands`); non-whitelisted commands are rejected outright;
- The tool loop has a turn limit (`projects[].max_agent_turns`, default 40) and is force-wrapped up on timeout;
- git commit/merge/push is executed by the system, the Agent cannot operate version control.

### Git integration

Managed projects must be git repositories. When executing a task:

1. A dedicated worktree is created (`.worktree-arranger/`, branch `arranger/<project name>`); all Agent changes happen inside the worktree, never polluting the main checkout;
2. After the Agent reports `completed`, the system commits in the worktree with the Agent's commit message, then merges back to the main branch with `--no-ff`; if an `origin` remote is configured it also pushes automatically;
3. If the Agent reports `blocked` / `cannot_do`, worktree changes are discarded, the task is marked `blocked` and awaits human handling;
4. On merge failure (conflicts etc.) the `arranger/<project name>` branch is kept for manual intervention and the task is marked `blocked`.

## 🖥 Web Dashboard

Start with `orcsy web`, then open [http://127.0.0.1:5116](http://127.0.0.1:5116):

- **Overview**: LLM connection info, scheduler status (idle/running, completed cycles, last scan, per-project counts this round);
- **Project cards**: git status, AGENT.md presence, task list (priority colors, status, description), format-error annotations, archived tasks; one-click open-project-folder and project removal (removes from config only, no files deleted);
- **Task management**: create/edit/delete tasks (written to `tasks/*.md`), requeue blocked tasks, mark done;
- **Manual trigger**: "Scan now" runs one scheduling round in the background (mutually exclusive with scheduled rounds, no double-claiming);
- **Run logs**: newest-first display, model thinking content collapsed by default and expandable; the page auto-refreshes every 5 seconds.

`[web] host / port / open_browser` options control the listen address, port and browser auto-open.

**Custom port** (highest to lowest priority): CLI `--port 8080` (address likewise `--host`) > `web.port` in `config.yaml`. On port conflicts:

- If the occupier is **another instance of this system** (`/api/state` reachable): prints that a dashboard is already running and exits, avoiding accidental duplicates;
- If the occupier is **another program** and the configured port was used: picks a free port automatically and says so on the console;
- **Explicit** `--port` occupied by another program: exits with an error (explicit choice is respected, never silently switched).

```bash
orcsy web --port 8080          # switch to this port on conflict
orcsy web --host 0.0.0.0       # allow LAN access (add auth yourself)
```

## ⚙️ Configuration Reference

See `config.example.yaml` at the repo root (kept identical to the `CONFIG_EXAMPLE` constant in `orcsy/__main__.py`, which the `init` command uses to generate `config.yaml`; after changing the constant, sync the template file — `tests/test_config_example.py` enforces this). For the installed edition the data directory defaults to `%APPDATA%\Orcsy`; the `ORCSY_DATA_DIR` env var relocates it entirely. Key options:

| Option | Description |
|---|---|
| `llm.provider` | `openai` (compatible protocol) or `anthropic` |
| `llm.base_url` / `llm.model` / `llm.api_key` | Model service URL, model name, API key |
| `llm.enable_thinking` | Request thinking on the API side (required for Anthropic; most compatible services return it by default) |
| `scheduler.interval_seconds` | Scan interval, default 300 s |
| `scheduler.max_parallel_agents` | Number of parallel Agents |
| `web.host / web.port / web.open_browser` | Dashboard listen address / port (default 5116) / auto-open browser |
| `logging.retention_days` | Log retention days (default 7, 0 disables): the active log is archived into `logs/archive/` after N days, and archived/size-rotated old logs beyond N days are cleaned; editable on the dashboard's "Scheduler & Dashboard" page, effective on save |
| `projects[].path` | Project root directory (absolute path recommended) |
| `projects[].tasks_dir` | Task list directory, default `tasks` |
| `projects[].guide_file` | Development guide file, default `AGENT.md` |
| `projects[].max_tasks_per_cycle` | Max tasks executed per round, default 1 |
| `projects[].max_agent_turns` | Tool-loop turn limit per task, default 40 |
| `projects[].allowed_commands` | Agent command whitelist; built-in default when omitted |
| `projects[].allowed_branch` | Work only on this branch; defaults to the current branch |
| `projects[].llm` | Per-project dedicated model binding: partial override of the global `llm` config (any of provider / base_url / api_key / model / temperature etc.; unset fields inherit) — only the bound model handles that project's tasks; editable on the dashboard's project dialog |
| `projects[].repo` | Remote repo binding: `{url, token?, provider?, api_base?}` (GitHub / Gitee / GitLab / Gitea; token optional for public repos); once bound, "Pull Issues" on the project card imports open Issues into the task queue |
| `update.repo` / `update.token` / `update.auto_check` | Self-update: Gitee source repo URL (Release assets carry both `orcsy-setup-*.exe` for first install and `orcsy-update-*.zip` for updates), private-repo token, auto-check on startup (default on); one-click check/update from the dashboard header, or manual package import in Settings (Windows installed edition) |

## 📊 Logging & Observability

- Console + `logs/orcsy.log` (rotating, 5MB × 5 files); every scheduling round, every task start/end, commit hashes and blocking reasons are logged;
- Task files double as reports: every executed task carries an "Agent result" section;
- The `validate` command confirms config, task format and guide file presence before anything runs.

## 🛠 Rebuild from Source

```bash
# One-click packaging (version -> frontend build -> PyInstaller dual exe -> Inno Setup installer + zip update package)
build.bat

# Publish a Gitee Release with BOTH assets at once (setup exe + zip update package;
# token via --token / GITEE_TOKEN env / config update.token):
uv run python build/release.py

# Version rule: feature-level changes bump the minor digit (0.13.0);
# functional patches bump the patch digit (0.12.3). Single source: orcsy/__init__.py

# Or step by step:
# 1. Build exes (needs dev dependencies: uv sync --dev; output dist/*.exe)
uv run pyinstaller orcsy.spec --noconfirm

# 2. Compile the installer (needs Inno Setup 6; output build/Setup/*.exe)
cd build && ISCC.exe installer.iss
```

## 📁 Project Structure

```
orcsy/
  __main__.py      CLI (init / validate / run / web)
  config.py        Config model & loading
  scheduler.py     Periodic scheduling (serial / parallel multi-Agent, thread-safe, web-triggerable)
  agent.py         Project Agent: scan → sort → claim → execute → write back
  executor.py      Single-task execution: worktree, Agent loop, commit & merge
  task_model.py    Task file parsing & priority sorting
  llm.py           LLM client (OpenAI-compatible / Anthropic) & Agent tool loop
  tools.py         Agent toolset (path confinement, command whitelist, finish protocol)
  prompts.py       System prompt construction (injects AGENT.md guide)
  probe.py         Static project tech-stack detection (auto-fills AGENT.md)
  templates.py     Built-in template reading & AGENT.md generation (single copy source)
  web.py           Web dashboard (HTTP server + JSON API + embedded single-page UI)
  git_ops.py       git operation wrappers
  tray.py          Tray residency & autostart
  logging_setup.py Logging
  templates/       AGENT.md and task templates
dashboard/         Dashboard frontend (Vue 3 + Naive UI, build output compiled into the exe)
build/
  build.py         One-click build pipeline (version single-source → PyInstaller → Inno Setup + zip update package)
  release.py       Publish a Gitee Release (uploads the setup exe and the zip update package together)
  installer.iss    Inno Setup installer script
  icon.ico/.png    App icons
  Setup/           Build output orcsy-setup-*.exe and orcsy-update-*.zip
tests/             End-to-end self-checks (mocked LLM / real HTTP)
orcsy.spec PyInstaller packaging config (output dist/*.exe)
Dockerfile         Container image (multi-stage uv build, slim git + Python base)
docker/            Container entrypoint script entrypoint.sh (git env initialization)
docker-compose.yml Batteries-included compose example (/data and /projects mounts)
deploy/linux/      Native Linux deployment (systemd unit + install/uninstall scripts)
```

## 🔒 Security & Boundaries

- `config.yaml` contains `api_key` and is already in `.gitignore` — never commit or share it; the installed edition keeps its data directory at `%APPDATA%\Orcsy` (config, skill library and logs all live here, local files unaffected by OneDrive), not in the program directory; configs from 0.8.x and earlier kept in the "Documents" folder are migrated automatically on first launch of a newer version (originals kept as backup);
- The web dashboard listens on `127.0.0.1` only by default and is not exposed to the LAN; for remote access change `web.host` yourself and add authentication yourself (no login feature in the current version);
- Agent changes are confined to managed projects; merge conflicts turn into blocked instead of force-pushing;
- Tasks marked `blocked` / `cannot_do` state their reason; after human confirmation, click "Requeue" on the dashboard (or set status back to open) to re-enter the queue;
- The default guidelines require: any change involving dependencies, CI or secrets must be blocked for human confirmation (adjustable in AGENT.md).

## 📄 License

[Apache-2.0](LICENSE)
