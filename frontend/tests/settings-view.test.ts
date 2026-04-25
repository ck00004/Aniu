import assert from 'node:assert/strict'
import test from 'node:test'
import { readFileSync } from 'node:fs'

test('settings view renders global prompt configuration fields', () => {
  const source = readFileSync(new URL('../src/views/SettingsView.vue', import.meta.url), 'utf-8')

  assert.ok(source.includes('全局提示词配置'))
  assert.ok(source.includes('手动分析默认提示词'))
  assert.ok(source.includes('交易任务执行要求'))
  assert.ok(source.includes('Jin10 诊断系统提示词'))
  assert.ok(source.includes('聊天确认附加提示词'))
})