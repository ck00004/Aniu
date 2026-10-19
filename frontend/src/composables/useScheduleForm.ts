import { reactive } from 'vue'

import type { ScheduleConfig } from '@/types'

type ScheduleLike = Pick<ScheduleConfig, 'id' | 'name' | 'run_type' | 'market_day_type' | 'cron_expression' | 'task_prompt' | 'timeout_seconds' | 'enabled'>

type ScheduleKey = 'preMarket' | 'midday' | 'postMarket' | 'night'
type NonTradingKey = 'nonTradingFirst' | 'nonTradingSecond'
type SessionKey = 'morning' | 'afternoon'

type FixedTaskTimeOption = {
  hour: number
  minute: number
  label: string
}

export const FIXED_TASK_TIME_OPTIONS = {
  preMarket: {
    options: [
      { hour: 8, minute: 0, label: '08:00' },
      { hour: 8, minute: 15, label: '08:15' },
      { hour: 8, minute: 30, label: '08:30' },
      { hour: 8, minute: 45, label: '08:45' },
    ] as FixedTaskTimeOption[],
  },
  midday: {
    options: [
      { hour: 12, minute: 0, label: '12:00' },
      { hour: 12, minute: 15, label: '12:15' },
      { hour: 12, minute: 30, label: '12:30' },
      { hour: 12, minute: 45, label: '12:45' },
    ] as FixedTaskTimeOption[],
  },
  postMarket: {
    options: [
      { hour: 15, minute: 15, label: '15:15' },
      { hour: 15, minute: 30, label: '15:30' },
      { hour: 15, minute: 45, label: '15:45' },
      { hour: 16, minute: 0, label: '16:00' },
    ] as FixedTaskTimeOption[],
  },
  night: {
    options: [
      { hour: 20, minute: 0, label: '20:00' },
      { hour: 20, minute: 30, label: '20:30' },
      { hour: 21, minute: 0, label: '21:00' },
      { hour: 21, minute: 30, label: '21:30' },
    ] as FixedTaskTimeOption[],
  },
} as const

export const RUN_COUNT_OPTIONS = [1, 2, 3, 4] as const

export type SessionTimeMode = 'auto' | 'manual'

// 手动模式的次数上限（自动模式为 1-4；手动通过增删时间条目调整次数）
export const MAX_MANUAL_TIMES = 8

// 交易时段窗口（手动时间必须落在窗口内，否则交易运行会被交易时段门禁拦截）
export const SESSION_WINDOWS: Record<SessionKey, { start: { hour: number; minute: number }; end: { hour: number; minute: number }; label: string }> = {
  morning: { start: { hour: 9, minute: 30 }, end: { hour: 11, minute: 30 }, label: '09:30 - 11:30' },
  afternoon: { start: { hour: 13, minute: 0 }, end: { hour: 15, minute: 0 }, label: '13:00 - 15:00' },
}

export interface ScheduleFormState {
  preMarket: { enabled: boolean; hour: number; minute: number; prompt: string }
  postMarket: { enabled: boolean; hour: number; minute: number; prompt: string }
  midday: { enabled: boolean; hour: number; minute: number; prompt: string }
  night: { enabled: boolean; hour: number; minute: number; prompt: string }
  nonTradingFirst: { enabled: boolean; hour: number; minute: number; prompt: string }
  nonTradingSecond: { enabled: boolean; hour: number; minute: number; prompt: string }
  morning: { enabled: boolean; runCount: number; prompt: string; timeMode: SessionTimeMode; manualTimes: string[] }
  afternoon: { enabled: boolean; runCount: number; prompt: string; timeMode: SessionTimeMode; manualTimes: string[] }
}

const FIXED_TASK_NAMES = {
  preMarket: '盘前分析',
  midday: '午间复盘',
  postMarket: '收盘分析',
  night: '夜间分析',
} as const

const NON_TRADING_TASK_NAMES = {
  nonTradingFirst: '非交易日分析1号',
  nonTradingSecond: '非交易日分析2号',
} as const

const SESSION_TASK_NAMES = {
  morning: '上午运行',
  afternoon: '下午运行',
} as const

const DEFAULT_TIMEOUT = 1800

