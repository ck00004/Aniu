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
