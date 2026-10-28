import assert from 'node:assert/strict'
import test from 'node:test'

import { useScheduleForm } from '../src/composables/useScheduleForm.ts'

test('getMorningRunTimes supports select string values', () => {
  const { scheduleSettings, getMorningRunTimes } = useScheduleForm()

  scheduleSettings.morning.runCount = '3' as unknown as number

  assert.equal(getMorningRunTimes(), '09:30, 10:15, 11:00')
})

test('buildPayload creates correct morning run count from select string values', () => {
  const { scheduleSettings, buildPayload } = useScheduleForm()

  scheduleSettings.morning.runCount = '4' as unknown as number

  const payload = buildPayload([])
  const morningRuns = payload.filter((item) => item.name.startsWith('上午运行'))

  assert.equal(morningRuns.length, 4)
  assert.equal(morningRuns.every((item) => item.run_type === 'trade'), true)
})

test('buildPayload marks fixed tasks as analysis', () => {
  const { buildPayload } = useScheduleForm()

  const payload = buildPayload([])
  const fixedRuns = payload.filter((item) => ['盘前分析', '午间复盘', '收盘分析', '夜间分析'].includes(item.name))

  assert.equal(fixedRuns.length, 4)
  assert.equal(fixedRuns.every((item) => item.run_type === 'analysis'), true)
})

test('buildPayload preserves disabled session schedules', () => {
  const { scheduleSettings, syncFromSchedules, buildPayload } = useScheduleForm()

  syncFromSchedules([
    {
      id: 11,
      name: '上午运行1号',
      run_type: 'trade',
      market_day_type: 'trading_day',
      cron_expression: '0 10 * * 1-5',
      task_prompt: 'session',
      timeout_seconds: 1800,
      enabled: false,
    },
    {
      id: 12,
      name: '上午运行2号',
      run_type: 'trade',
      market_day_type: 'trading_day',
      cron_expression: '0 11 * * 1-5',
      task_prompt: 'session',
      timeout_seconds: 1800,
      enabled: false,
    },
  ])

  const payload = buildPayload([
    {
      id: 11,
      name: '上午运行1号',
      run_type: 'trade',
      market_day_type: 'trading_day',
      cron_expression: '0 10 * * 1-5',
      task_prompt: 'session',
      timeout_seconds: 1800,
      enabled: false,
    },
    {
      id: 12,
      name: '上午运行2号',
      run_type: 'trade',
      market_day_type: 'trading_day',
      cron_expression: '0 11 * * 1-5',
      task_prompt: 'session',
      timeout_seconds: 1800,
      enabled: false,
    },
  ])

  const morningRuns = payload.filter((item) => item.name.startsWith('上午运行'))
  assert.equal(morningRuns.every((item) => item.enabled === false), true)
})

test('syncFromSchedules preserves custom pre-market times', () => {
  const { scheduleSettings, syncFromSchedules } = useScheduleForm()

  syncFromSchedules([
    {
      id: 1,
      name: '盘前分析',
      run_type: 'analysis',
      market_day_type: 'trading_day',
      cron_expression: '30 7 * * 1-5',
      task_prompt: 'a',
      timeout_seconds: 1800,
      enabled: true,
    },
  ])

  assert.equal(scheduleSettings.preMarket.hour, 7)
  assert.equal(scheduleSettings.preMarket.minute, 30)
})

test('pre-market default display time is 08:00', () => {
  const { scheduleSettings } = useScheduleForm()

  assert.equal(scheduleSettings.preMarket.hour, 8)
  assert.equal(scheduleSettings.preMarket.minute, 0)
})

test('syncFromSchedules preserves legacy pre-market time instead of forcing a fixed option', () => {
  const { scheduleSettings, syncFromSchedules } = useScheduleForm()

  syncFromSchedules([
    {
      id: 9,
      name: '盘前分析',
      run_type: 'analysis',
      market_day_type: 'trading_day',
      cron_expression: '15 7 * * 1-5',
      task_prompt: 'legacy',
      timeout_seconds: 1800,
      enabled: true,
    },
  ])

  assert.equal(scheduleSettings.preMarket.hour, 7)
  assert.equal(scheduleSettings.preMarket.minute, 15)
})

