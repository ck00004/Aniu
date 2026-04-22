import { reactive } from 'vue'

import type { ScheduleConfig } from '@/types'

type ScheduleLike = Pick<ScheduleConfig, 'id' | 'name' | 'run_type' | 'cron_expression' | 'task_prompt' | 'timeout_seconds' | 'enabled'>

type ScheduleKey = 'preMarket' | 'midday' | 'postMarket' | 'night'
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

export interface ScheduleFormState {
  preMarket: { enabled: boolean; hour: number; minute: number; prompt: string }
  postMarket: { enabled: boolean; hour: number; minute: number; prompt: string }
  midday: { enabled: boolean; hour: number; minute: number; prompt: string }
  night: { enabled: boolean; hour: number; minute: number; prompt: string }
  morning: { enabled: boolean; runCount: number; prompt: string }
  afternoon: { enabled: boolean; runCount: number; prompt: string }
}

const FIXED_TASK_NAMES = {
  preMarket: '盘前分析',
  midday: '午间复盘',
  postMarket: '收盘分析',
  night: '夜间分析',
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
  morning: { enabled: true, runCount: 2, prompt: '你正在执行盘中交易操作，你的唯一目标是追求收益最大化。' },
  afternoon: { enabled: true, runCount: 2, prompt: '你正在执行盘中交易操作，你的唯一目标是追求收益最大化。' },
})

function parseCron(cronExpression: string) {
  const [minuteText = '0', hourText = '0'] = cronExpression.split(' ')
  return {
    minute: Number(minuteText),
    hour: Number(hourText),
  }
}

function buildCron(hour: number, minute: number) {
  return `${minute} ${hour} * * 1-5`
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
      const matched = schedules.find((item) => item.name === FIXED_TASK_NAMES[key])
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
      const matched = schedules
        .filter((item) => item.name.startsWith(SESSION_TASK_NAMES[key]))
        .sort((a, b) => a.cron_expression.localeCompare(b.cron_expression))

      if (matched.length === 0) {
        return
      }

      scheduleSettings[key].enabled = matched.some((item) => item.enabled)
      scheduleSettings[key].runCount = Number(matched.length)
      scheduleSettings[key].prompt = matched[0].task_prompt || scheduleSettings[key].prompt
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
        cron_expression: buildCron(current.hour, current.minute),
        task_prompt: current.prompt,
        timeout_seconds: existing?.timeout_seconds ?? DEFAULT_TIMEOUT,
        enabled: current.enabled,
      }
    })

    const sessionPayload = (Object.keys(SESSION_TASK_NAMES) as SessionKey[]).flatMap((key) => {
      const current = scheduleSettings[key]
      const existing = existingSchedules.filter((item) => item.name.startsWith(SESSION_TASK_NAMES[key]))
      return getSessionTimes(key, current.runCount).map((time, index) => ({
        id: existing[index]?.id,
        name: `${SESSION_TASK_NAMES[key]}${index + 1}号`,
        run_type: 'trade' as const,
        cron_expression: buildCron(time.hour, time.minute),
        task_prompt: current.prompt,
        timeout_seconds: existing[index]?.timeout_seconds ?? DEFAULT_TIMEOUT,
        enabled: current.enabled,
      }))
    })

    return [...fixedPayload, ...sessionPayload]
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

  function autoResizeTextarea(event: Event) {
    const textarea = event.target as HTMLTextAreaElement
    textarea.style.height = 'auto'
    textarea.style.height = `${Math.min(textarea.scrollHeight, 200)}px`
  }

  function getMorningRunTimes() {
    return getSessionTimeLabels('morning', scheduleSettings.morning.runCount)
  }

  function getAfternoonRunTimes() {
    return getSessionTimeLabels('afternoon', scheduleSettings.afternoon.runCount)
  }

  return {
    scheduleSettings,
    fixedTaskTimeOptions: FIXED_TASK_TIME_OPTIONS,
    runCountOptions: RUN_COUNT_OPTIONS,
    syncFromSchedules,
    buildPayload,
    setFixedTaskTime,
    getSectionTimeValue,
    setSectionTimeValue,
    autoResizeTextarea,
    getMorningRunTimes,
    getAfternoonRunTimes,
  }
}
