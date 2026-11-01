// 版本号比较：返回 >0 / 0 / <0（'V0.14.0' 与 '0.14' 等形态均可，缺段按 0 补）
export function cmpVersion(a: string, b: string): number {
  const pa = (a || '').replace(/^[Vv]/, '').split(/[.\-+ ]+/).map(x => parseInt(x) || 0)
  const pb = (b || '').replace(/^[Vv]/, '').split(/[.\-+ ]+/).map(x => parseInt(x) || 0)
  const len = Math.max(pa.length, pb.length)
  for (let i = 0; i < len; i++) {
    const d = (pa[i] || 0) - (pb[i] || 0)
    if (d) return d
  }
  return 0
}

// 相对时间显示：后端时间格式 "YYYY-MM-DD HH:MM[:SS]"
export function relTime(s: string | undefined | null): string {
  if (!s) return '—'
  const d = new Date(s.replace(' ', 'T'))
  const x = (Date.now() - d.getTime()) / 1000
  if (Number.isNaN(x) || x < 0) return s
  if (x < 60) return '刚刚'
  if (x < 3600) return `${Math.floor(x / 60)} 分钟前`
  if (x < 86400) return `${Math.floor(x / 3600)} 小时前`
  return s
}

// token 数量的厂商风格展示：1000448 → "1M"，131072 → "131K"。
// M 用十进制（1e6）换算并保留一位小数，与厂商控制台的 "1M/2M" 口径一致，
// 避免 999424 这类值被二进制换算成 "977K" 的困惑。
export function fmtTokens(v: number | null | undefined): string {
  if (v == null || !Number.isFinite(v) || v <= 0) return ''
  if (v >= 1e6) {
    const m = Math.round((v / 1e6) * 10) / 10
    return `${m}M`
  }
  // 整千按十进制（128000 → 128K）；非整千的二进制整值按 /1024（32768 → 32K）
  if (v % 1000 === 0) return `${Math.round(v / 1000)}K`
  if (v % 1024 === 0) return `${Math.round(v / 1024)}K`
  return `${Math.round(v / 1000)}K`
}

// 运行日志的展示结构：一条日志 = 首行（时间戳开头）+ 若干续行（异常堆栈、
// 模型思考正文等）。续行归属上一条，避免多行消息被时间排序拆散。
export interface LogEntry {
  head: string      // 首行（含时间戳与级别）
  body: string      // 续行（不含首行，可为空）
  thinking: boolean // 💭 模型思考条目，看板里折叠展示
}

const LOG_HEAD_RE = /^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}/

// 把日志尾文本拆成条目并按从新到旧排序（文件按时间正序追加，反转即可）。
export function parseLogEntries(text: string): LogEntry[] {
  const entries: LogEntry[] = []
  for (const line of (text || '').split('\n')) {
    if (LOG_HEAD_RE.test(line) || entries.length === 0) {
      entries.push({ head: line, body: '', thinking: line.includes('💭') })
    } else {
      const last = entries[entries.length - 1]
      last.body += (last.body ? '\n' : '') + line
    }
  }
  return entries.reverse()
}
