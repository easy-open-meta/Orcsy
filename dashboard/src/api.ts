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
  /** 项目专属模型绑定（部分覆盖全局 llm）；null = 未绑定 */
  llm?: Record<string, string> | null
  /** 远程仓库绑定（Issues 导入）；null = 未绑定；认证 token 与账号密码二选一；
   * auto_pull_interval 为定时自动拉取间隔（秒），缺省 = 仅手动拉取 */
  repo?: { url: string; token?: string; username?: string
           password?: string; provider?: string; api_base?: string
           auto_pull_interval?: number } | null
  /** 项目级工具开关覆盖 {工具名: bool}；null = 跟随全局 tools_cfg */
  tools?: Record<string, boolean> | null
  /** 项目级访问权限覆盖；null = 跟随全局 permissions_cfg.mode */
  permission_mode?: PermissionMode | null
  /** 当前生效的访问权限模式（后端解析：项目覆盖 ?? 全局） */
  permission_mode_effective?: PermissionMode
  /** 定时拉取 Issues 状态（repo.auto_pull_interval 启用时非空） */
  issue_auto_pull?: { interval: number; imported?: number; skipped?: number
                      total?: number; last_error?: string } | null
  /** 程序自更新状态机 */
  update?: {
    phase: 'idle' | 'checking' | 'latest' | 'available' | 'downloading'
           | 'applying' | 'restarting' | 'error'
    message?: string
    current?: string
    latest?: string
    notes?: string
    asset_name?: string
    percent?: number
    install_log_tail?: string
    target_version?: string | null
  }
  is_git_repo: boolean
  guide_file_path: string
  guide_exists: boolean
  tasks: TaskT[]
  parse_errors: { id: string; path: string; error: string }[]
  archived: TaskT[]
}

export interface SkillT {
  name: string
  description: string
  scope: string      // 'global' 或项目名
  enabled: boolean
  path: string
  origin: string     // 'created'=本程序新建（可硬删除）；'imported'=外部导入（只能移除）
  error: string
}

/** 访问权限模式：变更前询问 / 自主模式（仅高风险询问）/ 完全访问（不询问） */
export type PermissionMode = 'ask_on_change' | 'autonomous' | 'full_access'

/** 一条挂起的人工审批请求（GET /api/state 的 approvals 字段） */
export interface ApprovalT {
  id: string
  project: string
  task_id: string
  tool: string
  args_preview: string
  mode: PermissionMode
  created_at: number
  waited_seconds: number
}

export interface AppStateT {
  version: string
  config_path: string
  /** 程序自更新状态机（与 ProjectT.update 同构；看板轮询取这里） */
  update?: ProjectT['update']
  /** 上次升级结果：后端一次性曝光（读后即清）；ok=false 时弹窗并已自动打开安装日志 */
  update_result?: { ok: boolean; message?: string } | null
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
  web_cfg: { host: string; port: number; open_browser: boolean; auth_enabled: boolean }
  update_cfg: { repo: string; token: string; auto_check: boolean }
  logging: { retention_days: number }
  /** 全局 Agent 工具开关：只含显式设置过的项，未列出的工具默认启用 */
  tools_cfg: Record<string, boolean>
  /** 可开关工具目录（名称+中文描述；finish 为协议工具不在其中） */
  tools_catalog: { name: string; description: string }[]
  /** 全局访问权限：模式 + 审批超时（分钟） */
  permissions_cfg: { mode: PermissionMode; timeout_minutes: number }
  /** 当前挂起的人工审批请求（5s 轮询刷新；处理后条目消失） */
  approvals: ApprovalT[]
  app_autostart: { enabled: boolean }
  skills_dir: string
  skills: SkillT[]
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
  /** 目录下的文件（完整路径）；文件选择模式用 */
  files?: string[]
  note?: string
}

// Windows 回环偶发连接重置 / 看板重启瞬间，fetch 会抛 TypeError("Failed to
// fetch")——网络层失败转成人话；GET 是幂等读，自动重试一次
const NET_ERR = '无法连接看板服务（可能正在重启），请刷新页面重试'

function isNetErr(e: unknown): boolean {
  return e instanceof TypeError  // fetch 的网络层失败统一是 TypeError
}

// ---- 登录鉴权：401 全局回调（store 注册后把应用切到未登录态，登录层接管） ----
export class UnauthorizedError extends Error {
  constructor() { super('未登录或会话已过期') }
}
let onUnauthorized: (() => void) | null = null
export function setUnauthorizedHandler(fn: () => void): void { onUnauthorized = fn }

function handle401(r: Response, path: string): void {
  // /api/login 的 401 是「密码错误」，属正常业务反馈，不触发全局登出
  if (r.status === 401 && path !== '/api/login') onUnauthorized?.()
}

async function post(path: string, body: unknown): Promise<any> {
  let r: Response
  try {
    r = await fetch(path, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    })
  } catch {
    throw new Error(NET_ERR)
  }
  handle401(r, path)
  const j = await r.json().catch(() => ({}))
  if (!r.ok || j.ok === false) throw new Error(j.message || j.error || `HTTP ${r.status}`)
  return j
}

