# Orcsy

English | [简体中文](README.zh-CN.md)

📖 **[User Guide (Wiki)](docs/user-guide.md)** — installation, features, and step-by-step usage for non-technical users.

> Orcsy is an Agent task-orchestration system that plugs into any LLM API. Write tasks as Markdown, and a scheduler scans each managed project's `tasks/` directory, dispatches them by priority (P0 → P4) to that project's dedicated Agent, the Agent reads and writes real code and runs tests inside an isolated git worktree, and on completion Orcsy commits, merges and writes the status back — an unattended, closed-loop development pipeline with a web dashboard throughout.

![version](https://img.shields.io/badge/version-0.23.0-blue) ![python](https://img.shields.io/badge/python-3.11%2B-green) ![platform](https://img.shields.io/badge/platform-Windows%20%7C%20Linux%20%7C%20Docker-lightgrey) ![license](https://img.shields.io/badge/license-Apache--2.0-orange)

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

- **Multi-Model Agent Project Team** — assemble **👑 Project Manager / ⚙️ Backend / 🎨 Frontend / 🧪 QA** crews for any project: the PM explores code read-only, decomposes DAG subtasks (`depends_on_refs`) or dispatches directly; developers code inside task-isolated Worktree sandboxes with upstream API contracts auto-injected into prompts; QA spins up local servers and browsers for end-to-end testing, files defects via `report_bug` and auto-regresses after fixes; downstream tasks chain within the same cycle (`chain_within_cycle`) — a full requirement closes out in one cycle.
- **🔍 Independent Review Agent (pre-merge gate)** — finished tasks land a WIP commit and are audited by a **different second model** (acceptance-criteria spot checks, optional test runs); only a pass merges and archives, failures return structured issues for automatic rework (round cap adjustable) — no "grading your own work".
- **🛡️ Data masking (secrets never leave the machine)** — API keys and database passwords the Agent reads are masked locally **before being sent to the model provider**: processing happens only in memory (no disk writes, never logged, mapping cleared when the task ends); high-confidence rules work out of the box (sk-/tp-/ghp_/JWT/PEM private keys/Bearer/URL tokens/connection-string passwords), the model sees `[REDACTED:api_key:…]` placeholders, written-back files are restored to the original values; key-value enhanced rules and custom regex are opt-in.
- **🌐 Local debugging & browser automation** — native Python CDP client for local Chrome / Edge (zero drivers): element snapshots, ref-addressed clicking/typing, screenshots, console/network listening; plus `dev_server_*` background services and `http_request` local API debugging (host allowlist enforced) — a complete web/API verification loop.
- **Any LLM** — OpenAI-compatible protocol (DeepSeek / GLM / Kimi / Qwen / Ollama / vLLM…) and the Anthropic protocol, both with native function calling; switch providers by changing `base_url` + `model`.
- **Markdown as tasks** — one `.md` file per task: frontmatter for metadata, body for requirements and acceptance criteria. Plain text, versionable, human-editable.
- **One dedicated Agent / Team per project** — its own `AGENT.md` guidelines, task list, worktree and conversation history; multi-Agent parallelism; per-project dedicated model binding.
- **🛠️ Sandboxed tools & 3-tier approvals** — file tools confined to the project worktree, 90+ default command whitelist (9 ecosystems, visual toggles in the dashboard), three permission modes (ask-on-change / autonomous / full-access with a pending-approval queue); missing toolchains are silently installed per the built-in `env-setup` skill (automatic mirror direct-download on proxy-less China networks).
- **Concurrent sub-agents (`delegate_explore`)** — PM and developer roles can spawn 2–4 **read-only sub-agents** in one call to explore different modules in parallel and get a consolidated report in a single round.
- **Skill library** — import local directories, create in the dashboard, or one-click scan user-level skills from ZCode / Claude Code / Codex etc. with batch import; prompts only receive a "name + description" index, full content is read on demand via `read_skill`, never bloating the context.
- **Repo Issues integration** — bind a GitHub / Gitee / GitLab / Gitea repo (self-hosted included) and pull open Issues into the task queue in one click (label-mapped priorities, archive-aware dedup, one-way import).
- **Web dashboard** — status overview, task CRUD and drag-and-drop sorting, a **dedicated human-gated commit page** (file list + diff, confirm commit/push), model connectivity test, run logs with collapsible model thinking; tray icon, autostart, self-update (zero-UAC / one prompt under system dirs), task-end alerts, per-role usage ledger with a 12-week heatmap.
- **Zero-friction project onboarding** — creating/importing a project only adds `tasks/` and `AGENT.md`; tech stack auto-detection pre-fills run and test commands.

## 🚀 Quick Start

### Option 1: Windows installer (recommended)

1. Run `build/Setup/orcsy-setup-<version>-windows-amd64.exe` and follow the wizard;
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

> ⚠️ **The container listens on `--host 0.0.0.0`, so a login password is required.** The first start generates a `/data/config.yaml` template, then refuses to run because of "non-loopback bind with an empty `web.auth_password`" (an actionable message appears in the logs). Edit `./orcsy-data/config.yaml`, set `web.auth_password` to a strong password, and run `docker compose restart`; you can change it from the Settings page afterwards.

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

The service listens on `--host 0.0.0.0`, so the installer generates `/var/lib/orcsy/config.yaml` with a random login password on first install (printed in the install output; override with the `ORCSY_AUTH_PASSWORD` env var). Existing config files are left untouched — if their `web.host` is non-loopback with an empty `web.auth_password`, the service refuses to start; add a password and `systemctl restart orcsy`.

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

## 🔐 Dashboard Login Auth (required for external deployments)

The dashboard binds to `127.0.0.1` with no auth by default (zero friction for local use). Exposing it via Docker / systemd / `--host 0.0.0.0` **requires** a login password — with a non-loopback host and an empty password the program refuses to start and the dashboard refuses to save that config (the dashboard can browse server directories, write tasks and start/stop agents). Secrets (LLM API key, update token, project repo tokens) are only ever returned masked by `/api/state`; enter a new value to replace one, leave the field empty to keep the stored value:

```yaml
web:
  host: 0.0.0.0
  port: 5116
  auth_password: "your-strong-password"   # non-empty enables auth; empty = off
```

You can also set/change/clear the password while logged in, under "Settings → Application" (takes effect on save).

Behavior:

- When enabled, every page and API except the login screen requires a session; wrong passwords incur a delay penalty (brute-force mitigation); the session cookie (HttpOnly, 7 days) lives in memory, so a program restart requires logging in again; changing the password invalidates all existing sessions immediately.
- The password is stored securely as a **PBKDF2-HMAC-SHA256 (100,000 iterations)** ciphertext; if forgotten, run `orcsy reset-password` on the server (or `--clear` to remove).
- Auth is not a substitute for TLS: over plain HTTP the password and session can be sniffed. For public deployments, put a reverse proxy (nginx/caddy) with HTTPS in front.

### Data masking (secrets never leave the machine)

Agents read workspace files and run commands — API keys and database passwords found there are masked locally **before being sent to the model provider**: processing happens only in memory (no disk writes, never logged, mapping cleared when the task ends); high-confidence rules are on by default (sk-/tp-/ghp_/JWT/PEM private keys/Bearer/URL tokens/connection-string passwords, etc.), the model sees `[REDACTED:api_key:xxxx]` placeholders, written-back files are restored to the original values before hitting disk, and finish summaries stay masked (keys never land in task files). Key-value enhanced rules (`api_key=xxx`) and custom regex are configurable in Settings → 🛡 Data masking or the `masking` section of config.yaml.

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
| `list_dir` / `find_files` / `grep` | List structure / find by glob / regex search (auto-ignores .git/node_modules etc.) |
| `read_file` / `write_file` / `edit_file` / `multi_edit` | Read (truncated when over-long) / write / exact replacement (unique `old_string`) / multi-replacement in one file (all-or-nothing) |
| `delete_file` / `move_file` | Delete (directories require `recursive: true`) / move-rename (existing target requires `overwrite: true`) |
| `run_command` | Run whitelisted commands (python/node/git/package managers etc.) for tests and builds; missing toolchains are installed per the `env-setup` skill; freshly installed tools are usable immediately |
| `web_fetch` | Fetch the text content of a public URL (intranet/loopback/cloud-metadata targets always rejected) |
| `browser_*` / `dev_server_*` / `http_request` | Local debugging trio (see Features), off by default, opt-in |
| `read_skill` | Read the full body of one skill |
| `finish` | Structured conclusion (completed / blocked / cannot_do + summary + commit message) |

Safety boundaries:

- File tools are confined to the project root (worktree); absolute paths and `../` traversal are always rejected;
- The command tool is whitelisted (`projects[].allowed_commands` fully replaces the default); non-whitelisted commands are rejected outright;
- `web_fetch` is the only networked tool: http/https only, intranet targets always rejected;
- The tool loop has a turn limit (`projects[].max_agent_turns`, default 40; `-1` = unlimited with loop guards still active);
- git commit/merge/push is executed by the system, the Agent cannot operate version control.

Every tool except `finish` can be toggled by name or group — global defaults plus per-project overrides, visually editable in the dashboard, effective on save (two-layer enforcement: schema filtering + dispatch backstop). The local-debug group (browser / dev_server / http_request) is off by default.

### Permission modes (human approval)

| Mode | Semantics |
|---|---|
| `ask_on_change` | Ask before changes: all write/edit/delete/move/run operations pause for approval |
| `autonomous` | Autonomous: only high-risk operations (delete/move/run commands) ask |
| `full_access` | Full access (default, never asks) |

Read-only tools and finish never ask; paused operations have zero side effects, and the dashboard's "🔔 Pending approval" queue approves/rejects each request (with an "always allow within this task" checkbox); timeouts auto-reject and feed back to the model as error text. Global default plus `projects[].permission_mode` per-project override; `permissions.timeout_minutes` controls the approval timeout.

### Git integration

Managed projects must be git repositories. All Agent changes happen inside a dedicated worktree (`.worktree-arranger/`, branch `arranger/<project name>`); after the Agent reports `completed`, the system commits with its message and merges back with `--no-ff` (auto-push if `origin` is configured); on `blocked` / `cannot_do` or merge conflicts, the task is marked `blocked` for human handling.

## 🖥 Web Dashboard

Start with `orcsy web`, then open [http://127.0.0.1:5116](http://127.0.0.1:5116): status overview, project cards, task CRUD and drag-and-drop sorting, manual "scan now" trigger, force-stopping running tasks, the usage ledger and heatmap, and run logs (model thinking collapsible); the page auto-refreshes every 5 seconds.

Custom port/address: CLI `--port / --host` > `web.port / web.host` in `config.yaml`. When the configured port is taken by another program and not explicitly set, a free port is picked automatically with a console notice; an explicitly set port that is occupied exits with an error.

```bash
orcsy web --port 8080          # switch to this port on conflict
orcsy web --host 0.0.0.0       # allow LAN access (set a login password)
```

## ⚙️ Configuration Reference

See `config.example.yaml` at the repo root for the full annotated config (kept identical to the `CONFIG_EXAMPLE` constant in `orcsy/__main__.py`). The installed edition's data directory defaults to `%APPDATA%\Orcsy`; the `ORCSY_DATA_DIR` env var relocates it entirely. Key options:

| Option | Description |
|---|---|
| `llm.*` | provider (`openai` compatible / `anthropic`), base_url / model / api_key, enable_thinking (request-side model thinking) |
| `scheduler.interval_seconds` / `max_parallel_agents` | Scan interval (default 300 s) / number of parallel Agents |
| `web.host / port / open_browser` | Dashboard listen address / port (default 5116) / auto-open browser |
| `logging.retention_days` / `max_total_mb` | Log retention days (default 7) / total size cap MB (default 200); whichever hits first cleans from the oldest |
| `tools` / `commands` | Global tool toggles and command-whitelist toggles (`{name: bool}`; unlisted stay enabled/allowed; `finish` cannot be disabled) |
| `env.vars` | Env vars injected into Agent subprocesses: configure `GOPROXY` / `HTTP_PROXY` / `PIP_INDEX_URL` etc. when dependency downloads fail in mainland China (overriding PATH and other system vars is rejected) |
| `permissions.mode` / `timeout_minutes` | Permission mode (ask_on_change / autonomous / full_access) / approval timeout in minutes (default 30) |
| `review.*` | Task review (Review Agent): `llm` binds the review model (**must differ from the task model**, default `~` = review off), `max_turns` review rounds, `max_review_rounds` rework limit (default 2), `allow_test_commands` real test runs, `squash_merge` collapse rework commits; per-project `review_enabled` / `review_llm` / `review_max_rounds` override |
| `masking.*` | Data masking: `enabled` (default on) / `enhanced_rules` key-value rules (default off) / `custom_patterns` custom regex — see the data-masking section under Dashboard Login Auth |
| `notify.sound` / `system` | Task-end alerts: bundled sound / tray-balloon notification (both on by default; manual stops stay silent) |
| `projects[].path` / `tasks_dir` / `guide_file` | Project root (absolute recommended) / task dir (default `tasks`) / guide file (default `AGENT.md`) |
| `projects[].max_tasks_per_cycle` / `max_agent_turns` | Tasks per round (default 1) / per-task turn cap (default 40; **-1 = unlimited**) |
| `projects[].llm` | Per-project dedicated model: partial override of the global `llm` (unset fields inherit) — only the bound model handles that project's tasks |
| `projects[].repo` | Remote repo binding `{url, token?, provider?, api_base?}`; enables one-click Issue import |
| `projects[].tools` / `allowed_commands` / `permission_mode` | Per-project tool toggles / command whitelist (explicit list fully replaces the default) / permission mode; unset = follow global |
| `projects[].allowed_branch` / `team` | Work only on this branch / multi-model team composition (referencing the global `agent_profiles` pool) |
| `update.*` | Self-update source repo (official Gitee source by default) / private token / auto-check on startup |
| frontmatter `order` | Drag-and-drop sequence (breaks ties within a priority for display and claiming; delete the line to return to automatic ordering) |
| frontmatter `requeue_seq` / `review_pending` / `review_round` | Auto-maintained, never hand-edited: requeue priority / pending-review state / cumulative rework rounds |

## 📊 Logging & Observability

- Console + `logs/orcsy.log` (rotating, 5MB × 5 files); every scheduling round, every task start/end, commit hashes and blocking reasons are logged;
- Task files double as reports: every executed task carries an "Agent result" section;
- The `validate` command confirms config, task format and guide file presence before anything runs.

## 🛠 Rebuild from Source

```bash
# One-click packaging (version -> frontend build -> PyInstaller dual exe -> Inno Setup installer + zip update package)
build.bat

# Publish a Gitee Release with ALL THREE assets at once (setup exe + zip update
# package + its .sha256 checksum; online update refuses to install without it):
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

- `config.yaml` contains `api_key` and is already in `.gitignore` — never commit or share it; the installed edition keeps its data directory at `%APPDATA%\Orcsy` (config, skill library and logs all live here), not in the program directory;
- The dashboard listens on `127.0.0.1` only by default; exposing it externally **requires** a login password (see Dashboard Login Auth), and secrets are only ever returned masked by the API;
- Sensitive data is masked locally before leaving the machine (see Data masking); file tools are confined to the project, commands are whitelisted, and `web_fetch` rejects intranet targets;
- Agent changes are confined to managed projects; merge conflicts turn into blocked instead of force-pushing; the final `git commit` / `push` is always human-gated;
- Tasks marked `blocked` / `cannot_do` state their reason; after human confirmation, click "Requeue" on the dashboard to re-enter the queue;
- The default guidelines require: any change involving dependencies, CI or secrets must be blocked for human confirmation (adjustable in AGENT.md).

## 📄 License

[Apache-2.0](LICENSE)