const defaultState = (): ScheduleFormState => ({
  preMarket: { enabled: false, hour: 8, minute: 0, prompt: '你正在执行盘前分析任务，请分析今日市场情况和持仓情况，做好今日市场走势预测，为你决策交易做好准备。' },
  postMarket: { enabled: false, hour: 15, minute: 30, prompt: '你正在执行收盘分析任务，请对今日市场和交易操作进行全面复盘，总结今日市场和明日可能的走势。' },
  midday: { enabled: false, hour: 12, minute: 0, prompt: '你正在执行午间复盘任务，请对上午市场和交易操作进行复盘，做好下午市场走势预测，为你决策交易做好准备。' },
  night: { enabled: false, hour: 21, minute: 0, prompt: '你正在执行夜间分析任务，请结合收盘结果、盘后资讯、政策消息与题材演化，梳理次日重点方向、风险点和跟踪计划。' },
  nonTradingFirst: { enabled: false, hour: 9, minute: 30, prompt: '你正在执行非交易日分析任务，请复盘最新市场环境、消息面与持仓状态，为下一个交易日的首次分析准备重点结论。' },
  nonTradingSecond: { enabled: false, hour: 20, minute: 0, prompt: '你正在执行非交易日补充分析任务，请补充梳理非交易日新增信息、情绪变化和次日需要重点跟踪的交易线索。' },
  morning: { enabled: true, runCount: 2, prompt: '你正在执行盘中交易操作，你的唯一目标是追求收益最大化。', timeMode: 'auto', manualTimes: [] },
  afternoon: { enabled: true, runCount: 2, prompt: '你正在执行盘中交易操作，你的唯一目标是追求收益最大化。', timeMode: 'auto', manualTimes: [] },
})

function parseCron(cronExpression: string) {
  const [minuteText = '0', hourText = '0'] = cronExpression.split(' ')
  return {
    minute: Number(minuteText),
    hour: Number(hourText),
  }
}

function buildCron(hour: number, minute: number, marketDayType: 'trading_day' | 'non_trading_day' = 'trading_day') {
  return `${minute} ${hour} * * ${marketDayType === 'trading_day' ? '1-5' : '*'}`
}

function isValidHourMinute(hour: number, minute: number) {
  return Number.isInteger(hour) && Number.isInteger(minute) && hour >= 0 && hour <= 23 && minute >= 0 && minute <= 59
}

function formatTimeValue(hour: number, minute: number) {
  return `${String(hour).padStart(2, '0')}:${String(minute).padStart(2, '0')}`
}

function parseTimeValue(value: string) {
  const match = /^(\d{2}):(\d{2})$/.exec(value.trim())
  if (!match) {
    return null
  }

  const hour = Number(match[1])
  const minute = Number(match[2])
  return isValidHourMinute(hour, minute) ? { hour, minute } : null
}

function getSessionTimes(session: SessionKey, runCount: number) {
  const count = Number(runCount)

  if (session === 'morning') {
    switch (count) {
      case 1:
        return [{ hour: 10, minute: 30 }]
      case 2:
        return [{ hour: 10, minute: 0 }, { hour: 11, minute: 0 }]
      case 3:
        return [{ hour: 9, minute: 30 }, { hour: 10, minute: 15 }, { hour: 11, minute: 0 }]
      case 4:
        return [{ hour: 9, minute: 30 }, { hour: 10, minute: 0 }, { hour: 10, minute: 30 }, { hour: 11, minute: 0 }]
      default:
        return [{ hour: 10, minute: 0 }, { hour: 11, minute: 0 }]
    }
  }

  switch (count) {
    case 1:
      return [{ hour: 14, minute: 0 }]
    case 2:
      return [{ hour: 13, minute: 30 }, { hour: 14, minute: 30 }]
    case 3:
      return [{ hour: 13, minute: 0 }, { hour: 13, minute: 45 }, { hour: 14, minute: 30 }]
    case 4:
      return [{ hour: 13, minute: 0 }, { hour: 13, minute: 30 }, { hour: 14, minute: 0 }, { hour: 14, minute: 30 }]
    default:
      return [{ hour: 13, minute: 30 }, { hour: 14, minute: 30 }]
  }
}

function getSessionTimeLabels(session: SessionKey, runCount: number) {
  return getSessionTimes(session, runCount)
    .map(({ hour, minute }) => `${String(hour).padStart(2, '0')}:${String(minute).padStart(2, '0')}`)
    .join(', ')
}

