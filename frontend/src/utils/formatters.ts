const BEIJING_TIMEZONE = 'Asia/Shanghai'

function parseDate(value: string | Date | null | undefined) {
  if (!value) return null

  const date = value instanceof Date ? value : new Date(value)
  if (Number.isNaN(date.getTime())) return null

  return date
}

function parseDateParts(value: string | Date | null | undefined) {
  const d = parseDate(value)
  if (!d) return null

  const formatter = new Intl.DateTimeFormat('zh-CN', {
    timeZone: BEIJING_TIMEZONE,
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
    hour12: false,
  })

  const parts = formatter.formatToParts(d)
  const partMap = Object.fromEntries(parts.map((part) => [part.type, part.value]))

  return {
    year: partMap.year,
    month: partMap.month,
    day: partMap.day,
    hour: partMap.hour,
    minute: partMap.minute,
    second: partMap.second,
  }
}

function dayKeyToNumber(dayKey: string) {
  const [year, month, day] = dayKey.split('-').map(Number)
  return Math.floor(Date.UTC(year, month - 1, day) / 86400000)
}

export function getBeijingDateKey(value: string | Date | null | undefined): string | null {
  const parts = parseDateParts(value)
  if (!parts) return null
  return `${parts.year}-${parts.month}-${parts.day}`
}

export function getBeijingDayDifference(
  value: string | Date | null | undefined,
  reference: string | Date | null | undefined = new Date(),
): number | null {
  const dayKey = getBeijingDateKey(value)
  const referenceDayKey = getBeijingDateKey(reference)
  if (!dayKey || !referenceDayKey) return null
  return dayKeyToNumber(referenceDayKey) - dayKeyToNumber(dayKey)
}

export function formatChatSessionTime(
  value: string | Date | null | undefined,
  reference: string | Date | null | undefined = new Date(),
): string {
  const parts = parseDateParts(value)
  if (!parts) return ''

  const dayDifference = getBeijingDayDifference(value, reference)
  if (dayDifference !== null && dayDifference <= 0) {
    return `${parts.hour}:${parts.minute}`
  }

  return `${parts.month}-${parts.day}`
}

export function formatMoney(val: number | null | undefined): string {
  if (val === undefined || val === null) return '¥--'
  return `¥${val.toFixed(2)}`
}

export function formatPercent(val: number | null | undefined): string {
  if (val === undefined || val === null) return '--%'
  return `${(val * 100).toFixed(2)}%`
}

export function formatTime(isoStr: string | null | undefined): string {
  if (!isoStr) return '从未运行'
  const parts = parseDateParts(isoStr)
  if (!parts) return '--'
  return `${parts.year}-${parts.month}-${parts.day} ${parts.hour}:${parts.minute}:${parts.second}`
}

export function formatShortTime(isoStr: string | null | undefined): string {
  if (!isoStr) return '--'
  const parts = parseDateParts(isoStr)
  if (!parts) return '--'
  return `${parts.hour}:${parts.minute}`
}

export function formatMinuteTime(isoStr: string | null | undefined): string {
  if (!isoStr) return '--'
  const parts = parseDateParts(isoStr)
  if (!parts) return typeof isoStr === 'string' ? isoStr.slice(0, 16) : '--'
  return `${parts.year}-${parts.month}-${parts.day} ${parts.hour}:${parts.minute}`
}

export function formatWeekdayMinuteTime(isoStr: string | null | undefined): string {
  if (!isoStr) return '--'

  const d = parseDate(isoStr)
  if (!d) {
    return typeof isoStr === 'string' ? isoStr : '--'
  }

  const dateText = new Intl.DateTimeFormat('zh-CN', {
    timeZone: BEIJING_TIMEZONE,
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    weekday: 'short',
    hour: '2-digit',
    minute: '2-digit',
    hour12: false,
  }).format(d)

  const normalized = dateText.replace(/\//g, '-').replace(',', '')
  return normalized.replace(/周([一二三四五六日天])/, '（周$1）')
}

export function statusTone(status: string) {
  switch (status) {
    case 'running': return 'tone-info'
    case 'completed': return 'tone-success'
    case 'failed': return 'tone-error'
    case 'error': return 'tone-error'
    default: return 'tone-idle'
  }
}

export function statusText(status: string) {
  switch (status) {
    case 'running': return '执行中'
    case 'completed': return '成功'
    case 'failed': return '失败'
    case 'pending': return '等待中'
    default: return status || '空闲'
  }
}

export function pnlClass(val: number | null | undefined): string {
  if (val == null || Number.isNaN(val)) return 'pnl-zero'
  if (val === 0) return 'pnl-zero'
  return val > 0 ? 'pnl-up' : 'pnl-down'
}

export function formatPnl(val: number | null | undefined): string {
  if (val === undefined || val === null) return '--'
  const prefix = val > 0 ? '+' : ''
  return `${prefix}${val.toFixed(2)}`
}