test('syncFromSchedules preserves custom midday times', () => {
  const { scheduleSettings, syncFromSchedules } = useScheduleForm()

  syncFromSchedules([
    {
      id: 2,
      name: '午间复盘',
      run_type: 'analysis',
      market_day_type: 'trading_day',
      cron_expression: '45 11 * * 1-5',
      task_prompt: 'b',
      timeout_seconds: 1800,
      enabled: true,
    },
  ])

  assert.equal(scheduleSettings.midday.hour, 11)
  assert.equal(scheduleSettings.midday.minute, 45)
})

test('syncFromSchedules preserves custom post-market times', () => {
  const { scheduleSettings, syncFromSchedules } = useScheduleForm()

  syncFromSchedules([
    {
      id: 3,
      name: '收盘分析',
      run_type: 'analysis',
      market_day_type: 'trading_day',
      cron_expression: '15 16 * * 1-5',
      task_prompt: 'c',
      timeout_seconds: 1800,
      enabled: true,
    },
  ])

  assert.equal(scheduleSettings.postMarket.hour, 16)
  assert.equal(scheduleSettings.postMarket.minute, 15)
})

test('syncFromSchedules preserves custom night analysis times', () => {
  const { scheduleSettings, syncFromSchedules } = useScheduleForm()

  syncFromSchedules([
    {
      id: 4,
      name: '夜间分析',
      run_type: 'analysis',
      market_day_type: 'trading_day',
      cron_expression: '20 21 * * 1-5',
      task_prompt: 'night',
      timeout_seconds: 1800,
      enabled: true,
    },
  ])

  assert.equal(scheduleSettings.night.hour, 21)
  assert.equal(scheduleSettings.night.minute, 20)
  assert.equal(scheduleSettings.night.prompt, 'night')
})

test('setSectionTimeValue updates analysis task time from HH:MM input', () => {
  const { scheduleSettings, getSectionTimeValue, setSectionTimeValue } = useScheduleForm()

  setSectionTimeValue('postMarket', '14:07')

  assert.equal(scheduleSettings.postMarket.hour, 14)
  assert.equal(scheduleSettings.postMarket.minute, 7)
  assert.equal(getSectionTimeValue('postMarket'), '14:07')
})

test('night analysis default display time is 21:00', () => {
  const { scheduleSettings } = useScheduleForm()

  assert.equal(scheduleSettings.night.hour, 21)
  assert.equal(scheduleSettings.night.minute, 0)
})

test('buildPayload includes two non-trading analysis slots', () => {
  const { buildPayload } = useScheduleForm()

  const payload = buildPayload([])
  const nonTradingRuns = payload.filter((item) => item.market_day_type === 'non_trading_day')

  assert.equal(nonTradingRuns.length, 2)
  assert.equal(nonTradingRuns.every((item) => item.run_type === 'analysis'), true)
})

test('syncFromSchedules preserves non-trading task times', () => {
  const { scheduleSettings, syncFromSchedules } = useScheduleForm()

  syncFromSchedules([
    {
      id: 21,
      name: '非交易日分析1号',
      run_type: 'analysis',
      market_day_type: 'non_trading_day',
      cron_expression: '45 9 * * *',
      task_prompt: 'holiday',
      timeout_seconds: 1800,
      enabled: true,
    },
  ])

  assert.equal(scheduleSettings.nonTradingFirst.hour, 9)
  assert.equal(scheduleSettings.nonTradingFirst.minute, 45)
  assert.equal(scheduleSettings.nonTradingFirst.prompt, 'holiday')
})

// ---------------------------------------------------------------------------
// 交易时段手动时间机制
// ---------------------------------------------------------------------------

function makeSessionSchedule(id: number, name: string, cron: string, enabled = true) {
  return {
    id,
    name,
    run_type: 'trade' as const,
    market_day_type: 'trading_day' as const,
    cron_expression: cron,
    task_prompt: 'session',
    timeout_seconds: 1800,
    enabled,
  }
}

test('setSessionTimeMode 切到手动时以当前自动时间初始化列表', () => {
  const { scheduleSettings, setSessionTimeMode, getSessionManualTimes } = useScheduleForm()

  scheduleSettings.morning.runCount = 3
  setSessionTimeMode('morning', 'manual')

  assert.deepEqual(getSessionManualTimes('morning'), ['09:30', '10:15', '11:00'])
  assert.equal(scheduleSettings.morning.timeMode, 'manual')
})

