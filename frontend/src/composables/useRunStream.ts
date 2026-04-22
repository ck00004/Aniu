import { computed, effectScope, reactive, ref, watch } from 'vue'

import { api, getStoredToken } from '@/services/api'
import type { ApiDetail, TradeDetail } from '@/types'
import { parseSseChunk } from '@/utils/sse'

const PERSIST_KEY = 'aniu.runstream.v2'
const MAX_EVENT_BUFFER = 300
const STREAM_REVEAL_INTERVAL_MS = 180
const DEFAULT_RUN_TYPE_LABEL = '分析任务'
const STAGE_MSG_ANALYZING = '正在通过Skill技能获取信息并进行分析...'
const STAGE_MSG_FINAL_STREAMING = '正在生成最终结论...'

interface PersistedRunState {
  liveRunId: number | null
  manualRunning: boolean
  liveStartedAt: number | null
  liveRunTypeLabel: string | null
}

export interface RunStreamEvent {
  type: string
  run_id?: number
  ts?: number
  [key: string]: unknown
}

export interface ToolCallEntry {
  tool_call_id?: string
  tool_name: string
  phase?: string
  status: 'running' | 'done'
  ok?: boolean
  summary?: string
  arguments?: unknown
  started_at: number
  finished_at?: number
}

export interface RunStreamState {
  status: 'idle' | 'connecting' | 'running' | 'completed' | 'failed' | 'error'
  stage: string
  stageMessage: string
  events: RunStreamEvent[]
  toolCalls: ToolCallEntry[]
  apiDetails: ApiDetail[]
  tradeDetails: TradeDetail[]
  finalAnswer: string
  finalStarted: boolean
  finalStreaming: boolean
  errorMessage: string
  heartbeatAt: number
}

interface StartOptions {
  startedAt?: number
  runTypeLabel?: string
}

function readPersisted(): PersistedRunState | null {
  try {
    const raw = window.sessionStorage.getItem(PERSIST_KEY)
    if (!raw) return null
    const parsed = JSON.parse(raw) as PersistedRunState
    if (typeof parsed !== 'object' || parsed === null) return null
    return parsed
  } catch {
    return null
  }
}

function writePersisted(value: PersistedRunState | null): void {
  try {
    if (value === null || value.liveRunId === null) {
      window.sessionStorage.removeItem(PERSIST_KEY)
    } else {
      window.sessionStorage.setItem(PERSIST_KEY, JSON.stringify(value))
    }
  } catch {
    // ignore storage errors
  }
}

const TOOL_LABELS: Record<string, { label: string, summary: string }> = {
  mx_get_positions: { label: '获取持仓', summary: '读取当前账户持仓与仓位分布。' },
  mx_get_balance: { label: '获取资产', summary: '读取账户总资产、现金和收益情况。' },
  mx_get_orders: { label: '获取委托', summary: '读取近期委托和成交记录。' },
  mx_get_self_selects: { label: '获取自选', summary: '读取当前自选股票列表。' },
  mx_query_market: { label: '查询行情', summary: '获取目标股票的实时行情。' },
  mx_search_news: { label: '搜索资讯', summary: '查询相关新闻或公告。' },
  mx_screen_stocks: { label: '筛选股票', summary: '按条件筛选候选标的。' },
  mx_manage_self_select: { label: '管理自选', summary: '维护后续关注股票列表。' },
  mx_moni_trade: { label: '提交模拟交易', summary: '提交买入或卖出指令。' },
  mx_moni_cancel: { label: '撤销委托', summary: '撤销尚未完成的委托单。' },
}

const TRADE_TOOL_NAMES = new Set(['mx_moni_trade', 'mx_moni_cancel'])

