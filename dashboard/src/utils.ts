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