export function useScheduleForm() {
  const scheduleSettings = reactive<ScheduleFormState>(defaultState())

  function syncFromSchedules(schedules: ScheduleLike[]) {
    Object.assign(scheduleSettings, defaultState())

    ;(Object.keys(FIXED_TASK_NAMES) as ScheduleKey[]).forEach((key) => {
      const matched = schedules.find((item) => item.name === FIXED_TASK_NAMES[key] && item.market_day_type !== 'non_trading_day')
      if (!matched) {
        return
      }

      const { hour, minute } = parseCron(matched.cron_expression)
      scheduleSettings[key].enabled = matched.enabled
      if (isValidHourMinute(hour, minute)) {
        scheduleSettings[key].hour = hour
        scheduleSettings[key].minute = minute
      }
      scheduleSettings[key].prompt = matched.task_prompt || scheduleSettings[key].prompt
    })

    ;(Object.keys(NON_TRADING_TASK_NAMES) as NonTradingKey[]).forEach((key) => {
      const matched = schedules.find((item) => item.name === NON_TRADING_TASK_NAMES[key] || (item.market_day_type === 'non_trading_day' && item.name === NON_TRADING_TASK_NAMES[key]))
      if (!matched) {
        return
      }

      const { hour, minute } = parseCron(matched.cron_expression)
      scheduleSettings[key].enabled = matched.enabled
      if (isValidHourMinute(hour, minute)) {
        scheduleSettings[key].hour = hour
        scheduleSettings[key].minute = minute
      }
      scheduleSettings[key].prompt = matched.task_prompt || scheduleSettings[key].prompt
    })

    ;(Object.keys(SESSION_TASK_NAMES) as SessionKey[]).forEach((key) => {
      // 按解析后的时间排序（旧实现按 cron 字符串排序，分钟在前会导致
      // 10:00 排在 9:45 之前，时间错序）
      const matched = schedules
        .filter((item) => item.name.startsWith(SESSION_TASK_NAMES[key]) && item.market_day_type !== 'non_trading_day')
        .map((item) => {
          const { hour, minute } = parseCron(item.cron_expression)
          return { item, hour, minute }
        })
        .filter(({ hour, minute }) => isValidHourMinute(hour, minute))
        .sort((a, b) => (a.hour - b.hour) || (a.minute - b.minute))

      if (matched.length === 0) {
        return
      }

      scheduleSettings[key].enabled = matched.some(({ item }) => item.enabled)
      scheduleSettings[key].runCount = Number(matched.length)
      scheduleSettings[key].prompt = matched[0].item.task_prompt || scheduleSettings[key].prompt

      // 实际时间与自动模板一致 → auto 模式；否则 → manual 模式并保留实际时间
      const actualTimes = matched.map(({ hour, minute }) => formatTimeValue(hour, minute))
      const autoTimes = getSessionTimes(key, matched.length)
        .map(({ hour, minute }) => formatTimeValue(hour, minute))
      const isAuto = actualTimes.length === autoTimes.length
        && actualTimes.every((time, index) => time === autoTimes[index])
      scheduleSettings[key].timeMode = isAuto ? 'auto' : 'manual'
      scheduleSettings[key].manualTimes = isAuto ? [] : actualTimes
    })
  }

  function buildPayload(existingSchedules: ScheduleLike[]) {
    const fixedPayload = (Object.keys(FIXED_TASK_NAMES) as ScheduleKey[]).map((key) => {
      const existing = existingSchedules.find((item) => item.name === FIXED_TASK_NAMES[key])
      const current = scheduleSettings[key]
      return {
        id: existing?.id,
        name: FIXED_TASK_NAMES[key],
        run_type: 'analysis' as const,
        market_day_type: 'trading_day' as const,
        cron_expression: buildCron(current.hour, current.minute, 'trading_day'),
        task_prompt: current.prompt,
        timeout_seconds: existing?.timeout_seconds ?? DEFAULT_TIMEOUT,
        enabled: current.enabled,
      }
    })

    const nonTradingPayload = (Object.keys(NON_TRADING_TASK_NAMES) as NonTradingKey[]).map((key) => {
      const existing = existingSchedules.find((item) => item.name === NON_TRADING_TASK_NAMES[key] || (item.market_day_type === 'non_trading_day' && item.name === NON_TRADING_TASK_NAMES[key]))
      const current = scheduleSettings[key]
      return {
        id: existing?.id,
        name: NON_TRADING_TASK_NAMES[key],
        run_type: 'analysis' as const,
        market_day_type: 'non_trading_day' as const,
        cron_expression: buildCron(current.hour, current.minute, 'non_trading_day'),
        task_prompt: current.prompt,
        timeout_seconds: existing?.timeout_seconds ?? DEFAULT_TIMEOUT,
        enabled: current.enabled,
      }
    })

    const sessionPayload = (Object.keys(SESSION_TASK_NAMES) as SessionKey[]).flatMap((key) => {
      const current = scheduleSettings[key]
      const existing = existingSchedules
        .filter((item) => item.name.startsWith(SESSION_TASK_NAMES[key]) && item.market_day_type !== 'non_trading_day')
        .map((item) => {
          const { hour, minute } = parseCron(item.cron_expression)
          return { item, hour, minute }
        })
        .filter(({ hour, minute }) => isValidHourMinute(hour, minute))
        .sort((a, b) => (a.hour - b.hour) || (a.minute - b.minute))

      // manual 模式用手动时间（排序去重）；空/无效时回退自动模板，
      // 避免误清空该时段全部任务
      const manualParsed = (current.timeMode === 'manual' ? current.manualTimes : [])
        .map((value) => parseTimeValue(value))
        .filter((parsed): parsed is { hour: number; minute: number } => parsed !== null)
      const uniqueManual = new Map(
        manualParsed.map(({ hour, minute }) => [`${hour}:${minute}`, { hour, minute }] as const),
      )
      const times = uniqueManual.size > 0
        ? [...uniqueManual.values()].sort((a, b) => (a.hour - b.hour) || (a.minute - b.minute))
        : getSessionTimes(key, current.runCount)

      return times.map((time, index) => ({
        id: existing[index]?.item.id,
        name: `${SESSION_TASK_NAMES[key]}${index + 1}号`,
        run_type: 'trade' as const,
        market_day_type: 'trading_day' as const,
        cron_expression: buildCron(time.hour, time.minute, 'trading_day'),
        task_prompt: current.prompt,
        timeout_seconds: existing[index]?.item.timeout_seconds ?? DEFAULT_TIMEOUT,
        enabled: current.enabled,
      }))
    })

    return [...fixedPayload, ...nonTradingPayload, ...sessionPayload]
  }

  function setFixedTaskTime(section: ScheduleKey, option: FixedTaskTimeOption) {
    scheduleSettings[section].hour = option.hour
    scheduleSettings[section].minute = option.minute
  }

  function getSectionTimeValue(section: ScheduleKey) {
    return formatTimeValue(scheduleSettings[section].hour, scheduleSettings[section].minute)
  }

  function setSectionTimeValue(section: ScheduleKey, value: string) {
    const parsed = parseTimeValue(value)
    if (!parsed) {
      return
    }
    scheduleSettings[section].hour = parsed.hour
    scheduleSettings[section].minute = parsed.minute
  }

  function getNonTradingSectionTimeValue(section: NonTradingKey) {
    return formatTimeValue(scheduleSettings[section].hour, scheduleSettings[section].minute)
  }

  function setNonTradingSectionTimeValue(section: NonTradingKey, value: string) {
    const parsed = parseTimeValue(value)
    if (!parsed) {
      return
    }
    scheduleSettings[section].hour = parsed.hour
    scheduleSettings[section].minute = parsed.minute
  }

  function autoResizeTextarea(event: Event) {
    const textarea = event.target as HTMLTextAreaElement
    textarea.style.height = 'auto'
    textarea.style.height = `${Math.min(textarea.scrollHeight, 200)}px`
  }

  function getMorningRunTimes() {
    return getSessionRunTimes('morning')
  }

  function getAfternoonRunTimes() {
    return getSessionRunTimes('afternoon')
  }

  // ---- 交易时段手动时间机制 ----

  /** 当前时段生效的时间标签（auto 用模板，manual 用手动列表）。 */
  function getSessionRunTimes(section: SessionKey) {
    const current = scheduleSettings[section]
    if (current.timeMode === 'manual') {
      return current.manualTimes.filter((time) => parseTimeValue(time) !== null).join(', ')
    }
    return getSessionTimeLabels(section, current.runCount)
  }

  /** 切换时间模式；切到手动时以当前生效时间初始化列表，避免从空白开始。 */
  function setSessionTimeMode(section: SessionKey, mode: SessionTimeMode) {
    const current = scheduleSettings[section]
    if (current.timeMode === mode) {
      return
    }
    current.timeMode = mode
    if (mode === 'manual' && current.manualTimes.length === 0) {
      current.manualTimes = getSessionTimes(section, current.runCount)
        .map(({ hour, minute }) => formatTimeValue(hour, minute))
    }
  }

  function getSessionManualTimes(section: SessionKey) {
    return scheduleSettings[section].manualTimes
  }

  /** 更新手动时间条目；无效输入原样保留，由 validate 在保存时拦截。 */
  function setSessionManualTime(section: SessionKey, index: number, value: string) {
    const times = scheduleSettings[section].manualTimes
    if (index < 0 || index >= times.length) {
      return
    }
    times[index] = value.trim()
  }

  function addSessionManualTime(section: SessionKey) {
    const times = scheduleSettings[section].manualTimes
    if (times.length >= MAX_MANUAL_TIMES) {
      return
    }
    // 新条目默认取窗口中点（或最后一条 +30 分钟，超窗回落到窗口中点）
    const window = SESSION_WINDOWS[section]
    const last = times
      .map((value) => parseTimeValue(value))
      .filter((parsed): parsed is { hour: number; minute: number } => parsed !== null)
      .sort((a, b) => (a.hour - b.hour) || (a.minute - b.minute))
    const lastTime = last.length > 0 ? last[last.length - 1] : undefined
    let candidate = { hour: Math.floor((window.start.hour + window.end.hour) / 2), minute: window.start.minute }
    if (lastTime) {
      const total = lastTime.hour * 60 + lastTime.minute + 30
      const clamped = Math.min(Math.max(total, window.start.hour * 60 + window.start.minute), window.end.hour * 60 + window.end.minute)
      candidate = { hour: Math.floor(clamped / 60), minute: clamped % 60 }
    }
    times.push(formatTimeValue(candidate.hour, candidate.minute))
  }

  function removeSessionManualTime(section: SessionKey, index: number) {
    const times = scheduleSettings[section].manualTimes
    if (index < 0 || index >= times.length) {
      return
    }
    times.splice(index, 1)
  }

  /** 校验手动时间列表；返回错误文案，null 表示通过。 */
  function validateSessionTimes(section: SessionKey): string | null {
    const current = scheduleSettings[section]
    if (current.timeMode !== 'manual') {
      return null
    }
    if (current.manualTimes.length === 0) {
      return '请至少添加一个运行时间。'
    }
    if (current.manualTimes.length > MAX_MANUAL_TIMES) {
      return `运行时间最多 ${MAX_MANUAL_TIMES} 个。`
    }
    const window = SESSION_WINDOWS[section]
    const seen = new Set<string>()
    for (const raw of current.manualTimes) {
      const parsed = parseTimeValue(raw)
      if (parsed === null) {
        return `时间格式无效：${raw || '（空）'}，请使用 HH:MM。`
      }
      const total = parsed.hour * 60 + parsed.minute
      const start = window.start.hour * 60 + window.start.minute
      const end = window.end.hour * 60 + window.end.minute
      if (total < start || total > end) {
        return `${raw} 超出${section === 'morning' ? '上午' : '下午'}交易时段（${window.label}）。`
      }
      if (seen.has(raw)) {
        return `存在重复时间：${raw}。`
      }
      seen.add(raw)
    }
    return null
  }

  return {
    scheduleSettings,
    fixedTaskTimeOptions: FIXED_TASK_TIME_OPTIONS,
    runCountOptions: RUN_COUNT_OPTIONS,
    sessionWindows: SESSION_WINDOWS,
    maxManualTimes: MAX_MANUAL_TIMES,
    syncFromSchedules,
    buildPayload,
    setFixedTaskTime,
    getSectionTimeValue,
    setSectionTimeValue,
    getNonTradingSectionTimeValue,
    setNonTradingSectionTimeValue,
    autoResizeTextarea,
    getMorningRunTimes,
    getAfternoonRunTimes,
    getSessionRunTimes,
    setSessionTimeMode,
    getSessionManualTimes,
    setSessionManualTime,
    addSessionManualTime,
    removeSessionManualTime,
    validateSessionTimes,
  }
}