function createRunStream() {
  const state = reactive<RunStreamState>({
    status: 'idle',
    stage: '',
    stageMessage: '',
    events: [],
    toolCalls: [],
    apiDetails: [],
    tradeDetails: [],
    finalAnswer: '',
    finalStarted: false,
    finalStreaming: false,
    errorMessage: '',
    heartbeatAt: 0,
  })

  const runId = ref<number | null>(null)
  const liveRunId = ref<number | null>(null)
  const manualRunning = ref(false)
  const liveFocused = ref(false)
  const liveStartedAt = ref<number>(0)
  const liveNow = ref<number>(Date.now())
  const liveRunTypeLabel = ref(DEFAULT_RUN_TYPE_LABEL)
  const pendingPostRunId = ref<number | null>(null)

  let controller: AbortController | null = null
  let timerId: number | null = null
  let apiRevealTimerId: number | null = null
  let tradeRevealTimerId: number | null = null
  let streamItemSerial = 0
  const listeners = new Set<(event: RunStreamEvent) => void>()
  const pendingApiDetails: ApiDetail[] = []
  const pendingTradeDetails: TradeDetail[] = []

  type ApiDetailStatus = NonNullable<ApiDetail['status']>
  type TradeDetailStatus = NonNullable<TradeDetail['status']>

  function nextStreamKey(prefix: 'api' | 'trade') {
    streamItemSerial += 1
    return `${prefix}-${Date.now()}-${streamItemSerial}`
  }

  function clearRevealTimers() {
    if (apiRevealTimerId !== null) {
      window.clearTimeout(apiRevealTimerId)
      apiRevealTimerId = null
    }
    if (tradeRevealTimerId !== null) {
      window.clearTimeout(tradeRevealTimerId)
      tradeRevealTimerId = null
    }
  }

  function scheduleApiReveal() {
    if (apiRevealTimerId !== null || pendingApiDetails.length === 0) {
      return
    }
    apiRevealTimerId = window.setTimeout(() => {
      apiRevealTimerId = null
      const nextItem = pendingApiDetails.shift()
      if (nextItem) {
        state.apiDetails.push(nextItem)
      }
      scheduleApiReveal()
    }, STREAM_REVEAL_INTERVAL_MS)
  }

  function scheduleTradeReveal() {
    if (tradeRevealTimerId !== null || pendingTradeDetails.length === 0) {
      return
    }
    tradeRevealTimerId = window.setTimeout(() => {
      tradeRevealTimerId = null
      const nextItem = pendingTradeDetails.shift()
      if (nextItem) {
        state.tradeDetails.push(nextItem)
      }
      scheduleTradeReveal()
    }, STREAM_REVEAL_INTERVAL_MS)
  }

  function enqueueApiDetail(detail: ApiDetail) {
    if (
      state.apiDetails.length === 0
      && pendingApiDetails.length === 0
      && apiRevealTimerId === null
    ) {
      state.apiDetails.push(detail)
      return
    }
    pendingApiDetails.push(detail)
    scheduleApiReveal()
  }

  function enqueueTradeDetail(detail: TradeDetail) {
    if (
      state.tradeDetails.length === 0
      && pendingTradeDetails.length === 0
      && tradeRevealTimerId === null
    ) {
      state.tradeDetails.push(detail)
      return
    }
    pendingTradeDetails.push(detail)
    scheduleTradeReveal()
  }

  function resetLiveCollections() {
    clearRevealTimers()
    pendingApiDetails.length = 0
    pendingTradeDetails.length = 0
    state.toolCalls = []
    state.apiDetails = []
    state.tradeDetails = []
  }

  function reset() {
    state.status = 'idle'
    state.stage = ''
    state.stageMessage = ''
    state.events = []
    resetLiveCollections()
    state.finalAnswer = ''
    state.finalStarted = false
    state.finalStreaming = false
    state.errorMessage = ''
    state.heartbeatAt = 0
    runId.value = null
    liveRunId.value = null
    liveFocused.value = false
    manualRunning.value = false
    liveStartedAt.value = 0
    liveRunTypeLabel.value = DEFAULT_RUN_TYPE_LABEL
    pendingPostRunId.value = null
    writePersisted(null)
  }

  function onEvent(fn: (event: RunStreamEvent) => void): () => void {
    listeners.add(fn)
    return () => listeners.delete(fn)
  }

  function pushEvent(event: RunStreamEvent) {
    state.events.push(event)
    if (state.events.length > MAX_EVENT_BUFFER) {
      state.events.splice(0, state.events.length - MAX_EVENT_BUFFER)
    }
  }

  function applyEvent(event: RunStreamEvent) {
    pushEvent(event)

    switch (event.type) {
      case 'heartbeat':
        state.heartbeatAt = normalizeTs(event.ts)
        return
      case 'stage':
        state.stage = String(event.stage || '')
        state.stageMessage = String(event.message || '')
        state.status = 'running'
        break
      case 'llm_request':
        state.stage = 'llm'
        state.status = 'running'
        state.stageMessage = STAGE_MSG_ANALYZING
        break
      case 'tool_call':
        applyToolCallEvent(event)
        break
      case 'llm_message':
        state.stage = 'llm'
        state.status = 'running'
        state.stageMessage = STAGE_MSG_ANALYZING
        break
      case 'final_started':
        state.stage = 'final'
        state.status = 'running'
        state.stageMessage = STAGE_MSG_FINAL_STREAMING
        state.finalStarted = true
        state.finalStreaming = true
        state.finalAnswer = ''
        break
      case 'final_delta':
        state.stage = 'final'
        state.status = 'running'
        state.stageMessage = STAGE_MSG_FINAL_STREAMING
        state.finalStarted = true
        state.finalStreaming = true
        state.finalAnswer += String(event.delta || '')
        break
      case 'final_finished':
        state.stage = 'final'
        state.status = 'running'
        state.stageMessage = '最终结论已生成'
        state.finalStarted = true
        state.finalStreaming = false
        if (typeof event.content === 'string') {
          state.finalAnswer = event.content
        }
        break
      case 'trade_order':
        applyTradeOrderEvent(event)
        break
      case 'completed':
        state.status = 'completed'
        state.stage = 'completed'
        state.stageMessage = String(event.message || '任务完成')
        state.finalStreaming = false
        manualRunning.value = false
        if (liveRunId.value !== null) {
          pendingPostRunId.value = liveRunId.value
        }
        break
      case 'failed':
        state.status = 'failed'
        state.stage = 'failed'
        state.stageMessage = String(event.message || '任务失败')
        state.errorMessage = String(event.message || '')
        state.finalStreaming = false
        manualRunning.value = false
        if (liveRunId.value !== null) {
          pendingPostRunId.value = liveRunId.value
        }
        break
    }

    for (const fn of listeners) {
      try {
        fn(event)
      } catch (err) {
        console.error('[useRunStream] listener failed', err)
      }
    }
  }

  function applyToolCallEvent(event: RunStreamEvent) {
    const toolName = String(event.tool_name || '')
    const toolCallId = String(event.tool_call_id || '')
    const phase = typeof event.phase === 'string' ? event.phase : undefined
    const status = String(event.status || 'running') as 'running' | 'done'
    const resultStatus = resolveApiDetailStatus(status, event.ok)
    const existing = state.toolCalls.find(
      (item) => item.status === 'running'
        && (
          (toolCallId && item.tool_call_id === toolCallId)
          || (!toolCallId && item.tool_name === toolName && item.phase === phase)
        ),
    )

    if (status === 'running' && !existing) {
      state.toolCalls.push({
        tool_call_id: toolCallId || undefined,
        tool_name: toolName,
        phase,
        status: 'running',
        arguments: event.arguments,
        started_at: normalizeTs(event.ts),
      })
      appendLiveApiDetail(event, resultStatus)
      return
    }

    if (status === 'done' && existing) {
      existing.status = 'done'
      existing.ok = event.ok as boolean | undefined
      existing.summary = event.summary as string | undefined
      existing.finished_at = normalizeTs(event.ts)
      syncApiDetailStatus(toolName, toolCallId, resultStatus, event.ok)
      return
    }

    if (status === 'done') {
      state.toolCalls.push({
        tool_call_id: toolCallId || undefined,
        tool_name: toolName,
        phase,
        status: 'done',
        ok: event.ok as boolean | undefined,
        summary: event.summary as string | undefined,
        started_at: normalizeTs(event.ts),
        finished_at: normalizeTs(event.ts),
      })
      appendLiveApiDetail(event, resultStatus)
    }
  }

  function resolveApiDetailStatus(
    toolStatus: 'running' | 'done',
    ok: unknown,
  ): ApiDetailStatus {
    if (toolStatus === 'running') {
      return 'running'
    }
    return ok === false ? 'failed' : 'done'
  }

  function updateApiDetailStatusInCollection(
    collection: ApiDetail[],
    toolName: string,
    toolCallId: string,
    status: ApiDetailStatus,
    ok: boolean | null,
  ): boolean {
    for (let idx = collection.length - 1; idx >= 0; idx -= 1) {
      const item = collection[idx]
      const matched = toolCallId
        ? item.tool_call_id === toolCallId
        : item.tool_name === toolName && item.status === 'running'
      if (!matched) {
        continue
      }
      item.status = status
      item.ok = ok
      if (toolCallId && !item.tool_call_id) {
        item.tool_call_id = toolCallId
      }
      return true
    }
    return false
  }

  function syncApiDetailStatus(
    toolName: string,
    toolCallId: string,
    status: ApiDetailStatus,
    rawOk: unknown,
  ) {
    const ok = typeof rawOk === 'boolean' ? rawOk : null
    if (updateApiDetailStatusInCollection(state.apiDetails, toolName, toolCallId, status, ok)) {
      return
    }
    updateApiDetailStatusInCollection(pendingApiDetails, toolName, toolCallId, status, ok)
  }

  function appendLiveApiDetail(event: RunStreamEvent, detailStatus: ApiDetailStatus) {
    const toolName = String(event.tool_name || '')
    if (!toolName || TRADE_TOOL_NAMES.has(toolName)) {
      return
    }

    const toolText = getToolText(toolName)
    enqueueApiDetail({
      tool_name: toolName,
      name: toolText.label,
      summary: toolText.summary,
      preview_index: null,
      tool_call_id: typeof event.tool_call_id === 'string' ? event.tool_call_id : null,
      status: detailStatus,
      ok: detailStatus === 'running'
        ? null
        : detailStatus === 'failed'
          ? false
          : true,
      stream_key: nextStreamKey('api'),
    })
  }

  function resolveTradeDetailStatus(value: unknown): TradeDetailStatus {
    const text = String(value ?? '').trim().toLowerCase()
    if (text && ['fail', 'error', 'reject'].some((flag) => text.includes(flag))) {
      return 'failed'
    }
    return 'done'
  }

  function applyTradeOrderEvent(event: RunStreamEvent) {
    const actionName = String(event.action || '').toUpperCase()
    const tradeAction = actionName === 'SELL' ? 'sell' : 'buy'
    const symbol = String(event.symbol || '--')
    const volume = Number(event.quantity ?? 0)
    const price = event.price == null ? null : Number(event.price)
    const amount = price == null ? null : Number((price * volume).toFixed(2))
    const tradeStatus = resolveTradeDetailStatus(event.status)

    state.stage = 'trade'
    state.status = 'running'
    state.stageMessage = '正在记录交易执行结果...'
    enqueueTradeDetail({
      action: tradeAction,
      action_text: tradeAction === 'sell' ? '模拟卖出' : '模拟买入',
      symbol,
      name: symbol,
      volume,
      price,
      amount,
      summary: `挂单${tradeAction === 'sell' ? '卖出' : '买入'}${symbol}共计${volume}股。`,
      tool_name: null,
      preview_index: null,
      status: tradeStatus,
      ok: tradeStatus !== 'failed',
      stream_key: nextStreamKey('trade'),
    })
  }

  async function start(id: number, options?: StartOptions): Promise<void> {
    stop()
    state.status = 'connecting'
    state.stage = ''
    state.stageMessage = '正在建立实时连接...'
    state.events = []
    resetLiveCollections()
    state.finalAnswer = ''
    state.finalStarted = false
    state.finalStreaming = false
    state.errorMessage = ''
    state.heartbeatAt = 0
    runId.value = id
    liveRunId.value = id
    pendingPostRunId.value = null
    manualRunning.value = true
    liveStartedAt.value = options?.startedAt ?? Date.now()
    liveRunTypeLabel.value = options?.runTypeLabel || DEFAULT_RUN_TYPE_LABEL
    liveNow.value = Date.now()
    ensureTimer()

    controller = new AbortController()
    const token = getStoredToken()
    const headers: Record<string, string> = { Accept: 'text/event-stream' }
    if (token) {
      headers.Authorization = `Bearer ${token}`
    }

    try {
      const response = await fetch(api.runEventsUrl(id), {
        method: 'GET',
        headers,
        signal: controller.signal,
        cache: 'no-store',
      })
      if (!response.ok || !response.body) {
        throw new Error(`SSE 连接失败 (${response.status})`)
      }
      state.status = 'running'

      const reader = response.body.getReader()
      const decoder = new TextDecoder('utf-8')
      let buffer = ''

      while (true) {
        const { value, done } = await reader.read()
        if (done) break
        buffer += decoder.decode(value, { stream: true })

        let idx = buffer.indexOf('\n\n')
        while (idx >= 0) {
          const chunk = buffer.slice(0, idx)
          buffer = buffer.slice(idx + 2)
          const event = parseSseChunk<RunStreamEvent>(chunk, (err, payload) => {
            console.warn('[useRunStream] parse failed', err, payload)
          })
          if (event) applyEvent(event)
          idx = buffer.indexOf('\n\n')
        }
      }
    } catch (err) {
      if ((err as DOMException)?.name === 'AbortError') return
      state.status = 'error'
      state.stage = 'error'
      state.stageMessage = '事件流已中断'
      state.errorMessage = (err as Error).message || '事件流中断'
      state.finalStreaming = false
      manualRunning.value = false
      console.error('[useRunStream] stream error', err)
    }
  }

  function stop() {
    if (controller) {
      controller.abort()
      controller = null
    }
  }

  function ensureTimer() {
    if (timerId !== null) return
    timerId = window.setInterval(() => {
      liveNow.value = Date.now()
    }, 1000)
  }

  watch(
    () => state.status,
    (status) => {
      const active = status === 'connecting' || status === 'running'
      if (active) {
        ensureTimer()
      } else if (timerId !== null) {
        window.clearInterval(timerId)
        timerId = null
      }
    },
  )

  const liveActive = computed(
    () => state.status === 'connecting' || state.status === 'running',
  )

  const liveVisible = computed(() => liveFocused.value && state.status !== 'idle')

  const liveElapsed = computed(() => {
    if (liveStartedAt.value === 0) return '--'
    const seconds = Math.max(0, Math.floor((liveNow.value - liveStartedAt.value) / 1000))
    const minutes = Math.floor(seconds / 60)
    const remain = seconds % 60
    return minutes > 0 ? `${minutes}分 ${String(remain).padStart(2, '0')}秒` : `${remain}秒`
  })

  const liveStartedAtIso = computed(() =>
    liveStartedAt.value > 0 ? new Date(liveStartedAt.value).toISOString() : null,
  )

  const liveOutputText = computed(() => {
    if (!state.finalStarted) {
      return STAGE_MSG_ANALYZING
    }
    if (state.finalAnswer) {
      return state.finalAnswer
    }
    return STAGE_MSG_FINAL_STREAMING
  })

  watch(
    [liveRunId, manualRunning, liveStartedAt, liveRunTypeLabel],
    ([id, running, startedAt, runTypeLabel]) => {
      if (id === null && !running) {
        writePersisted(null)
      } else {
        writePersisted({
          liveRunId: id,
          manualRunning: running,
          liveStartedAt: startedAt || null,
          liveRunTypeLabel: runTypeLabel || null,
        })
      }
    },
  )

  function rehydrate() {
    const persisted = readPersisted()
    if (!persisted || persisted.liveRunId === null) return
    const id = persisted.liveRunId
    liveRunId.value = id
    runId.value = id
    manualRunning.value = persisted.manualRunning
    liveStartedAt.value = persisted.liveStartedAt ?? Date.now()
    liveRunTypeLabel.value = persisted.liveRunTypeLabel || DEFAULT_RUN_TYPE_LABEL
    state.status = 'connecting'
    state.stageMessage = '正在恢复实时运行...'
    liveNow.value = Date.now()

    void api
      .getRun(id)
      .then((detail) => {
        const status = String(detail?.status || '').toLowerCase()
        const terminal = status === 'completed' || status === 'failed' || status === 'error'
        if (terminal) {
          state.status = status === 'completed' ? 'completed' : 'failed'
          state.stage = status === 'completed' ? 'completed' : 'failed'
          state.stageMessage = status === 'completed' ? '任务完成' : '任务失败'
          state.finalStarted = true
          state.finalStreaming = false
          state.finalAnswer = String(detail?.final_answer || detail?.output_markdown || '')
          clearRevealTimers()
          pendingApiDetails.length = 0
          pendingTradeDetails.length = 0
          state.apiDetails = Array.isArray(detail?.api_details) ? detail.api_details : []
          state.tradeDetails = Array.isArray(detail?.trade_details) ? detail.trade_details : []
          manualRunning.value = false
          writePersisted(null)
        } else {
          void start(id, {
            startedAt: persisted.liveStartedAt ?? Date.now(),
            runTypeLabel: persisted.liveRunTypeLabel || DEFAULT_RUN_TYPE_LABEL,
          })
        }
      })
      .catch((err) => {
        console.warn('[useRunStream] rehydrate failed', err)
        state.status = 'error'
        state.stage = 'error'
        state.stageMessage = '恢复运行失败'
        state.errorMessage = (err as Error).message || '恢复运行失败'
        state.finalStreaming = false
        manualRunning.value = false
        writePersisted(null)
      })
  }

  rehydrate()

  return {
    state,
    runId,
    liveRunId,
    manualRunning,
    liveFocused,
    liveStartedAt,
    liveStartedAtIso,
    liveRunTypeLabel,
    liveNow,
    liveActive,
    liveVisible,
    liveElapsed,
    liveOutputText,
    pendingPostRunId,
    start,
    stop,
    reset,
    onEvent,
  }
}

let singleton: ReturnType<typeof createRunStream> | null = null
let singletonScope: ReturnType<typeof effectScope> | null = null

export function useRunStream() {
  if (!singleton) {
    singletonScope = effectScope(true)
    singleton = singletonScope.run(() => createRunStream())!
  }
  return singleton
}

function getToolText(toolName: string) {
  return TOOL_LABELS[toolName] ?? { label: toolName || '未命名调用', summary: '执行一次系统或技能调用。' }
}

function normalizeTs(value: unknown): number {
  const numeric = Number(value)
  if (!Number.isFinite(numeric) || numeric <= 0) {
    return Date.now() / 1000
  }
  return numeric
}
