import assert from 'node:assert/strict'
import test from 'node:test'
import { readFileSync } from 'node:fs'

const viewSource = () =>
  readFileSync(new URL('../src/views/MemoriesView.vue', import.meta.url), 'utf-8')

test('梦境页提供补整理指定日期的工具栏', () => {
  const source = viewSource()

  assert.ok(source.includes('补整理指定日期'))
  assert.ok(source.includes('type="date"'))
  assert.ok(source.includes(':max="backfillMaxDate"'))
  assert.ok(source.includes('handleBackfillDream'))
  // 当天不可选：避免占用当晚自动整理
  assert.ok(source.includes('当天日期不可选'))
})

test('梦境详情提供按日期重新整理入口', () => {
  const source = viewSource()

  assert.ok(source.includes('重新整理'))
  assert.ok(source.includes('handleRerunDream(dream)'))
  assert.ok(source.includes('triggerDream(dream.target_date)'))
  // 重跑前需要确认（覆盖已有整理结果）
  assert.ok(source.includes('会被本次覆盖'))
})
