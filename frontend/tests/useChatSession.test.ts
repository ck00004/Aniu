import assert from 'node:assert/strict'
import test from 'node:test'
import { nextTick } from 'vue'

import { api } from '../src/services/api.ts'
import { useChatSession } from '../src/composables/useChatSession.ts'

type StreamEvent = {
  type: string
  [key: string]: unknown
}

class MemoryStorage implements Storage {
  private readonly store = new Map<string, string>()

  get length() {
    return this.store.size
  }

  clear() {
    this.store.clear()
  }

  getItem(key: string) {
    return this.store.has(key) ? this.store.get(key)! : null
  }

  key(index: number) {
    return Array.from(this.store.keys())[index] ?? null
  }

  removeItem(key: string) {
    this.store.delete(key)
  }

  setItem(key: string, value: string) {
    this.store.set(key, value)
  }
}

function createSseResponse(events: StreamEvent[]): Response {
  const encoder = new TextEncoder()
  const payload = events
    .map((event) => `event: ${event.type}\ndata: ${JSON.stringify(event)}\n\n`)
    .join('')

  const stream = new ReadableStream<Uint8Array>({
    start(controller) {
      controller.enqueue(encoder.encode(payload))
      controller.close()
    },
  })

  return new Response(stream, {
    status: 200,
    headers: {
      'Content-Type': 'text/event-stream',
    },
  })
}

function installBrowserMocks() {
  const storage = new MemoryStorage()
  const originalFetch = globalThis.fetch
  const originalWindow = globalThis.window
  const originalLocalStorage = globalThis.localStorage

  Object.defineProperty(globalThis, 'window', {
    configurable: true,
    value: globalThis,
  })

  Object.defineProperty(globalThis, 'localStorage', {
    configurable: true,
    value: storage,
  })

  Object.defineProperty(globalThis, 'location', {
    configurable: true,
    value: {
      pathname: '/chat',
      href: '/chat',
    },
  })

  return {
    setFetchResponse(events: StreamEvent[]) {
      globalThis.fetch = async () => createSseResponse(events)
    },
    restore() {
      Object.defineProperty(globalThis, 'window', {
        configurable: true,
        value: originalWindow,
      })

      Object.defineProperty(globalThis, 'localStorage', {
        configurable: true,
        value: originalLocalStorage,
      })

      globalThis.fetch = originalFetch
    },
  }
}

test('sendMessage 用 completed 事件替换“正在思考”占位文本', async () => {
  const browser = installBrowserMocks()

  try {
    browser.setFetchResponse([
      { type: 'llm_request', iteration: 1 },
      { type: 'completed', message: '最终回复' },
    ])

    const chat = useChatSession()
    chat.activeSessionId.value = 1
    chat.input.value = '你好'

    await chat.sendMessage()
    await nextTick()

    assert.equal(chat.errorMessage.value, '')
    assert.equal(chat.messages.value.at(-1)?.role, 'assistant')
    assert.equal(chat.messages.value.at(-1)?.content, '最终回复')
  } finally {
    browser.restore()
  }
})

test('sendMessage 支持 final_delta / final_finished 流式最终答案事件', async () => {
  const browser = installBrowserMocks()

  try {
    browser.setFetchResponse([
      { type: 'llm_request', iteration: 1 },
      { type: 'final_started' },
      { type: 'final_delta', delta: '你' },
      { type: 'final_delta', delta: '好' },
      { type: 'final_finished', content: '你好' },
    ])

    const chat = useChatSession()
    chat.activeSessionId.value = 1
    chat.input.value = '你好'

    await chat.sendMessage()
    await nextTick()

    assert.equal(chat.errorMessage.value, '')
    assert.equal(chat.messages.value.at(-1)?.role, 'assistant')
    assert.equal(chat.messages.value.at(-1)?.content, '你好')
  } finally {
    browser.restore()
  }
})

test('sendMessage 流式输出时不会在每个 delta 重建 messages 数组', async () => {
  const browser = installBrowserMocks()

  try {
    browser.setFetchResponse([
      { type: 'final_started' },
      { type: 'final_delta', delta: 'A' },
      { type: 'final_delta', delta: 'B' },
      { type: 'final_finished', content: 'AB' },
    ])

    const chat = useChatSession()
    chat.activeSessionId.value = 1
    chat.input.value = 'test'

    const pending = chat.sendMessage()
    const messagesRef = chat.messages.value
    await pending
    await nextTick()

    assert.equal(chat.messages.value, messagesRef)
    assert.equal(chat.messages.value.at(-1)?.content, 'AB')
  } finally {
    browser.restore()
  }
})

