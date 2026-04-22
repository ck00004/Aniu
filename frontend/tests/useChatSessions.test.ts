import assert from 'node:assert/strict'
import test from 'node:test'

import { useChatSessions } from '../src/composables/useChatSessions.ts'

function resetState() {
  const chatSessions = useChatSessions()
  chatSessions.sessions.value = []
  chatSessions.currentSessionId.value = null
  chatSessions.errorMessage.value = ''
  return chatSessions
}

test('touchSession updates local metadata and reorders the touched session to the top', () => {
  const chatSessions = resetState()

  chatSessions.sessions.value = [
    {
      id: 1,
      title: '新对话',
      created_at: '2026-04-18T00:00:00Z',
      updated_at: '2026-04-18T00:00:00Z',
      last_message_at: '2026-04-18T00:00:00Z',
      message_count: 0,
    },
    {
      id: 2,
      title: '已有会话',
      created_at: '2026-04-18T00:05:00Z',
      updated_at: '2026-04-18T00:05:00Z',
      last_message_at: '2026-04-18T00:05:00Z',
      message_count: 6,
    },
  ]

  chatSessions.touchSession(1, {
    title: '第一行标题',
    message_count: 2,
  })

  assert.equal(chatSessions.sessions.value[0]?.id, 1)
  assert.equal(chatSessions.sessions.value[0]?.title, '第一行标题')
  assert.equal(chatSessions.sessions.value[0]?.message_count, 2)
  assert.ok(chatSessions.sessions.value[0]?.last_message_at)
  assert.ok(
    new Date(chatSessions.sessions.value[0].last_message_at as string).getTime()
    >= new Date('2026-04-18T00:05:00Z').getTime(),
  )
})