test('手动模式 buildPayload 使用自定义时间（排序去重）', () => {
  const { scheduleSettings, buildPayload } = useScheduleForm()

  scheduleSettings.morning.timeMode = 'manual'
  scheduleSettings.morning.manualTimes = ['11:00', '09:45', '10:30', '09:45']

  const morningRuns = buildPayload([])
    .filter((item) => item.name.startsWith('上午运行'))
    .sort((a, b) => a.name.localeCompare(b.name, 'zh'))

  assert.equal(morningRuns.length, 3)
  assert.equal(morningRuns[0].cron_expression, '45 9 * * 1-5')
  assert.equal(morningRuns[1].cron_expression, '30 10 * * 1-5')
  assert.equal(morningRuns[2].cron_expression, '0 11 * * 1-5')
})

test('手动模式空列表回退自动模板，避免误清空任务', () => {
  const { scheduleSettings, buildPayload } = useScheduleForm()

  scheduleSettings.afternoon.timeMode = 'manual'
  scheduleSettings.afternoon.manualTimes = []
  scheduleSettings.afternoon.runCount = 2

  const afternoonRuns = buildPayload([]).filter((item) => item.name.startsWith('下午运行'))
  assert.equal(afternoonRuns.length, 2)
})

test('syncFromSchedules 自动模板时间 → auto 模式', () => {
  const { scheduleSettings, syncFromSchedules } = useScheduleForm()

  syncFromSchedules([
    makeSessionSchedule(21, '上午运行1号', '30 9 * * 1-5'),
    makeSessionSchedule(22, '上午运行2号', '15 10 * * 1-5'),
    makeSessionSchedule(23, '上午运行3号', '0 11 * * 1-5'),
  ])

  assert.equal(scheduleSettings.morning.timeMode, 'auto')
  assert.deepEqual(scheduleSettings.morning.manualTimes, [])
  assert.equal(scheduleSettings.morning.runCount, 3)
})

test('syncFromSchedules 自定义时间 → manual 模式并保留实际时间', () => {
  const { scheduleSettings, syncFromSchedules, getSessionManualTimes } = useScheduleForm()

  syncFromSchedules([
    makeSessionSchedule(31, '上午运行1号', '40 9 * * 1-5'),
    makeSessionSchedule(32, '上午运行2号', '20 10 * * 1-5'),
  ])

  assert.equal(scheduleSettings.morning.timeMode, 'manual')
  assert.deepEqual(getSessionManualTimes('morning'), ['09:40', '10:20'])
})

test('syncFromSchedules 超过 4 个自定义时间 → manual 模式且数量保留', () => {
  const { scheduleSettings, syncFromSchedules, getSessionManualTimes } = useScheduleForm()

  syncFromSchedules([
    makeSessionSchedule(41, '下午运行1号', '5 13 * * 1-5'),
    makeSessionSchedule(42, '下午运行2号', '15 13 * * 1-5'),
    makeSessionSchedule(43, '下午运行3号', '25 13 * * 1-5'),
    makeSessionSchedule(44, '下午运行4号', '35 13 * * 1-5'),
    makeSessionSchedule(45, '下午运行5号', '45 13 * * 1-5'),
    makeSessionSchedule(46, '下午运行6号', '55 13 * * 1-5'),
  ])

  assert.equal(scheduleSettings.afternoon.timeMode, 'manual')
  assert.equal(getSessionManualTimes('afternoon').length, 6)
  assert.equal(scheduleSettings.afternoon.runCount, 6)
})

test('syncFromSchedules 按解析时间排序（修复 cron 字符串排序错序）', () => {
  const { syncFromSchedules, getSessionManualTimes } = useScheduleForm()

  // 10:00 的 cron "0 10" 按字符串排序会排在 "45 9"（9:45）之前
  syncFromSchedules([
    makeSessionSchedule(51, '上午运行1号', '0 10 * * 1-5'),
    makeSessionSchedule(52, '上午运行2号', '45 9 * * 1-5'),
  ])

  assert.deepEqual(getSessionManualTimes('morning'), ['09:45', '10:00'])
})