test('sendMessage 遇到 failed 事件时保留已发送消息并显示失败助手消息', async () => {
  const browser = installBrowserMocks()

  try {
    browser.setFetchResponse([
      { type: 'llm_request', iteration: 1 },
      { type: 'final_started' },
      { type: 'final_delta', delta: 'Partial answer' },
      { type: 'failed', message: 'model unavailable' },
    ])

    const chat = useChatSession()
    chat.activeSessionId.value = 1
    chat.input.value = '请继续'

    const result = await chat.sendMessage()
    await nextTick()

    assert.deepEqual(result, { sessionId: 1 })
    assert.equal(chat.errorMessage.value, 'model unavailable')
    assert.equal(chat.messages.value.length, 2)
    assert.equal(chat.messages.value[0]?.role, 'user')
    assert.equal(chat.messages.value[0]?.content, '请继续')
    assert.equal(chat.messages.value[1]?.role, 'assistant')
    assert.equal(
      chat.messages.value[1]?.content,
      'Partial answer\n\n执行失败：model unavailable',
    )
    assert.equal(chat.input.value, '')
    assert.equal(chat.pendingAttachments.value.length, 0)
  } finally {
    browser.restore()
  }
})

test('loadOlderMessages 会在当前消息前方追加更早分页内容', async () => {
  const originalGetChatSessionMessages = api.getChatSessionMessages

  try {
    const calls: Array<{ sessionId: number, beforeId?: number }> = []
    api.getChatSessionMessages = (async (sessionId: number, options = {}) => {
      calls.push({ sessionId, beforeId: options.beforeId })
      if (calls.length === 1) {
        return {
          session: {
            id: sessionId,
            title: 'Paged Session',
            created_at: '2026-04-18T00:00:00Z',
            updated_at: '2026-04-18T00:00:00Z',
            last_message_at: '2026-04-18T00:00:00Z',
            message_count: 5,
          },
          messages: [
            { id: 4, role: 'assistant', content: 'message-4' },
            { id: 5, role: 'user', content: 'message-5' },
          ],
          has_more: true,
          next_before_id: 4,
        }
      }
      return {
        session: {
          id: sessionId,
          title: 'Paged Session',
          created_at: '2026-04-18T00:00:00Z',
          updated_at: '2026-04-18T00:00:00Z',
          last_message_at: '2026-04-18T00:00:00Z',
          message_count: 5,
        },
        messages: [
          { id: 2, role: 'assistant', content: 'message-2' },
          { id: 3, role: 'user', content: 'message-3' },
        ],
        has_more: true,
        next_before_id: 2,
      }
    }) as typeof api.getChatSessionMessages

    const chat = useChatSession()
    await chat.loadSession(7)
    await chat.loadOlderMessages()

    assert.deepEqual(calls, [
      { sessionId: 7, beforeId: undefined },
      { sessionId: 7, beforeId: 4 },
    ])
    assert.deepEqual(
      chat.messages.value.map((item) => item.content),
      ['message-2', 'message-3', 'message-4', 'message-5'],
    )
    assert.equal(chat.hasMoreMessages.value, true)
  } finally {
    api.getChatSessionMessages = originalGetChatSessionMessages
  }
})

test('sendMessage 对同名工具调用按 tool_call_id 配对，不会串单', async () => {
  const browser = installBrowserMocks()

  try {
    browser.setFetchResponse([
      {
        type: 'tool_call',
        tool_name: 'mx_query_market',
        tool_call_id: 'call-a',
        status: 'running',
        arguments: { query: 'A' },
      },
      {
        type: 'tool_call',
        tool_name: 'mx_query_market',
        tool_call_id: 'call-b',
        status: 'running',
        arguments: { query: 'B' },
      },
      {
        type: 'tool_call',
        tool_name: 'mx_query_market',
        tool_call_id: 'call-b',
        status: 'done',
        ok: true,
        summary: 'B done',
      },
      {
        type: 'tool_call',
        tool_name: 'mx_query_market',
        tool_call_id: 'call-a',
        status: 'done',
        ok: true,
        summary: 'A done',
      },
      { type: 'completed', message: 'done' },
    ])

    const chat = useChatSession()
    chat.activeSessionId.value = 1
    chat.input.value = '检查工具调用'

    await chat.sendMessage()
    await nextTick()

    const toolCalls = chat.messages.value.at(-1)?.tool_calls ?? []
    assert.equal(toolCalls.length, 2)
    assert.deepEqual(
      toolCalls.map((item) => ({
        id: item.tool_call_id,
        status: item.status,
        summary: item.summary,
      })),
      [
        { id: 'call-a', status: 'done', summary: 'A done' },
        { id: 'call-b', status: 'done', summary: 'B done' },
      ],
    )
  } finally {
    browser.restore()
  }
})
