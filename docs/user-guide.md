# Orcsy User Guide

English | [简体中文](用户指南.md)

> For first-time, non-technical users: install Orcsy, connect a project, create tasks, and understand every part of the dashboard — without needing to write code.
>
> Version: **0.21.0**　|　Default dashboard: **http://127.0.0.1:5116**
>
> ℹ️ The dashboard is currently Chinese-only. This guide uses English names for buttons and cards; a Chinese-label reference is in [section 9.1](#91-top-toolbar).

---

## Contents

1. [Quick start: your first task in 5 minutes](#1-quick-start-your-first-task-in-5-minutes)
2. [What Orcsy is and what it can do for you](#2-what-orcsy-is-and-what-it-can-do-for-you)
3. [Three core ideas in plain English](#3-three-core-ideas-in-plain-english)
4. [Installation: choose the path that fits you](#4-installation-choose-the-path-that-fits-you)
5. [First launch: connect your LLM](#5-first-launch-connect-your-llm)
6. [Add your first project](#6-add-your-first-project)
7. [Create and manage tasks](#7-create-and-manage-tasks)
8. [Let Orcsy start working](#8-let-orcsy-start-working)
9. [Dashboard map: what every button does](#9-dashboard-map-what-every-button-does)
10. [Advanced features: approvals, skills, Issues, mirrors](#10-advanced-features-approvals-skills-issues-mirrors)
11. [Data, backup, and migration](#11-data-backup-and-migration)
12. [FAQ and troubleshooting](#12-faq-and-troubleshooting)
13. [Glossary](#13-glossary)
14. [Appendix: task template and command reference](#14-appendix-task-template-and-command-reference)

---

## 1. Quick start: your first task in 5 minutes

If Orcsy is already installed and you just want to try it, do these five things:

1. **Open the dashboard**: double-click the Orcsy desktop shortcut, or visit <http://127.0.0.1:5116>.
2. **Connect a model**: click **Projects & Settings** in the top bar → fill in API Key, API base URL, and model under **🤖 LLM Access** → click **Test connection** → on success click **Save model settings**.
3. **Add a project**: click **＋ New project** at the top right → choose a project folder on your computer → click **Save**. Orcsy creates `tasks/` and `AGENT.md` inside it and leaves the rest of your code alone.
4. **Add a task**: go back to **Dashboard**, type a one-line task title in the project card (for example "Fix the login page at 125% zoom"), choose a priority, and click **Add**.
5. **Let it run**: click **▶ Scan now** in the top bar. The task moves from **Open** to **In progress**, then to **Done** or **Blocked**. If the project is a Git repository, you will also get a Git commit when it succeeds.

> 💡 For your first run, use a **test project** or a **copy**. Orcsy really edits files and runs commands. In a Git repository, edits happen in a temporary work copy first and are merged back only on success; in a non-Git project, edits happen directly in the project folder.

---

## 2. What Orcsy is and what it can do for you

### 2.1 One sentence

**Orcsy is a tool that turns a written task into work done for you.** You describe what you want in a Markdown file. Orcsy periodically checks the task list, hands the task to an AI agent, and lets it read code, edit files, and run tests inside your project. When it finishes, Orcsy commits the changes to Git and updates the task status.

A simple way to picture the roles:

- **You** are the product owner: you write down what to do and what "done" means.
- **Orcsy** is the project manager: it prioritizes, schedules, and tracks progress.
- **The LLM** is the engineer: it actually reads and changes the code.
- **The dashboard** is the office display: you can always see what is running and where it stands.

### 2.2 What it is good at

- Development tasks with clear acceptance criteria: bug fixes, small features, tests, copy changes, small refactors;
- Repetitive maintenance: run tests after a dependency bump, apply a config change in several places, generate documentation;
- Turning GitHub / Gitee / GitLab / Gitea Issues into development tasks automatically;
- Queuing work across several projects, or running several projects in parallel;
- Making progress overnight or over the weekend without someone watching.

### 2.3 What it is not good at

- Vague requests where even you cannot say what "done" looks like;
- Tasks that need many product decisions, visual taste, or major database schema changes;
- Work that needs production databases, third-party admin consoles, or secret managers you have not given it access to;
- "One-click deploy to production" or app-store releases. Orcsy changes code and commits it; it does not deploy your product.

### 2.4 Do I need to know how to code?

**You do not need to write code, but you should be able to:**

- install Windows software and unzip files;
- sign up for an LLM API key on a provider's website;
- describe a task in a sentence or two, ideally with acceptance criteria;
- find your project folder on disk;
- read a Chinese/English message in the run log when something goes wrong.

If you do not want to touch a command line at all, **use the Windows installer**. Everything else can be done by clicking in the browser.

---

## 3. Three core ideas in plain English

### 3.1 Project

A **project** is one code folder, for example `D:\Repos\my-app`.

Orcsy only does two things inside it:

1. Creates a `tasks/` folder if it does not exist, to hold tasks;
2. Creates an `AGENT.md` file if it does not exist, to tell the AI the rules of this project.

It does **not** rearrange the rest of your repository, and it never writes configuration, logs, or skills into the project folder.

### 3.2 Task

A **task** is one Markdown file (`.md`) inside the project's `tasks/` folder.

The file name is the task ID, for example `P1-fix-login-crash.md`. The top of the file holds metadata (title, priority, status); the body holds the request, symptoms, and acceptance criteria.

You can:

- create a task by clicking in the dashboard;
- drop a `.md` file into `tasks/`;
- edit the file in any text editor.

All three are equivalent, because the dashboard edits those same files.

### 3.3 Agent

Each project has its own dedicated Agent. It is not a robot that sits around all day; it is an executor that wakes up when there is a task.

When it works, it:

1. reads the project's `AGENT.md` to learn the stack, run commands, and coding rules;
2. reads the task file to understand the request;
3. in a Git repository, creates a **temporary work copy** (a Git worktree); every edit happens there first (non-Git projects are edited in place);
4. reads files, edits files, runs tests and builds;
5. reports the result; Orcsy then commits and merges back to the main branch;
6. writes the result back into the task file and archives completed tasks under `tasks/done/`.

> 💡 Why a temporary copy? Because AI makes mistakes too. In a Git project, edits stay in the copy until the task succeeds. If the task is blocked, the copy is discarded and your main working tree stays clean. Non-Git projects do not have this safety layer, so back up or practice on a copy first.

---

## 4. Installation: choose the path that fits you

### 4.1 Pick a method

| Your situation | Recommended method | Difficulty | What you need |
|---|---|---|---|
| Windows PC, want the easiest path | **Windows installer** | ★☆☆ | An API key |
| Want to read or modify the source | **Run from source** | ★★★ | Some command-line comfort, `uv` |
| Server or NAS, do not want a local runtime | **Docker Compose** | ★★☆ | Basic Docker knowledge |
| Linux server, want a boot-time service | **Linux systemd** | ★★★ | root access, some Linux comfort |

> For most non-technical users, the **Windows installer** is enough.

### 4.2 Option 1: Windows installer (recommended)

#### Before you start

- Windows 10 / 11;
- an LLM API key (DeepSeek, Zhipu GLM, Kimi, OpenAI, Qwen, etc.);
- a project folder you want Orcsy to work on;
- ideally the project is a **Git repository** (not required, but then you lose automatic commit/merge).

#### Steps

1. Download `orcsy-setup-0.21.0-windows-amd64.exe` from the release page:
   - Gitee: <https://gitee.com/java-and-net/Orcsy/releases>
   - GitHub: <https://github.com/easy-open-meta/Orcsy/releases>
2. Double-click it. If Windows SmartScreen appears, click **More info** → **Run anyway**.
3. Follow the wizard. You can keep the default location `C:\Program Files\Orcsy`, or choose a **user-writable folder** such as `D:\Tools\Orcsy`.
   - Under `C:\Program Files`: future auto-updates will ask for administrator permission (UAC) once; click **Yes**.
   - In a user-writable folder: future updates run silently with no prompt.
4. Under optional tasks you may check **Initialize sample tasks**. This only places a sample template in the data directory; it is optional.
5. Click **Install**. When it finishes, Orcsy starts automatically:
   - an Orcsy tray icon appears near the clock;
   - your browser opens the dashboard at <http://127.0.0.1:5116>.
6. If the browser does not open: right-click the tray icon → **Show dashboard**, or open the address manually.

#### Three things to know after installation

- **Data directory**: `C:\Users\<you>\AppData\Roaming\Orcsy`
  - `config.yaml`: configuration (contains your API key — **do not share it**);
  - `skills\`: skill library;
  - `logs\`: run logs;
  - `logs\usage.jsonl`: usage ledger.
- **Program directory**: program files only; your data is not stored there.
- **Uninstall**: Windows **Settings → Apps → Installed apps** → Orcsy. You can choose whether to delete the data directory.

> ⚠️ `config.yaml` is generated automatically on first launch. You do **not** need to create it by hand. Filling in the model settings in the dashboard is the easiest path.

### 4.3 Option 2: run from source (for people who want the source)

Prerequisite: [uv](https://docs.astral.sh/uv/) is installed. You do not need Python preinstalled; `uv` prepares Python 3.11+ for you.

```bash
# 1. Get the source
git clone https://gitee.com/java-and-net/Orcsy.git orcsy
cd orcsy

# 2. Install dependencies
uv sync

# 3. Generate config and templates under run/
uv run orcsy init --dir run

# 4. Edit run/config.yaml: at least api_key, base_url, model
#    Then point projects at your own project (absolute path)

# 5. Validate config and task files
uv run orcsy validate --config run/config.yaml

# 6. Start the dashboard (also starts the scheduler and opens the browser)
uv run orcsy web --config run/config.yaml
```

Useful flags:

| Flag | Effect |
|---|---|
| `--port 8080` | Use another dashboard port |
| `--host 0.0.0.0` | Allow LAN access (**you must set a login password**) |
| `--no-browser` | Do not open the browser automatically |
| `--no-tray` | Do not show the tray icon |
| `--no-sched` | Do not run the scheduler; only manual **Scan now** |

If you only want a command-line scheduler without the dashboard:

```bash
uv run orcsy run --config run/config.yaml          # keep scheduling
uv run orcsy run --config run/config.yaml --once   # run one cycle and exit (Task Scheduler / cron)
```

### 4.4 Option 3: Docker (server / NAS)

Prerequisites: Docker and Docker Compose are installed.

```bash
# 1. Build and start from the repository root
docker compose up -d --build

# 2. First start generates ./orcsy-data/config.yaml
#    The container listens on 0.0.0.0, so a login password is mandatory;
#    otherwise startup is refused. Edit web.auth_password, then:
docker compose restart

# 3. Open the dashboard
#    http://<server-ip>:5116
```

Volume mapping:

| Host | Container | Contents |
|---|---|---|
| `./orcsy-data` | `/data` | config, skills, logs, usage ledger |
| `./projects` | `/projects` | managed projects (one subfolder per project) |

Important rules:

- When creating a project in the dashboard, use the **container path**, for example `/projects/my-app`, which maps to `./projects/my-app` on the host;
- `./projects/my-app` on the host must be a Git repository;
- To let Orcsy push to a remote, configure Git credentials:
  - HTTPS: put a token in the remote URL;
  - SSH: mount `~/.ssh` as described in `docker-compose.yml`.
- Logs: `docker compose logs -f`;
- Stop: `docker compose down`;
- Upgrade: back up `./orcsy-data`, pull the new source, then `docker compose up -d --build`.

### 4.5 Option 4: Linux systemd service

Prerequisites: Linux server, root access, `git` installed.

```bash
# Run from the repository root (as root)
sudo bash deploy/linux/install.sh

# The installer will:
# - install uv if needed
# - create a dedicated orcsy user
# - install the program under /opt/orcsy
# - put data under /var/lib/orcsy
# - use /srv/orcsy-projects for managed projects
# - generate a random login password and print it
```

After installation:

1. Open `http://<server-ip>:5116` in a browser;
2. Log in with the password printed by the installer;
3. Fill in the model settings under **Projects & Settings**; when creating a project, use `/srv/orcsy-projects/<name>` as the path.

Service commands:

```bash
systemctl status orcsy          # status
sudo systemctl restart orcsy    # restart after editing /var/lib/orcsy/config.yaml
journalctl -u orcsy -f          # follow logs
sudo bash deploy/linux/uninstall.sh   # uninstall (data directory is kept by default)
```

If the server has a firewall, allow port 5116 (or your custom port).

### 4.6 Post-install checklist

- [ ] The dashboard opens at `http://127.0.0.1:5116`;
- [ ] The version and model name appear in the top bar;
- [ ] Model settings are saved and **Test connection** succeeds;
- [ ] At least one project is listed, with a `git` or `non-git` badge;
- [ ] No red **Missing guide** badge on the project card;
- [ ] The **Run log** section is visible;
- [ ] You receive a sound/system notification when a task finishes (if enabled).

---

## 5. First launch: connect your LLM

Orcsy does not include an AI model. It needs an external LLM service. You provide the endpoint, model name, and key.

### 5.1 Open Settings

Click **Projects & Settings** in the top bar, then find the first card: **🤖 LLM Access**.

### 5.2 Fill in the fields

| Field | What to enter | Notes |
|---|---|---|
| provider | Most services: `openai (compatible)`; Claude or Anthropic-compatible services: `anthropic` | Wrong choice = connection failure |
| model | The model name from your provider, e.g. `deepseek-chat`, `glm-4-plus`, `moonshot-v1-32k` | You can click **Fetch list** and pick from the dropdown |
| base_url | The API endpoint from your provider, usually ending in `/v1` | See the table below |
| API Key | The key generated in the provider's console, usually starting with `sk-` | Only a masked value is shown; plaintext is never echoed back |
| temperature | Default `0.2` is fine | Higher = more creative; lower is better for code |
| max_tokens | Default `8192` is fine | Auto-adjusted to the model limit after fetching the list |
| max_context_tokens | Default `131072` is fine | Same as above |
| timeout_seconds | Default `300` seconds | Use `600`–`900` for long/thinking models; `-1` means no timeout |
| Enable model thinking | Off by default | Only turn on if you want to see the thinking process; unsupported models may fail |

### 5.3 Common providers

| Provider | Protocol | base_url | Example model |
|---|---|---|---|
| DeepSeek | openai | `https://api.deepseek.com/v1` | `deepseek-chat` |
| Zhipu GLM | openai | `https://open.bigmodel.cn/api/paas/v4` | `glm-4-plus` |
| Kimi (Moonshot) | openai | `https://api.moonshot.cn/v1` | `moonshot-v1-32k` |
| Qwen (DashScope) | openai | See your Alibaba Cloud console (usually includes `/compatible-mode/v1`) | `qwen-plus` |
| OpenAI | openai | `https://api.openai.com/v1` | `gpt-4o` |
| Ollama (local) | openai | `http://127.0.0.1:11434/v1` | `qwen2.5-coder:14b` |
| Anthropic Claude | anthropic | `https://api.anthropic.com` | `claude-sonnet-4-...` |

> 💡 If you are not sure about the model name, click **Fetch list** first. If the provider does not expose a model list (404), type the model name manually.

### 5.4 What the two buttons do

- **Fetch list**: asks the provider for available models. On success, the model field becomes a dropdown and the context window / max output are filled in automatically.
- **Test connection**: sends a tiny chat request to verify network + key + model name. **Click it after every change.**

### 5.5 Save

Click **Save model settings**. The top bar will show the current model, for example:

```
v0.18.0 · model deepseek-chat (openai) · config C:\Users\you\AppData\Roaming\Orcsy\config.yaml
```

If it says **api_key not configured**, the key was not saved. Go back and check.

---

## 6. Add your first project

### 6.1 Create a project

Click **＋ New project** at the top right (or the button of the same name under **📁 Managed projects** in Settings).

Fill in, in order:

1. **Project name**: use letters, digits, and hyphens (underscores are also allowed), e.g. `my-app`; no spaces or special symbols, and it cannot start with a symbol. It appears in logs and branch names.
2. **Max tasks per cycle**: default `1`. This is how many tasks this project may run per scan. Keep 1 at first; raise it after things are stable.
3. **Project path**: click **Choose…** and pick your project folder, or paste an absolute path such as `D:\Repos\my-app`.
4. **Tasks directory**: default `tasks`; keep it.
5. **Guide file**: default `AGENT.md`; keep it.
6. **Max agent turns**: default `40`. The maximum number of "think → act" rounds per task. `-1` means unlimited (not recommended for beginners).
7. **Working branch**: optional. Leave blank to use the project's current branch.
8. **Command whitelist**: optional. Leave blank to use the default whitelist (python, node, npm, git, dotnet, etc.).
9. **Bind a dedicated model**: optional. When enabled, this project can use its own model; blank fields inherit the global config.
10. **Permission mode**: default **Follow global**. Choose **Ask before changes** if you want more safety.
11. **Override Agent tool switches**: optional. Follow global by default; leave it alone unless needed.
12. **Bind remote repository Issues**: optional. Turn on if you want GitHub/Gitee/GitLab/Gitea Issues to become tasks automatically.
13. **Stack / Run / Test**: Orcsy auto-detects these after you enter the path. You can edit them. They are written into `AGENT.md` to save the AI time.
14. **Enable this project**: keep it on.

Click **Save**. You will see:

- the project card on the **Dashboard**;
- a `tasks/` folder and `AGENT.md` in the project (if they did not exist);
- the project path, task counts, and a `git` or `non-git` badge on the card.

### 6.2 What the badges mean

| Badge | Meaning |
|---|---|
| `git` | The project is a Git repository. Successful tasks are committed and merged; if a remote is configured, pushed |
| `non-git` | Not a Git repository. The Agent edits files directly, with no automatic commit |
| `Missing guide` | No `AGENT.md`; click **AGENT.md** to create one |
| `Auto pull Nmin` | Scheduled Issue pulling is on, every N minutes |
| Switch | Enable/disable the project. Disabled projects are skipped; config and files are kept |

### 6.3 What AGENT.md is and why it matters

`AGENT.md` is the project's **employee handbook**. The AI reads it before every task.

It usually contains:

- the tech stack (language, framework);
- how to run the project locally;
- how to run tests;
- coding style and conventions;
- things that must never be done;
- when to stop and ask for help (`blocked`).

When you create a project, Orcsy generates an English template and fills in Stack / Run / Test from detection. Click **AGENT.md** on the project card to edit it; you can translate it or add project rules.

> 💡 The highest-leverage thing you can do for output quality is to make `AGENT.md` specific: test commands, coding rules, folders not to touch, and the definition of done.

### 6.4 Optional project settings

Click **Settings** on a project card to change:

- name, path, tasks directory, guide file;
- max tasks per cycle, max turns, working branch, command whitelist;
- dedicated model;
- permission mode;
- tool overrides;
- remote repository binding and auto-pull interval;
- enabled/disabled.

Renaming a project also updates skills bound to that project, so they do not become orphaned.

---

## 7. Create and manage tasks

### 7.1 Fastest way: quick add on the card

On the **Dashboard**, inside a project card:

1. type a one-line title in **New task title, press Enter to add**;
2. choose a priority (default `P2`);
3. click **Add** or press Enter.

The task ID is generated automatically as `priority-title`, for example `P2-fix-login-layout`. Duplicates get `-2`, `-3`.

### 7.2 Full way: the New task dialog

Click **New task** under the project card. You can fill in:

| Field | Meaning |
|---|---|
| Task ID | Leave blank to auto-generate; or use a short English name such as `fix-login-timeout` |
| Priority | `P0` highest, `P4` lowest |
| Title | One sentence describing the work |
| Labels | Comma-separated, e.g. `bug, ui`; used for grouping and display only |
| Assignee | Leave blank to let any Agent take it; enter a project name to restrict it |
| Description | Markdown body: symptoms, reproduction steps, acceptance criteria |

Click **Save**. The task enters `open` (to-do) status.

> ⚠️ Only **open** tasks are directly editable. In-progress, blocked, and completed tasks open read-only. To change one, click **Requeue** first.

### 7.3 How to choose a priority

| Priority | When to use | Examples |
|---|---|---|
| `P0` | Production incident, blocks everyone | Payment API returns 500, app crashes on start |
| `P1` | Important feature broken, or must ship this week | Login fails, core flow errors |
| `P2` | Normal request or ordinary bug (default) | Add a filter, fix a layout issue |
| `P3` | Can be scheduled, not urgent | Add docs, improve log messages |
| `P4` | Whenever there is time | Code cleanup, polish |

Ordering: **P0 → P4 first; within the same priority, the order you drag them into**. A blocked task that is requeued is picked up first on the next scan.

### 7.4 What a task file looks like

Open `tasks/P2-fix-login-layout.md` in a text editor:

```markdown
---
title: Fix login layout
priority: P2
status: open
labels: [bug, ui]
assignee:
---

## Symptoms

At 125% scaling, the login button overflows the card on the right.

## Steps to reproduce

1. Set Windows display scaling to 125%;
2. Open the login page;
3. Observe the button position.

## Acceptance criteria

- [ ] Layout is correct at 100% / 125% / 150%
- [ ] The existing login flow is unchanged
```

You can edit this file directly; the dashboard picks up the change on its next refresh.

> 💡 Do not put secrets, passwords, or internal addresses in task bodies. Task files live in the Git repository and may be committed.

### 7.5 Task lifecycle

```
open (to-do)
  │  claimed by the Agent
  ▼
in_progress (running)
  │
  ├── success ──▶ done ──▶ moved to tasks/done/
  ├── blocked ──▶ blocked ──▶ waiting for you; can be requeued
  └── manual stop ──▶ cancelled ──▶ set `status` back to `open` in the task file to rerun
```

| Status | Dashboard label | What you can do |
|---|---|---|
| `open` | Open | Edit, delete, drag to reorder |
| `in_progress` | In progress | View, force stop, mark done (not recommended) |
| `done` | Done | View; archived automatically |
| `blocked` | Blocked | View reason, requeue, delete |
| `cancelled` | Cancelled | View, delete; to rerun, set `status` back to `open` in the task file (or copy it into a new task) |

### 7.6 Actions on the task list

| Action | Where | Effect |
|---|---|---|
| Click a task row | Task list | Open the editor for open tasks; read-only view otherwise |
| Drag a task row | Task list | Reorder within the same priority; also determines the Agent's pickup order |
| Checkbox | Left of the task | Select for batch actions |
| Select page | Above the list | Select the 10 tasks on the current page |
| Batch delete | Appears after selection | Delete the selected task files; irreversible |
| Clear tasks (N) | Above the list | Delete all active tasks in the project; archived tasks are kept |
| Requeue | Blocked/in-progress task | Back to open; picked up first next scan |
| Mark done | Open/in-progress task | Mark complete manually (no auto-archive) |
| Force stop | In-progress task | Abort the AI loop and running commands, discard temporary changes, mark cancelled |
| Delete | Each task row | Delete one task file |

> ⚠️ **Delete**, **Clear**, and **Force stop** cannot be undone. Confirm the content before deleting a task; for Git projects, force stop discards this task's temporary changes, while non-Git projects keep whatever was already changed.

### 7.7 Archived tasks

Completed tasks are moved to `tasks/done/`. Expand **Archived (N) · click for details** at the bottom of the project card to view them; clicking a row opens a read-only view.

### 7.8 Five tips for writing good tasks

1. **Describe the current state**: error messages, screenshot description, reproduction steps.
2. **Describe what "done" means**: an acceptance checklist, as concrete as possible.
3. **Limit the scope**: tell it which page/module to change, and not to refactor the whole project.
4. **One task, one thing**: split large work into smaller tasks; success rate and reviewability both improve.
5. **Give a way to verify**: e.g. "run `npm test`" or "open `/login` and check".

A vague example:

> Improve the login.

A good example:

> The login button overflows the card at 125% scaling. Only adjust the login page styles; do not change login logic. Acceptance: the button is fully visible at 100%/125%/150%, and `npm run build` passes.

---

## 8. Let Orcsy start working

### 8.1 Two ways to trigger work

- **Scheduled scan**: the program scans automatically every interval. The default is 300 seconds; change it in Settings (30–86400 seconds).
- **Scan now**: click **▶ Scan now** to run one cycle immediately. Useful right after adding tasks.

They do not double-claim tasks: one project runs at most one Agent at a time.

### 8.2 Top status chips

| Chip | Meaning |
|---|---|
| Idle / Scanning… | Whether a scan is running |
| Cycles done N | Number of scan cycles since launch |
| Last scan just now / N min ago | Most recent scan time |
| Interval Ns | Current scan interval |
| Project: N tasks | Tasks processed by each project in the last cycle |

### 8.3 What happens while a task runs

1. Orcsy scans the project's `tasks/` folder and selects `status: open` tasks.
2. It picks one by priority and order.
3. The status becomes `in_progress`.
4. If the project is a Git repository, it creates a temporary work copy (`.worktree-arranger/`) under the project.
5. The AI reads `AGENT.md` and the task, then reads files, edits files, and runs commands.
6. After every round, a line is appended to the task's **## Execution progress** section, so a hard kill does not lose progress.
7. When the AI reports a result:
   - success: the system commits and merges back to the main branch; pushes if a remote exists; the task becomes `done` and is archived;
   - blocked: the temporary copy is discarded, the task becomes `blocked` with a reason;
   - cancelled: the temporary copy is discarded, the task becomes `cancelled`.

> Only Git projects have a temporary copy. Non-Git projects have no isolation layer; changes the Agent already made stay in the project folder, so review or revert them after a blocked/cancelled run.

### 8.4 Read the run log

The **Run log (newest first, click to collapse)** section at the bottom of the dashboard shows:

- the start and end of every scan cycle;
- the start and end of every task, commit hashes, blocked reasons;
- model thinking content (if enabled), folded into purple entries you can expand.

The page refreshes every 5 seconds. Logs are also written to `logs/orcsy.log`.

### 8.5 After a task ends

- Success: the task becomes **Done** and moves to the archive; the project gets a Git commit;
- Blocked: the task becomes **Blocked**; open it to see the reason under **Agent result**;
- Cancelled: the task becomes **Cancelled**; set `status` back to `open` in the task file to rerun it.

If notifications are enabled, a sound and tray balloon appear for done/blocked; manual stops stay silent.

---

## 9. Dashboard map: what every button does

### 9.1 Top toolbar

Chinese-label reference for the main UI (English name → label shown in the dashboard):

| English | Chinese label |
|---|---|
| Dashboard | 看板 |
| Projects & Settings | 项目与设置 |
| Skills | 技能 |
| Usage | 用量 |
| Check for updates | 检查更新 |
| Scan now | 立即扫描 |
| Restart | 重启 |
| New project | 新建项目 |
| Refresh | ↻ |
| Theme | ☀ / ☾ |
| Pending approvals | 🔔 待审批 |
| Log out | 退出 |
| Open / In progress / Done / Blocked / Cancelled | 待办 / 执行中 / 已完成 / 受阻 / 已取消 |
| New task | 新建任务 |
| Open folder | 打开文件夹 |
| Pull Issues | 拉取 Issues |
| Settings | 设置 |
| Remove | 移除 |
| Run log | 运行日志 |
| Save | 保存 |
| Add | 添加 |
| Cancel | 取消 |

Toolbar buttons:

| Button | Effect |
|---|---|
| Dashboard / Projects & Settings / Skills / Usage | Switch between the four main tabs |
| Check for updates | Check for a new version; when available, the button becomes **Update to x.y.z** |
| ▶ Scan now | Run one scheduling cycle immediately |
| ⟳ Restart | Restart the program so config/code changes take effect; running tasks are interrupted and can be requeued after restart |
| ＋ New project | Open the New project dialog |
| ↻ | Refresh dashboard data |
| ☀ / ☾ | Toggle light/dark theme |
| 🔔 Pending approvals N | N operations are waiting for your decision (approval modes only) |
| Log out | Appears when a login password is set |

### 9.2 Dashboard tab

- Status chips: scheduling state, cycles, last scan, interval, per-project counts;
- Project cards: name, enable switch, git badge, missing-guide badge, review-gate status, auto-pull badge, path, task counts;
- Quick add task;
- Task list: priority color, status tag, pending-approval tag, collapsible description, actions;
- Pagination (10 per page);
- Archived tasks;
- Run log.

### 9.3 Projects tab

Shows only the managed project cards (6 per page, paginated with lazy rendering); card actions match the dashboard: open folder, new task, AGENT.md, pull Issues, settings, remove. Project cards also show the review-gate status (Review on / Review off / Review model conflict).

### 9.4 Settings tab

An accordion menu of sections on the left (click to expand; the right side shows only the selected section), from top to bottom:

1. **🤖 LLM Access**: model, key, endpoint, parameters;
2. **⏱ Scheduler & dashboard**: interval, parallelism, port, log retention, login password;
3. **🔔 Notifications**: sound, system notification;
4. **🔍 Task review (Review Agent)**: review model binding, rework limit, test-command and squash-commit switches;
5. **🛠 Agent tools & permissions**: approval mode, timeout, tool switches, command whitelist, environment variables, PATH auto-fix;
6. **🪞 Mirror management & speed test**: test, apply fastest, add/disable mirrors;
7. **⚙️ App**: autostart, auto-update check, update source, manual package import.

### 9.5 Skills tab

- Browse the skill library;
- Scan user-level skills from AI tools (ZCode / Claude Code / Codex / OpenCode, etc.);
- Import a local skill directory;
- Create a skill;
- Edit, enable/disable, open folder, delete/remove;
- Select all, invert, batch delete.

### 9.6 Usage tab

- Total tasks, total turns, input tokens, output tokens;
- Per-project table (tasks, turns, input/output/total, last used);
- 12-week heatmap (darker = more tokens that day; hover for details);
- Latest 20 task records (time, project, task, result, turns, input/output, model).

---

## 10. Advanced features: approvals, skills, Issues, mirrors

### 10.1 Human approval (permission modes)

The default is **Full access**: the AI does not ask before acting. For more safety, change it in Settings:

| Mode | Who it is for | What it asks about |
|---|---|---|
| `ask_on_change` | First-time users, cautious users | write, edit, multi-edit, delete, move, run command |
| `autonomous` | Experienced users who only want high-risk gates | delete, move, run command |
| `full_access` (default) | Users who trust the AI and want speed | Nothing |

How it works:

1. When the AI wants a sensitive operation, the task **pauses**; the operation is not executed (zero side effects);
2. The top bar shows **🔔 Pending approvals N**;
3. Open it to see project, task, tool, argument preview, and waiting time;
4. Click **Approve** or **Reject**;
5. Check **Always allow this tool for this task** to reduce repeated prompts (current task only);
6. If nobody responds before the timeout (default 30 minutes), the request is automatically rejected; the task never hangs forever.

Rejections and timeouts are reported back to the AI with a reason, so it can adjust or finish as blocked.

### 10.2 Tool switches and command whitelist

**Tool switches** decide which capabilities the AI has. All are on by default (`finish` is the protocol tool that ends a task and is always available).

Common uses:

- No internet: turn off `web_fetch`;
- Code changes only, no commands: turn off `run_command`;
- Review only, no file changes: turn off write/edit/delete/move tools.

**Command whitelist** decides what `run_command` may execute. The default includes python, pytest, node, npm, cargo, go, dotnet, git, package managers, and common read-only commands. Turning a command off makes the AI's call fail immediately.

Projects can override global settings: open **Override Agent tool switches** in the project dialog and set each tool. Remove the override to follow global again.

### 10.3 Skill library

A skill is a **specialized handbook** for the AI, for example:

- project coding conventions;
- best practices for a framework;
- release process;
- environment setup guide.

Four sources:

1. **Built-in `env-setup`**: seeded on first launch; teaches the AI to install missing toolchains;
2. **Scan AI tools**: batch-import skills already installed in ZCode / Claude Code / Codex / OpenCode;
3. **Import local skill**: choose a directory containing `SKILL.md`;
4. **Create skill**: write name, description, and body directly in the dashboard.

Scope:

- **Global**: available to all projects;
- **Dedicated · project**: only that project.

Notes:

- Skills live in `skills\` under the data directory and are **never written into project folders**;
- The system prompt only receives a name + description index; the AI reads the full text via `read_skill` on demand, so many skills do not bloat the context;
- Removing an imported skill only removes the Orcsy copy; the original remains in the external tool's folder and can be re-scanned;
- A skill created in Orcsy is gone forever once deleted.

### 10.4 Issue import

If your project is hosted on GitHub / Gitee / GitLab / Gitea, Issues can become tasks.

Setup:

1. Project **Settings** → enable **Bind remote repository Issues**;
2. Enter the repository URL, e.g. `https://github.com/owner/repo`;
3. Choose the platform (self-hosted GitLab/Gitea must be selected explicitly);
4. Enter credentials:
   - token (recommended; GitHub supports tokens only);
   - or username + password (GitLab / Gitee / Gitea);
5. Optional: auto-pull interval in minutes (0 = manual only);
6. Save.

Use:

- Click **Pull Issues** on the project card to import open Issues into the queue;
- Priority defaults to `P2`; Issues labeled `P0`–`P4` use that label;
- Task files carry a source annotation; already-imported Issues (including archived ones) are skipped;
- **One-way import**: Orcsy does not write task status back to the Issue.

### 10.5 Mirrors and slow networks

If you are in a region with slow or blocked access to package registries, Orcsy has three fallbacks:

1. **Mirror speed test**: Settings → **🪞 Mirror management & speed test** → **Test all sources** → **Apply fastest to environment variables**;
2. **Environment variables**: fill `GOPROXY`, `PIP_INDEX_URL`, `npm_config_registry`, `HTTP_PROXY`, etc. under **Agent subprocess environment variables**;
3. **env-setup skill**: when a toolchain is missing, the AI follows the skill to install it; if package managers fail, it switches to direct domestic mirror downloads.

Mirror categories include: Go installer, Go module proxy, npm registry, pip index, Node.js installer, JDK, Python installer. You can disable sources or add your own internal mirror.

> ⚠️ **Apply fastest to environment variables** overwrites the same kind of variables you typed manually. Check the text box afterwards.

### 10.6 Multiple projects, parallelism, dedicated models

- One Agent per project; projects do not interfere;
- If **Parallel Agents** in Settings is greater than 1, multiple projects can run at the same time (1–16);
- One project still runs only one task at a time;
- A project can bind a dedicated model: use a cheaper model for simple projects and a stronger one for complex projects;
- A dedicated model overrides only the fields you fill; the rest inherit the global config.

### 10.7 Notifications

Settings → **🔔 Notifications**:

- **Sound**: the bundled "ding-dong" for done, a bundled warning tone for blocked (falls back to the Windows system sound if the bundled audio is missing);
- **System notification**: tray balloon, works even when the dashboard is closed;
- Manual stops (cancelled) stay silent;
- With `--no-tray`, there is no tray and therefore no system-notification channel.

### 10.8 Auto-update and restart

- Auto-check for updates on startup (can be turned off);
- Top bar **Check for updates** → when a new version exists, click **Update to x.y.z**;
- The update installs silently in the background, the app restarts, and the page reloads;
- If installed under `Program Files` or another system directory, one UAC prompt appears;
- A failed update rolls back automatically and opens the install log;
- You can also **import a local package**: choose `orcsy-update-*.zip`; same-version reinstall, upgrade, and downgrade are supported.

### 10.9 Dashboard login password

By default the dashboard only listens on `127.0.0.1`, so only this machine can reach it and no password is needed.

You **must set a password** if you:

- use `--host 0.0.0.0` to allow LAN access;
- deploy with Docker / systemd on a server;
- expose it through a reverse proxy to the internet.

With a non-loopback address and an empty password, the program refuses to start. If that protection were bypassed and the port exposed, anyone who can reach it could operate the dashboard, change configuration, and start/stop tasks (keys are shown only as masks, but they can still be replaced).

Where to set it:

- Settings → **⏱ Scheduler & dashboard** → **Dashboard login password**;
- or edit `web.auth_password` in `config.yaml`.

After it is set, opening the dashboard shows a login page: enter the password and click **Log in**. A **Log out** button appears at the top right.

Behavior:

- A login session lasts 7 days;
- After a program restart you need to log in again;
- Changing the password invalidates all old sessions immediately;
- Authentication is not a substitute for TLS. For internet exposure, put a reverse proxy (nginx / caddy) with HTTPS in front.

### 10.10 Logs, autostart, and port

**Logs**

- The **Run log** on the dashboard shows only the recent tail; the full log is `logs/orcsy.log` in the data directory;
- Under **⏱ Scheduler & dashboard** you can set:
  - **Log retention (days)**: default 7. When full, logs are archived to `logs/archive/`, and logs older than the limit are cleaned automatically; `0` disables automatic cleanup;
  - **Total log size limit (MB)**: default 200. When active logs + backups + archives exceed it, the oldest are deleted first; `0` means unlimited;
- Saving applies the new rules immediately.

**Autostart**

- Settings → **⚙️ App** → turn on **Start automatically on boot**;
- It writes to the current user's registry and needs no administrator rights;
- The tray menu also has an autostart checkbox;
- Turn the switch off to stop it.

**Dashboard port**

- Settings → **⏱ Scheduler & dashboard** → **Dashboard port**;
- Takes effect on the **next start**;
- You can also override it with `--port 8080`;
- Port-conflict behavior is described in [12.1](#121-the-dashboard-does-not-open--port-is-in-use).

---

### 10.11 Task review (Review Agent): a second model as gatekeeper

After a task finishes and before its changes are merged and archived, Orcsy can have an **independent review Agent** (**a different model** from the one that executed the task) inspect the result: it checks the implementation against the requirements and acceptance criteria, optionally running tests for real verification. Only a passed review gets merged into the main branch and archived; a failed review sends the task back for automatic rework with the issue list attached.

**Default state: review is off.** Without a review model configured, everything works as before (tasks are merged and archived on completion). To enable it, just give the system a **second model** (a cheap small model works fine):

1. Open the **🔍 Task review (Review Agent)** card on the settings page;
2. Fill in the review model's provider / base_url / api_key / model (click **Fetch model list** to pull candidates);
3. Save. The review gate immediately applies to **all projects** by default (to opt a project out, open that project's **Settings** → **Review Agent** and turn it off there).

**Hard rule: the review model must differ from the task model.** The task model is configured under **LLM access** on the settings page (projects may bind a dedicated model); saving a review model with the same name is rejected — two different models cross-check each other so the system never lets the executor grade its own work.

**How the review works:**

- When the task Agent reports completion, the changes are committed to the working branch first (**not merged yet**);
- The review Agent inspects the code read-only: it goes through the acceptance criteria in the task one by one, and actually runs tests when **Allow test commands** is on (default);
- Pass → merged into the main branch automatically → archived to **Completed**;
- Fail → the task is **automatically reworked** with the review comments (written into the task body, so the task Agent sees them on its next run); rework is allowed 2 times by default (globally adjustable 1–10, overridable per project); when the limit is reached the task becomes **Blocked** for human judgment — the work stays on the working branch, ready to inspect or merge manually;
- If the program is shut down mid-review, nothing is lost: the changes are already committed on the branch, and the review resumes automatically on the next start without re-running the task.

**Rework and commits:** after a reworked task finally passes review, its multiple WIP commits are squashed into a single commit before merging (**Squash commits after rework passes**, on by default), keeping the main-branch history clean.

**Badges on the dashboard:** `🔍 Pending review` on a task card means it has finished and is being reviewed or waiting for a re-review; `🔍 Rework ×N` means it has been sent back by review N times. Project cards show a **Review on / Review off / Review model conflict** status.

**Cost note:** one review costs roughly 3–8 rounds of read-only calls; on the settings **Usage** view it is recorded separately under the review kind, so you can account it apart from execution.

---

### 10.12 Multi-Model Agent Project Team

Orcsy 0.21.0 introduces **Multi-Model Agent Project Teams**. You can assemble specialized AI crews (Project Manager, Backend, Frontend, QA, and Custom Roles) for any project:

- **👑 Project Manager (PM)**: reads and explores codebase architecture read-only, decomposes requirements into DAG subtasks using `depends_on_refs`, or dispatches single-component tasks directly via `reassign_task`.
- **⚙️ Backend / 🎨 Frontend Developers**: code and unit-test inside task-isolated Git Worktree sandboxes, with upstream delivery summaries automatically injected into prompts.
- **🧪 Test Engineer (QA)**: spins up local dev servers and automated Chromium browsers for end-to-end integration testing; files structured defects using `report_bug`, automatically binds regression dependencies, and triggers automated retesting once bugs are resolved.
- **🛡️ Sandboxed Worktree + Human-Gated Final Commit**: all coding runs safely inside isolated worktrees and merges into the main working tree unstaged/uncommitted — **Orcsy never auto-commits or auto-pushes to main branch by default**, leaving 100% of final commit and push authority to the human reviewer!
- **⚡ Intra-Cycle Chaining (`chain_within_cycle`)**: downstream tasks execute immediately within the same cycle once dependencies are resolved, completing multi-phase requirements in minutes.
- **👥 Two-Tier Configuration**: global `agent_profiles` pool reusable across projects, with project-level team customization and fallback routing.

---

### 10.13 Local Debugging & Chromium CDP Automation

For Web and API projects, Orcsy ships built-in tools for end-to-end verification:
- **Chromium Browser Automation (`browser_*`)**: native Python CDP client connected to local Google Chrome or Microsoft Edge (zero external drivers required), providing element snapshots with `ref` addressing, clicking, typing, waiting, screenshots, and console/network log listening.
- **Dev Server Management (`dev_server_*`)**: runs background services with dynamic `{PORT}` allocation and readiness probing, terminating child processes cleanly on task end.
- **Local HTTP API Client (`http_request`)**: tests local/LAN REST and GraphQL endpoints with cookie jars and host allowlist enforcement to block intranet penetration.

---

## 11. Data, backup, and migration

### 11.1 Where data lives

| Content | Windows installer | Docker | Linux systemd |
|---|---|---|---|
| Config | `%APPDATA%\Orcsy\config.yaml` | `./orcsy-data/config.yaml` | `/var/lib/orcsy/config.yaml` |
| Skills | `%APPDATA%\Orcsy\skills\` | `./orcsy-data/skills/` | `/var/lib/orcsy/skills/` |
| Logs | `%APPDATA%\Orcsy\logs\` | `./orcsy-data/logs/` | `/var/lib/orcsy/logs/` |
| Usage ledger | `%APPDATA%\Orcsy\logs\usage.jsonl` | `./orcsy-data/logs/usage.jsonl` | `/var/lib/orcsy/logs/usage.jsonl` |
| Tasks | Each project's `tasks\` | `./projects/<project>/tasks/` | `/srv/orcsy-projects/<project>/tasks/` |
| Project guide | Each project's `AGENT.md` | `./projects/<project>/AGENT.md` | `/srv/orcsy-projects/<project>/AGENT.md` |

### 11.2 Backup

Back up:

- the data directory (config, skills, logs, usage);
- each project's `tasks/` and `AGENT.md`;
- the project code itself (use a Git remote).

`config.yaml` contains your API key. **Never upload it to a public repository or share it.**

### 11.3 Move to another computer

1. Install Orcsy on the new computer;
2. Close the program;
3. Copy the old data directory to the corresponding location;
4. Open `config.yaml` and update `projects[].path` to the new paths if needed;
5. Start the program and check that projects and tasks are present.

You can also set the `ORCSY_DATA_DIR` environment variable to move the whole data directory elsewhere (for example, an external drive).

---

## 12. FAQ and troubleshooting

### 12.1 The dashboard does not open / port is in use

- Check that the program is running (is the tray icon still there?);
- The default address is <http://127.0.0.1:5116>;
- If the port is occupied:
  - started with the configured port: Orcsy automatically picks a free port and prints it in the console;
  - started with an explicit `--port`: it exits with an error instead of silently changing ports (your explicit choice is respected);
- If another Orcsy is already running, it tells you and exits, so you do not accidentally run two.

### 12.2 A task stays "Blocked"

Open the task and read **Agent result** plus the run log. Common causes:

- LLM request failed (timeout, 429, 500, 502, 503, 504): Orcsy retries with backoff; only after that does it mark blocked;
- The AI decided it lacks information or needs a human decision;
- An approval was rejected or timed out;
- A test command is missing or the toolchain is not installed;
- A merge conflict.

What to do:

1. Read the log to find the cause;
2. Improve the task description, add information, or narrow the scope;
3. Click **Requeue**; it will be picked up first on the next scan.

### 12.3 Will a task hang forever if nobody handles an approval?

No. After the approval timeout (default 30 minutes) it is automatically rejected; the AI receives the reason and either adjusts or finishes as blocked.

### 12.4 Will tasks be lost if I shut down or kill the process?

Task files are not lost. Execution progress is written into the task's **## Execution progress** section in real time. An interrupted task is reset to open on the next launch and re-queued.

### 12.5 Dependency downloads fail on a slow network

Try, in order:

1. Settings → **Mirror management & speed test** → test → apply fastest;
2. Set `GOPROXY` / `PIP_INDEX_URL` / `npm_config_registry` / `HTTP_PROXY` under **Agent subprocess environment variables**;
3. Turn on **Write missing tool directories into the user PATH at task start**;
4. If your company has an internal mirror, add it on the mirror page and test it.

### 12.6 Which models are supported?

- OpenAI-compatible: DeepSeek, Zhipu GLM, Kimi, Qwen, OpenAI, Ollama, vLLM, etc.;
- Anthropic protocol: Claude official and compatible services.

Use **Fetch list** and **Test connection** to verify.

### 12.7 What should I watch out for when exposing the dashboard?

- By default it listens only on `127.0.0.1`, which is safest for local use;
- If exposed, you must set `web.auth_password`;
- A non-loopback address with an empty password refuses to start;
- Authentication is not TLS; use a reverse proxy with HTTPS for internet exposure;
- Never commit `config.yaml` to a repository.

### 12.8 Merge conflict / push failure

- Commits and merges are performed by the system; the AI does not operate Git directly;
- On a merge conflict, the `arranger/<project>` branch is kept, the task is marked blocked, and a human steps in;
- Push failures are usually credentials: use a token for HTTPS, a key for SSH;
- After fixing, click **Requeue**.

### 12.9 Does updating on Windows need administrator rights?

- In a user-writable install directory: fully zero-UAC;
- Under `Program Files` or another system directory: one UAC prompt; click **Yes** to continue. Cancel leaves the app running without updating;
- A failed update rolls back and keeps the install log.

### 12.10 Can I use it as a command-line scheduler without the dashboard?

Yes:

```bash
orcsy run --config config.yaml          # keep scheduling
orcsy run --config config.yaml --once   # one cycle, suitable for Task Scheduler / cron
```

The dashboard mode can also use `--no-sched` to keep only manual **Scan now**.

### 12.11 The tray icon is missing / I do not want a tray

- Start with `--no-tray`;
- Docker disables the tray automatically;
- The dashboard still works without a tray; just open the address. System notifications have no channel without a tray, though.

### 12.12 Where did the code go after a task finishes?

- Git project: committed and merged back to the main branch; pushed if a remote is configured;
- Non-Git project: edited directly in the project folder, no commit;
- Git project blocked/cancelled: the temporary copy is discarded and the main project folder is unchanged;
- Non-Git project blocked/cancelled: there is no temporary copy; changes the Agent already made stay in the project folder, so review or revert them yourself.

---

## 13. Glossary

| Term | Plain-English meaning |
|---|---|
| LLM | The AI service that provides the intelligence, e.g. DeepSeek, GPT, Claude |
| API Key | The "key" for calling the LLM service, like an account password |
| base_url | The "front door address" of the LLM service |
| Agent | The AI assistant that reads code, edits files, and runs commands |
| Task | A Markdown file describing what to do |
| Priority (P0–P4) | Who goes first; P0 is most urgent, P4 least |
| Status | Where the task is now: open, in progress, done, blocked, cancelled |
| AGENT.md | The project's employee handbook, telling the AI the rules |
| Git | A version-control tool that records every change |
| Repository (repo) | A project folder managed by Git |
| Commit | A recorded change in Git history |
| Branch | An independent line of work in Git |
| Worktree | A temporary working copy where the AI edits code without touching the main tree |
| Issue | A problem ticket on GitHub/Gitee/etc. |
| Token | The LLM billing unit; roughly "amount of text" |
| Approval | The AI pauses before a sensitive operation and waits for you to approve or reject |
| Skill | A specialized handbook for the AI |
| Mirror | An alternative download address, often faster in some regions |

---

## 14. Appendix: task template and command reference

### 14.1 General task template

```markdown
---
title: One sentence describing the work
priority: P2
status: open
labels: [bug]
assignee:
---

## Background / symptoms

What is happening now? What is wrong?

## Steps to reproduce (bugs)

1. …
2. …
3. …

## Expected result

What does "done" look like?

## Acceptance criteria

- [ ] …
- [ ] …

## Constraints

- Only change these files/modules;
- Do not change these things;
- Must pass this test/build command.
```

### 14.2 Command reference

```bash
# Initialize (source run)
uv run orcsy init --dir run

# Validate config and task files
uv run orcsy validate --config run/config.yaml

# Start the dashboard (with scheduled scanning)
uv run orcsy web --config run/config.yaml

# Run one cycle
uv run orcsy run --config run/config.yaml --once

# Keep scheduling without a dashboard
uv run orcsy run --config run/config.yaml

# Change the port
uv run orcsy web --config run/config.yaml --port 8080

# Allow LAN access (set a password!)
uv run orcsy web --config run/config.yaml --host 0.0.0.0

# Docker
docker compose up -d --build
docker compose logs -f
docker compose down

# Linux systemd
systemctl status orcsy
sudo systemctl restart orcsy
journalctl -u orcsy -f
```

### 14.3 Minimal configuration

```yaml
llm:
  provider: openai
  api_key: "sk-your-key"
  base_url: https://api.deepseek.com/v1
  model: deepseek-chat

scheduler:
  interval_seconds: 300
  max_parallel_agents: 1

web:
  host: 127.0.0.1
  port: 5116
  open_browser: true
  auth_password: ""

projects:
  - name: my-app
    path: D:/Repos/my-app
    enabled: true
```

> For the full configuration reference, see `config.example.yaml` and `README.md` in the repository root.

---

**Now hand the repetitive work to Orcsy and keep your time for what matters.**