test('validateSessionTimes 校验窗口/重复/空值/格式', () => {
  const { scheduleSettings, validateSessionTimes } = useScheduleForm()

  scheduleSettings.morning.timeMode = 'manual'
  scheduleSettings.morning.manualTimes = ['09:14']
  assert.match(validateSessionTimes('morning') || '', /超出上午交易时段/)

  // 09:15 起含集合竞价，属合法窗口
  scheduleSettings.morning.manualTimes = ['09:15', '09:25']
  assert.equal(validateSessionTimes('morning'), null)

  scheduleSettings.morning.manualTimes = ['11:40']
  assert.match(validateSessionTimes('morning') || '', /超出上午交易时段/)

  scheduleSettings.morning.manualTimes = ['10:00', '10:00']
  assert.match(validateSessionTimes('morning') || '', /重复/)

  scheduleSettings.morning.manualTimes = []
  assert.match(validateSessionTimes('morning') || '', /至少添加一个/)

  scheduleSettings.morning.manualTimes = ['10:00', '']
  assert.match(validateSessionTimes('morning') || '', /格式无效/)

  scheduleSettings.morning.manualTimes = ['09:35', '10:05', '11:25']
  assert.equal(validateSessionTimes('morning'), null)

  // auto 模式不校验
  scheduleSettings.morning.timeMode = 'auto'
  assert.equal(validateSessionTimes('morning'), null)

  // 下午窗口 13:00-15:00
  scheduleSettings.afternoon.timeMode = 'manual'
  scheduleSettings.afternoon.manualTimes = ['12:50']
  assert.match(validateSessionTimes('afternoon') || '', /超出下午交易时段/)
  scheduleSettings.afternoon.manualTimes = ['13:00', '14:59']
  assert.equal(validateSessionTimes('afternoon'), null)
})

test('addSessionManualTime 追加并遵守上限，removeSessionManualTime 删除', () => {
  const { scheduleSettings, addSessionManualTime, removeSessionManualTime, getSessionManualTimes } = useScheduleForm()

  scheduleSettings.morning.timeMode = 'manual'
  scheduleSettings.morning.manualTimes = []

  addSessionManualTime('morning')
  assert.equal(getSessionManualTimes('morning').length, 1)
  // 空列表时新条目取窗口中点（09:15-11:30 窗口 → 10:15）
  assert.equal(getSessionManualTimes('morning')[0], '10:15')

  addSessionManualTime('morning')
  // 上一条 +30 分钟
  assert.equal(getSessionManualTimes('morning')[1], '10:45')
  // 第三条 10:45+30=11:15，仍在窗口内
  addSessionManualTime('morning')
  assert.equal(getSessionManualTimes('morning')[2], '11:15')

  removeSessionManualTime('morning', 0)
  assert.deepEqual(getSessionManualTimes('morning'), ['10:45', '11:15'])

  // 上限保护：上午 10（2026-10 调整，原 8）
  scheduleSettings.morning.manualTimes = ['09:15', '09:20', '09:30', '09:35', '09:40', '09:45', '10:00', '10:05', '10:10', '10:15']
  addSessionManualTime('morning')
  assert.equal(getSessionManualTimes('morning').length, 10)
})

test('validateSessionTimes 上午上限 10、下午上限 8', () => {
  const { scheduleSettings, validateSessionTimes } = useScheduleForm()

  scheduleSettings.morning.timeMode = 'manual'
  scheduleSettings.morning.manualTimes = Array.from({ length: 10 }, (_, i) => `09:${String(15 + i).padStart(2, '0')}`)
  assert.equal(validateSessionTimes('morning'), null)
  scheduleSettings.morning.manualTimes.push('11:00')
  assert.match(validateSessionTimes('morning') || '', /最多 10 个/)

  scheduleSettings.afternoon.timeMode = 'manual'
  scheduleSettings.afternoon.manualTimes = Array.from({ length: 8 }, (_, i) => `13:${String(i * 5).padStart(2, '0')}`)
  assert.equal(validateSessionTimes('afternoon'), null)
  scheduleSettings.afternoon.manualTimes.push('14:30')
  assert.match(validateSessionTimes('afternoon') || '', /最多 8 个/)
})

test('getSessionRunTimes 两种模式分别返回模板/手动时间', () => {
  const { scheduleSettings, getSessionRunTimes } = useScheduleForm()

  scheduleSettings.morning.runCount = 2
  assert.equal(getSessionRunTimes('morning'), '10:00, 11:00')

  scheduleSettings.morning.timeMode = 'manual'
  scheduleSettings.morning.manualTimes = ['09:35', '10:40']
  assert.equal(getSessionRunTimes('morning'), '09:35, 10:40')
})
