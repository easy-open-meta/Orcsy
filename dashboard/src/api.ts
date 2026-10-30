// 后端 API 的类型化封装。端点与 orcsy/web.py 的 do_GET/do_POST 一一对应。

export type ThemeName = 'dark' | 'light'
export type TaskStatus = 'open' | 'in_progress' | 'done' | 'blocked' | 'cancelled'

export interface TaskT {
  id: string
  title: string
  priority: string
  rank: number
  status: TaskStatus
  labels: string[]
  assignee: string
  error: string
  file: string
  description: string
}

export interface ProjectT {
  name: string
  path: string
  enabled: boolean
  tasks_dir: string
  guide_file: string
  max_tasks_per_cycle: number
  max_agent_turns: number
  allowed_branch: string | null
  allowed_commands: string[] | null
  is_git_repo: boolean
  guide_file_path: string
  guide_exists: boolean
  tasks: TaskT[]
  parse_errors: { id: string; path: string; error: string }[]
  archived: TaskT[]
}

export interface AppStateT {
  version: string
  config_path: string
  llm: {
    provider: 'openai' | 'anthropic'
    model: string
    base_url: string
    key_configured: boolean
    api_key: string
    temperature: number
    max_tokens: number
    max_context_tokens: number
    enable_thinking: boolean
    timeout_seconds: number
  }
  scheduler: {
    running: boolean
    cycles_done: number
    last_cycle_at: string
    next_cycle_at: string
    interval_seconds: number
    last_cycle_stats: Record<string, number>
  }
  scheduler_cfg: { interval_seconds: number; max_parallel_agents: number }
  web_cfg: { host: string; port: number; open_browser: boolean }
  app_autostart: { enabled: boolean }
  projects: ProjectT[]
  log_tail: string
}

export interface ModelInfo {
  id: string
  context_window: number | null
  max_output_tokens: number | null
}

export interface DirListing {
  ok: boolean
  path: string
  parent: string | null
  dirs: string[]
  note?: string
}

async function post(path: string, body: unknown): Promise<any> {
  const r = await fetch(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
  const j = await r.json().catch(() => ({}))
  if (!r.ok || j.ok === false) throw new Error(j.message || j.error || `HTTP ${r.status}`)
  return j
}

async function get(path: string): Promise<any> {
  const r = await fetch(path)
  const j = await r.json().catch(() => ({}))
  if (!r.ok || j.ok === false) throw new Error(j.message || j.error || `HTTP ${r.status}`)
  return j
}

export const api = {
  state: (): Promise<AppStateT> => get('/api/state'),
  scan: () => post('/api/scan', {}),
  taskFull: (project: string, id: string) =>
    get(`/api/task?project=${encodeURIComponent(project)}&id=${encodeURIComponent(id)}`),
  taskSave: (p: {
    project: string; id: string; title: string; priority: string; status: string
    labels: string[]; assignee: string; description: string
  }) => post('/api/task', p),
  taskStatus: (project: string, id: string, status: string) =>
    post('/api/task/status', { project, id, status }),
  taskDelete: (project: string, id: string) => post('/api/task/delete', { project, id }),
  models: (p: { provider: string; base_url: string; api_key: string }) =>
    post('/api/models', p) as Promise<{ models: ModelInfo[] }>,
  browse: (path: string) => post('/api/browse', { path }) as Promise<DirListing>,
  probe: (path: string) =>
    post('/api/probe', { path }) as Promise<{ ok: boolean; stack: string; run: string; test: string }>,
  mkdir: (path: string) => post('/api/mkdir', { path }) as Promise<{ ok: boolean; path: string }>,
  projectSave: (p: Record<string, unknown>) => post('/api/project', p),
  projectDelete: (name: string) => post('/api/project/delete', { name }),
  projectOpenFolder: (name: string) => post('/api/project/open-folder', { name }),
  guide: (project: string) =>
    get(`/api/guide?project=${encodeURIComponent(project)}`) as Promise<{ file: string; content: string }>,
  guideSave: (project: string, content: string) => post('/api/guide', { project, content }),
  config: (p: Record<string, unknown>) => post('/api/config', p),
  autostart: (enabled: boolean) => post('/api/app-autostart', { enabled }),
}