async function get(path: string): Promise<any> {
  let last: unknown
  for (let attempt = 0; attempt < 2; attempt++) {
    try {
      const r = await fetch(path)
      handle401(r, path)
      const j = await r.json().catch(() => ({}))
      if (!r.ok || j.ok === false) throw new Error(j.message || j.error || `HTTP ${r.status}`)
      return j
    } catch (e: unknown) {
      if (!isNetErr(e)) throw e  // 业务错误直接抛，不重试
      last = e
      await new Promise(res => setTimeout(res, 250))
    }
  }
  throw new Error(NET_ERR)
}

export const api = {
  state: (): Promise<AppStateT> => get('/api/state'),
  /** 鉴权状态：enabled=是否配置了登录密码；authed=当前会话 Cookie 是否有效 */
  authStatus: () => get('/api/auth') as Promise<{
    ok: boolean; enabled: boolean; authed: boolean
  }>,
  login: (password: string) => post('/api/login', { password }),
  logout: () => post('/api/logout', {}),
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
  taskDeleteBatch: (project: string, ids: string[]) =>
    post('/api/task/delete-batch', { project, ids }) as Promise<{
      ok: boolean; message: string; deleted: number; failed: string[]
    }>,
  taskClear: (project: string) => post('/api/task/clear', { project }) as Promise<{
    ok: boolean; message: string; cleared: number
  }>,
  models: (p: { provider: string; base_url: string; api_key: string }) =>
    post('/api/models', p) as Promise<{ models: ModelInfo[] }>,
  testModel: (p: { provider: string; base_url: string; api_key: string; model: string }) =>
    post('/api/llm/test', p) as Promise<{
      ok: boolean; message: string; reply: string; elapsed_seconds: number
    }>,
  browse: (path: string) => post('/api/browse', { path }) as Promise<DirListing>,

  probe: (path: string) =>
    post('/api/probe', { path }) as Promise<{ ok: boolean; stack: string; run: string; test: string }>,
  mkdir: (path: string) => post('/api/mkdir', { path }) as Promise<{ ok: boolean; path: string }>,
  projectSave: (p: Record<string, unknown>) => post('/api/project', p),
  projectDelete: (name: string) => post('/api/project/delete', { name }),
  projectOpenFolder: (name: string) => post('/api/project/open-folder', { name }),
  issuesSync: (name: string) => post('/api/project/issues-sync', { name }) as Promise<{
    ok: boolean; message: string; imported: number; skipped: number
  }>,
  guide: (project: string) =>
    get(`/api/guide?project=${encodeURIComponent(project)}`) as Promise<{ file: string; content: string }>,
  guideSave: (project: string, content: string) => post('/api/guide', { project, content }),
  skillGet: (name: string) =>
    get(`/api/skill?name=${encodeURIComponent(name)}`) as Promise<SkillT & { body: string }>,
  skillSave: (p: {
    original_name?: string; name: string; description: string
    scope: string; enabled: boolean; body: string
  }) => post('/api/skill', p),
  skillRemove: (names: string[]) => post('/api/skill/remove', { names }) as Promise<{
    ok: boolean; message: string; removed: string[]
    skipped: { name: string; reason: string }[]
  }>,
  skillEnabled: (name: string, enabled: boolean) => post('/api/skill/enabled', { name, enabled }),
  skillImport: (path: string) =>
    post('/api/skill/import', { path }) as Promise<{ ok: boolean; message: string; skill: SkillT }>,
  skillScan: () => get('/api/skill/scan') as Promise<{
    ok: boolean
    sources: {
      tool: string; path: string
      skills: { name: string; description: string; path: string; exists: boolean }[]
    }[]
  }>,
  skillImportBatch: (paths: string[]) => post('/api/skill/import-batch', { paths }) as Promise<{
    ok: boolean; message: string; imported: string[]
    skipped: { path: string; reason: string }[]
  }>,
  skillOpenFolder: (name: string) => post('/api/skill/open-folder', { name }),
  config: (p: Record<string, unknown>) => post('/api/config', p),
  /** 响应一条挂起的审批：approved=批准/拒绝；always=本任务内始终允许该工具（仅批准时有意义） */
  approvalRespond: (id: string, approved: boolean, always = false) =>
    post('/api/approval', { id, approved, always }),
  autostart: (enabled: boolean) => post('/api/app-autostart', { enabled }),
  updateCheck: () => post('/api/update/check', {}) as Promise<{
    ok: boolean; phase: string; has_update: boolean; latest: string
    current: string; notes: string; asset_name: string; download_url: string
  }>,
  updateApply: () => post('/api/update/apply', {}) as Promise<{
    ok: boolean; phase: string; message: string
  }>,
  /** 手动导入本地 zip 更新包（服务端本地路径，非上传）；同/异版本均允许 */
  updateInstall: (path: string) => post('/api/update/install', { path }) as Promise<{
    ok: boolean; phase: string; message: string; target: string | null
  }>,
}
