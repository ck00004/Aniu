import assert from 'node:assert/strict'
import test from 'node:test'
import { readFileSync } from 'node:fs'

test('settings view renders global prompt configuration fields', () => {
  const source = readFileSync(new URL('../src/views/SettingsView.vue', import.meta.url), 'utf-8')

  assert.ok(source.includes('全局提示词配置'))
  assert.ok(source.includes('CLS API Base URL'))
  assert.ok(source.includes('资讯源诊断提示词'))
  assert.ok(source.includes('手动分析默认提示词'))
  assert.ok(source.includes('交易任务执行要求'))
  assert.ok(source.includes('资讯源诊断系统提示词'))
  assert.ok(source.includes('聊天确认附加提示词'))
})

test('chat view exposes persistent session reset entry', () => {
  const chatViewSource = readFileSync(new URL('../src/views/ChatView.vue', import.meta.url), 'utf-8')
  const sidebarSource = readFileSync(new URL('../src/components/chat/ChatSessionSidebar.vue', import.meta.url), 'utf-8')

  assert.ok(chatViewSource.includes('@reset-persistent="handleResetPersistent"'))
  assert.ok(chatViewSource.includes('resetSession: resetPersistentSession'))
  assert.ok(sidebarSource.includes('清空上下文'))
  assert.ok(sidebarSource.includes('resetPersistent'))
})