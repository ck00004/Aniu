import assert from 'node:assert/strict'
import test from 'node:test'
import { readFileSync } from 'node:fs'

test('schedule overview is rendered as a standalone panel before schedule settings panel', () => {
  const source = readFileSync(new URL('../src/views/ScheduleView.vue', import.meta.url), 'utf-8')

  const overviewIndex = source.indexOf('<section class="panel schedule-overview-wrapper">')
  const settingsIndex = source.indexOf('<section class="panel tasks-panel">')

  assert.notEqual(overviewIndex, -1)
  assert.notEqual(settingsIndex, -1)
  assert.ok(overviewIndex < settingsIndex)
})

test('analysis schedule cards provide custom time inputs', () => {
  const source = readFileSync(new URL('../src/views/ScheduleView.vue', import.meta.url), 'utf-8')

  const timeInputCount = source.match(/type="time"/g)?.length ?? 0

  assert.equal(timeInputCount, 4)
  assert.ok(source.includes('支持自定义 HH:MM'))
  assert.ok(source.includes('夜间分析'))
})
