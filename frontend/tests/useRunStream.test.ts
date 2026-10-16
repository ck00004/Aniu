import assert from 'node:assert/strict'
import test from 'node:test'
import { nextTick } from 'vue'

import { useRunStream } from '../src/composables/useRunStream.ts'

type StreamEvent = {
  type: string
  [key: string]: unknown
}

class MemoryStorage implements Storage {
  private readonly store = new Map<string, string>()

  get length(): number {
    return this.store.size
  }

  clear(): void {
    this.store.clear()
  }

  getItem(key: string): string | null {
    return this.store.has(key) ? this.store.get(key)! : null
  }

  key(index: number): string | null {
    return Array.from(this.store.keys())[index] ?? null
  }

  removeItem(key: string): void {
    this.store.delete(key)
  }

  setItem(key: string, value: string): void {
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

function createControllableSseResponse() {
  const encoder = new TextEncoder()
  let streamController!: ReadableStreamDefaultController<Uint8Array>
  const stream = new ReadableStream<Uint8Array>({
    start(controller) {
      streamController = controller
    },
  })
  const response = new Response(stream, {
    status: 200,
    headers: { 'Content-Type': 'text/event-stream' },
  })
  return {
    response,
    emit(event: StreamEvent): void {
      streamController.enqueue(
        encoder.encode(`event: ${event.type}\ndata: ${JSON.stringify(event)}\n\n`),
      )
    },
    close(): void {
      streamController.close()
    },
    fail(): void {
      streamController.error(new Error('stream broken'))
    },
  }
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

  Object.defineProperty(globalThis, 'sessionStorage', {
    configurable: true,
    value: storage,
  })

  Object.defineProperty(globalThis, 'location', {
    configurable: true,
    value: {
      pathname: '/tasks',
      href: '/tasks',
    },
  })

  return {
    setFetchResponse(events: StreamEvent[]) {
      globalThis.fetch = async () => createSseResponse(events)
    },
    setFetchHandler(handler: (url: string) => Promise<Response>) {
      globalThis.fetch = ((url: unknown) => handler(String(url))) as typeof fetch
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

      delete (globalThis as { sessionStorage?: Storage }).sessionStorage
      globalThis.fetch = originalFetch
    },
  }
}

test('运行中 liveVisible 为 true，结束后立即关闭让位给完整详情', async () => {
  const browser = installBrowserMocks()

  try {
    const controlled = createControllableSseResponse()
    browser.setFetchHandler(async () => controlled.response)

    const runStream = useRunStream()
    runStream.reset()
    runStream.liveFocused.value = true

    const pending = runStream.start(101)
    await nextTick()
    // 流未结束：实时快照可见
    controlled.emit({ type: 'stage', stage: 'llm', message: '分析中' })
    await nextTick()
    assert.equal(runStream.state.status, 'running')
    assert.equal(runStream.liveActive.value, true)
    assert.equal(runStream.liveVisible.value, true)

    controlled.emit({ type: 'completed', message: '任务完成' })
    controlled.close()
    await pending
    await nextTick()

    // 运行结束后：快照必须隐藏（否则详情面板与删除入口永久钉死）
    assert.equal(runStream.state.status, 'completed')
    assert.equal(runStream.liveActive.value, false)
    assert.equal(runStream.liveVisible.value, false)
    assert.equal(runStream.pendingPostRunId.value, 101)
  } finally {
    browser.restore()
  }
})

test('运行失败后 liveVisible 同样关闭', async () => {
  const browser = installBrowserMocks()

  try {
    browser.setFetchResponse([
      { type: 'stage', stage: 'llm', message: '分析中' },
      { type: 'failed', message: '上游超时' },
    ])

    const runStream = useRunStream()
    runStream.reset()
    runStream.liveFocused.value = true

    await runStream.start(102)
    await nextTick()

    assert.equal(runStream.state.status, 'failed')
    assert.equal(runStream.liveVisible.value, false)
  } finally {
    browser.restore()
  }
})

test('事件流连接 404 后不重试，liveVisible 关闭且状态为 error', async () => {
  const browser = installBrowserMocks()

  try {
    browser.setFetchHandler(async () => new Response('not found', { status: 404 }))

    const runStream = useRunStream()
    runStream.reset()
    runStream.liveFocused.value = true

    await runStream.start(103)
    await nextTick()

    assert.equal(runStream.state.status, 'error')
    assert.equal(runStream.liveVisible.value, false)
  } finally {
    browser.restore()
  }
})

test('事件流中断后带 after_seq 游标重连并续传', async () => {
  const browser = installBrowserMocks()
  const fetchCalls: Array<{ url: string }> = []

  try {
    let callCount = 0
    browser.setFetchHandler(async (url?: string) => {
      const target = String(url)
      fetchCalls.push({ url: target })
      callCount += 1
      if (callCount === 1) {
        // 第一次连接：推送两个事件后静默结束（无终态事件）
        const controlled = createControllableSseResponse()
        queueMicrotask(() => {
          controlled.emit({ type: 'stage', stage: 'llm', message: '分析中', event_seq: 1 })
          controlled.emit({ type: 'final_started', event_seq: 2 })
          controlled.close()
        })
        return controlled.response
      }
      // 第二次连接：续传剩余事件并正常结束
      return createSseResponse([
        { type: 'final_delta', delta: '续传内容', event_seq: 3 },
        { type: 'completed', message: '任务完成', event_seq: 4 },
      ])
    })

    const runStream = useRunStream()
    runStream.reset()
    runStream.liveFocused.value = true

    await runStream.start(201)
    await nextTick()

    // 发生了重连，且第二次请求携带 after_seq 游标
    assert.ok(fetchCalls.length >= 2, `expected reconnect, got ${fetchCalls.length}`)
    assert.match(fetchCalls[1]!.url, /after_seq=2/)
    assert.equal(runStream.state.status, 'completed')
    assert.equal(runStream.state.finalAnswer.includes('续传内容'), true)
    assert.equal(runStream.liveVisible.value, false)
  } finally {
    browser.restore()
  }
})
