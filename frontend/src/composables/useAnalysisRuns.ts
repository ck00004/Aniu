import { computed, ref, watch } from 'vue'

import type { ApiDetail, RawToolPreview, RawToolPreviewDetail, RunDetail, RunSummary, RunSummaryPage, TradeDetail, TradeOrder } from '@/types'

export interface AnalysisRunViewModel {
  id: number
  analysisType: string
  startTime: string
  endTime: string | null
  duration: string
  status: string
  apiCalls: number
  tradeCount: number
  inputTokens: string
  outputTokens: string
  totalTokens: string
  apiDetails: ApiDetail[]
  rawToolPreviews: RawToolPreview[]
  tradeDetails: TradeDetail[]
  output: string | null
  originalOutput: string | null
  revisedOutput: string | null
  hasConsistencyRevision: boolean
  summary: string
  detailLoaded: boolean
  jin10BaseUrl: string | null
  jin10SourceSummary: string | null
  jin10DiagnosisText: string | null
  jin10Metrics: DiagnosticMetric[]
  jin10FailureReason: string | null
  selfSelectAdditions: SelfSelectChange[]
  selfSelectRemovals: SelfSelectChange[]
}

export interface DiagnosticMetric {
  label: string
  value: string
  tone?: 'default' | 'warning'
}

export interface SelfSelectChange {
  action: 'add' | 'remove'
  target: string
  rawQuery: string
}

const RUNS_PAGE_SIZE = 100
const CONSISTENCY_REVISION_MARKER = '[一致性检查修正说明]'

function formatTokenValue(value: number | null | undefined) {
  return typeof value === 'number' && Number.isFinite(value) && value > 0 ? String(value) : '--'
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return !!value && typeof value === 'object' && !Array.isArray(value)
}

function getStringValue(value: unknown) {
  const text = String(value ?? '').trim()
  return text || null
}

function getNumberValue(value: unknown) {
  if (typeof value !== 'number' || !Number.isFinite(value)) {
    return null
  }
  return value
}

function formatMetricNumber(value: number | null) {
  return typeof value === 'number' ? String(value) : null
}

function normalizeJin10BaseUrl(url: string | null) {
  if (!url) {
    return null
  }

  try {
    const parsed = new URL(url)
    const pathname = parsed.pathname.replace(/\/api\/news\/?$/, '')
    return `${parsed.origin}${pathname}`
  } catch {
    return url.replace(/\/api\/news(?:\?.*)?$/, '')
  }
}

function getJin10SourceInfo(detail: RunDetail) {
  const skillPayloads = isRecord(detail.skill_payloads) ? detail.skill_payloads : null
  const prefetchedContext = isRecord(skillPayloads?.prefetched_context)
    ? skillPayloads.prefetched_context
    : null

  if (!prefetchedContext) {
    return {
      jin10BaseUrl: null,
      jin10SourceSummary: null,
      jin10DiagnosisText: null,
      jin10Metrics: [],
      jin10FailureReason: null,
    }
  }

  const fullUrl = getStringValue(prefetchedContext.url)
  const baseUrl = normalizeJin10BaseUrl(fullUrl)
  const ok = typeof prefetchedContext.ok === 'boolean' ? prefetchedContext.ok : null
  const error = getStringValue(prefetchedContext.error)
  const itemCount = getNumberValue(prefetchedContext.item_count)
  const total = getNumberValue(prefetchedContext.total)
  const hasMore = typeof prefetchedContext.has_more === 'boolean' ? prefetchedContext.has_more : null
  const requestCount = getNumberValue(prefetchedContext.request_count)
  const params = isRecord(prefetchedContext.params) ? prefetchedContext.params : null
  const analysisMeta = isRecord(prefetchedContext.analysis_meta)
    ? prefetchedContext.analysis_meta
    : null
  const date = getStringValue(params?.date)
  const startTime = getStringValue(params?.startTime)
  const endTime = getStringValue(params?.endTime)
  const diagnosisText = getStringValue(prefetchedContext.analysis_text)
  const analysisSummary = getStringValue(analysisMeta?.summary)
  const analysisError = getStringValue(analysisMeta?.failure_reason) || getStringValue(analysisMeta?.error)
  const chunkCount = getNumberValue(analysisMeta?.chunk_count)
  const analysisStatus = getStringValue(analysisMeta?.status)
  const fallbackUsed = typeof analysisMeta?.fallback_used === 'boolean' ? analysisMeta.fallback_used : null

  const segments: string[] = []
  if (date) {
    segments.push(`日期 ${date}`)
  }
  if (startTime || endTime) {
    segments.push(`时间 ${startTime ?? '--'}-${endTime ?? '--'}`)
  }
  if (typeof itemCount === 'number') {
    let countText = `已获取 ${itemCount} 条`
    if (typeof total === 'number' && total > itemCount) {
      countText += `，共 ${total} 条`
    }
    if (hasMore) {
      countText += '，当前仅展示部分结果'
    }
    segments.push(countText)
  }

  const metrics: DiagnosticMetric[] = []
  const fetchCountParts: string[] = []
  const itemCountText = formatMetricNumber(itemCount)
  if (itemCountText) {
    fetchCountParts.push(itemCountText)
  }
  const totalText = formatMetricNumber(total)
  if (totalText && totalText !== itemCountText) {
    fetchCountParts.push(`总量 ${totalText}`)
  }
  if (fetchCountParts.length) {
    metrics.push({
      label: '抓取条数',
      value: fetchCountParts.join(' / '),
    })
  }
  const requestCountText = formatMetricNumber(requestCount)
  if (requestCountText) {
    metrics.push({
      label: '分页请求数',
      value: requestCountText,
    })
  }
  const chunkCountText = formatMetricNumber(chunkCount)
  if (chunkCountText) {
    metrics.push({
      label: '分块数',
      value: chunkCountText,
    })
  }
  if (analysisStatus) {
    const statusLabelMap: Record<string, string> = {
      ok: '诊断完成',
      no_items: '无可用新闻',
      fetch_failed: '抓取失败',
      llm_unavailable: '模型未配置',
      failed: '诊断失败',
      fallback_raw_context: '诊断失败，已回退',
    }
    metrics.push({
      label: '诊断状态',
      value: statusLabelMap[analysisStatus] ?? analysisStatus,
      tone: analysisStatus === 'ok' || analysisStatus === 'no_items' ? 'default' : 'warning',
    })
  }
  if (fallbackUsed) {
    metrics.push({
      label: '回退策略',
      value: '已回退为原始新闻摘录',
      tone: 'warning',
    })
  }
  if (hasMore === true) {
    metrics.push({
      label: '拉取完整性',
      value: '仍有后续分页未合并',
      tone: 'warning',
    })
  }

  let summary = '已注入 Jin10 新闻上下文'
  if (ok === false) {
    summary = error ? `拉取失败：${error}` : '拉取失败'
  } else if (analysisError) {
    summary = `已拉取 Jin10 新闻，但诊断失败：${analysisError}`
  } else if (ok === true && segments.length) {
    summary = segments.join('；')
  } else if (segments.length) {
    summary = segments.join('；')
  }

  const finalDiagnosisText = diagnosisText || analysisSummary

  return {
    jin10BaseUrl: baseUrl ?? fullUrl,
    jin10SourceSummary: summary,
    jin10DiagnosisText: finalDiagnosisText,
    jin10Metrics: metrics,
    jin10FailureReason: analysisError || (ok === false ? error : null),
  }
}

function getConsistencyOutputSections(detail: RunDetail) {
  const decisionPayload = isRecord(detail.decision_payload) ? detail.decision_payload : null
  const originalFromDecision = getStringValue(decisionPayload?.original_final_answer)
  const finalAnswer = getStringValue(detail.final_answer)
  const outputMarkdown = getStringValue(detail.output_markdown)
  const errorMessage = getStringValue(detail.error_message)
  const analysisSummary = getStringValue(detail.analysis_summary)
  const fallbackOutput = outputMarkdown || finalAnswer || analysisSummary || errorMessage || '暂无分析输出'

  if (!finalAnswer || !finalAnswer.includes(CONSISTENCY_REVISION_MARKER)) {
    return {
      output: fallbackOutput,
      originalOutput: null,
      revisedOutput: null,
      hasConsistencyRevision: false,
    }
  }

  const [beforeMarker, ...afterMarkerParts] = finalAnswer.split(CONSISTENCY_REVISION_MARKER)
  const derivedOriginal = getStringValue(beforeMarker)
  const revisedCandidate = getStringValue(afterMarkerParts.join(CONSISTENCY_REVISION_MARKER))
  const originalOutput = originalFromDecision || derivedOriginal
  const revisedOutput = revisedCandidate || finalAnswer

  return {
    output: fallbackOutput,
    originalOutput,
    revisedOutput,
    hasConsistencyRevision: !!(originalOutput && revisedOutput),
  }
}

function normalizeSelfSelectTarget(rawTarget: string) {
  return rawTarget
    .replace(/^(把|将)/, '')
    .replace(/(加入|添加到?|放入|纳入|移除|移出|删除|去掉).+$/, '')
    .replace(/^(从)/, '')
    .replace(/(我的)?自选(股|列表)?(中)?/g, '')
    .replace(/[，。,；;:：]+$/g, '')
    .trim()
}

function parseSelfSelectChange(query: unknown): SelfSelectChange | null {
  const rawQuery = String(query ?? '').trim()
  if (!rawQuery) {
    return null
  }

  const normalized = rawQuery.replace(/\s+/g, '')
  const removalKeywords = ['移除', '移出', '删除', '去掉']
  const additionKeywords = ['加入', '添加', '放入', '纳入']
  const action = removalKeywords.some((keyword) => normalized.includes(keyword))
    ? 'remove'
    : additionKeywords.some((keyword) => normalized.includes(keyword))
      ? 'add'
      : null

  if (!action) {
    return null
  }

  const patterns: Array<{ action: 'add' | 'remove', regex: RegExp }> = [
    { action: 'add', regex: /(?:把|将)(.+?)(?:加入|添加到?|放入|纳入)(?:我的)?自选(?:股|列表)?/ },
    { action: 'remove', regex: /(?:把|将)(.+?)(?:从)?(?:我的)?自选(?:股|列表)?(?:中)?(?:删除|移除|移出|去掉)/ },
    { action: 'remove', regex: /(?:把|将)(.+?)(?:删除|移除|移出|去掉)(?:出)?(?:我的)?自选(?:股|列表)?/ },
    { action: 'add', regex: /(.+?)(?:加入|添加到?|放入|纳入)(?:我的)?自选(?:股|列表)?/ },
    { action: 'remove', regex: /(.+?)(?:从)?(?:我的)?自选(?:股|列表)?(?:中)?(?:删除|移除|移出|去掉)/ },
  ]

  const matched = patterns.find((item) => item.action === action && item.regex.test(rawQuery))
  const capture = matched ? rawQuery.match(matched.regex)?.[1] ?? '' : ''
  const target = normalizeSelfSelectTarget(capture) || rawQuery

  return {
    action,
    target,
    rawQuery,
  }
}

function getSelfSelectChanges(detail: RunDetail) {
  const changes = (detail.executed_actions ?? [])
    .filter((item): item is Record<string, unknown> => isRecord(item))
    .filter((item) => String(item.action ?? '').toUpperCase() === 'MANAGE_SELF_SELECT')
    .map((item) => parseSelfSelectChange(item.query ?? item.symbol))
    .filter((item): item is SelfSelectChange => item !== null)

  return {
    additions: changes.filter((item) => item.action === 'add'),
    removals: changes.filter((item) => item.action === 'remove'),
  }
}

function getTokenUsage(detail: RunDetail) {
  const responseUsage = extractUsage(detail.llm_response_payload)
  const requestUsage = extractUsage(detail.llm_request_payload)

  const promptTokens = Number(responseUsage?.prompt_tokens ?? requestUsage?.prompt_tokens ?? 0)
  const completionTokens = Number(responseUsage?.completion_tokens ?? requestUsage?.completion_tokens ?? 0)
  const totalTokens = Number(responseUsage?.total_tokens ?? requestUsage?.total_tokens ?? promptTokens + completionTokens)

  return {
    input: promptTokens > 0 ? String(promptTokens) : '--',
    output: completionTokens > 0 ? String(completionTokens) : '--',
    total: totalTokens > 0 ? String(totalTokens) : '--',
  }
}

function getDuration(startedAt: string, finishedAt: string | null) {
  if (!finishedAt) {
    return '进行中'
  }

  const start = new Date(startedAt).getTime()
  const end = new Date(finishedAt).getTime()
  if (Number.isNaN(start) || Number.isNaN(end) || end <= start) {
    return '--'
  }

  const totalSeconds = Math.floor((end - start) / 1000)
  const minutes = Math.floor(totalSeconds / 60)
  const seconds = totalSeconds % 60
  return `${minutes}分${String(seconds).padStart(2, '0')}秒`
}

function getRunTypeText(detail: Pick<RunDetail, 'run_type' | 'trigger_source'>) {
  if (detail.run_type === 'trade') return '交易任务'
  if (detail.run_type === 'analysis') return '分析任务'
  if (detail.trigger_source === 'manual') return '手动运行'
  return '任务运行'
}

function extractUsage(payload: unknown): Record<string, unknown> | undefined {
  if (!payload || typeof payload !== 'object') {
    return undefined
  }

  const directUsage = (payload as { usage?: Record<string, unknown> }).usage
  if (directUsage && typeof directUsage === 'object') {
    return directUsage
  }

  const responses = (payload as { responses?: unknown[] }).responses
  if (Array.isArray(responses)) {
    for (let index = responses.length - 1; index >= 0; index -= 1) {
      const item = responses[index]
      if (!item || typeof item !== 'object') {
        continue
      }
      const usage = (item as { usage?: Record<string, unknown> }).usage
      if (usage && typeof usage === 'object') {
        return usage
      }
    }
  }

  return undefined
}

function getApiToolText(name: string) {
  const mapping: Record<string, { label: string, summary: string }> = {
    mx_get_positions: { label: '获取持仓', summary: '读取当前账户持仓与仓位分布。' },
    mx_get_balance: { label: '获取资产', summary: '读取账户总资产、现金和收益情况。' },
    mx_get_orders: { label: '获取委托', summary: '读取近期委托和成交记录，用于判断交易状态。' },
    mx_get_self_selects: { label: '获取自选', summary: '读取当前自选股列表，辅助观察候选标的。' },
    mx_query_market: { label: '查询行情', summary: '获取目标股票的实时行情和基础市场数据。' },
    mx_search_news: { label: '搜索资讯', summary: '查询相关新闻或公告，辅助判断市场事件影响。' },
    mx_screen_stocks: { label: '筛选股票', summary: '按条件筛选候选标的，缩小分析范围。' },
    mx_manage_self_select: { label: '管理自选', summary: '增删自选股，维护后续关注列表。' },
    mx_moni_trade: { label: '提交模拟交易', summary: '向模拟交易系统提交买入或卖出指令。' },
    mx_moni_cancel: { label: '撤销委托', summary: '撤销尚未完成的模拟委托单。' },
  }
  return mapping[name] ?? { label: name || '未命名调用', summary: '执行一次系统或妙想工具调用。' }
}

function extractTradeName(payload: unknown) {
  if (!payload || typeof payload !== 'object') {
    return ''
  }

  const candidates = [
    (payload as { name?: unknown }).name,
    (payload as { stock_name?: unknown }).stock_name,
    (payload as { stockName?: unknown }).stockName,
    (payload as { security_name?: unknown }).security_name,
    (payload as { securityName?: unknown }).securityName,
  ]

  for (const candidate of candidates) {
    const value = String(candidate ?? '').trim()
    if (value) {
      return value
    }
  }

  const result = (payload as { result?: unknown }).result
  if (result && result !== payload) {
    return extractTradeName(result)
  }

  return ''
}

function getTradeSummary(action: 'buy' | 'sell', symbol: string, name: string, volume: number, price: number | null, amount: number | null) {
  void name
  void price
  void amount

  const displaySymbol = symbol || '--'
  const actionText = action === 'sell' ? '卖出' : '买入'

  return `挂单${actionText}${displaySymbol}共计${volume}股。`
}

function mapApiDetails(detail: RunDetail): ApiDetail[] {
  const tradeToolNames = new Set(['mx_moni_trade', 'mx_moni_cancel'])
  const skillPayloads = detail.skill_payloads && typeof detail.skill_payloads === 'object'
    ? detail.skill_payloads
    : null
  const decisionPayload = detail.decision_payload && typeof detail.decision_payload === 'object'
    ? detail.decision_payload
    : null

  const toolCalls = Array.isArray(skillPayloads?.tool_calls)
    ? skillPayloads?.tool_calls
    : Array.isArray(decisionPayload?.tool_calls)
      ? decisionPayload?.tool_calls
      : []

  return toolCalls
    .filter((item): item is Record<string, unknown> => !!item && typeof item === 'object')
    .filter((item) => !tradeToolNames.has(String(item.name ?? '')))
    .map((item, idx) => {
      const toolText = getApiToolText(String(item.name ?? ''))
      const result = item.result && typeof item.result === 'object'
        ? item.result as Record<string, unknown>
        : null
      const ok = typeof result?.ok === 'boolean' ? result.ok : null
      return {
        tool_name: String(item.name ?? ''),
        name: toolText.label,
        summary: toolText.summary,
        preview_index: idx,
        tool_call_id: typeof item.id === 'string' ? item.id : null,
        status: ok === false ? 'failed' : 'done',
        ok,
      }
    })
}

function resolveTradeDetailStatus(value: unknown): 'done' | 'failed' {
  const text = String(value ?? '').trim().toLowerCase()
  if (text && ['fail', 'error', 'reject'].some((flag) => text.includes(flag))) {
    return 'failed'
  }
  return 'done'
}

function mapTradeDetails(tradeOrders: TradeOrder[], executedActions: Array<Record<string, unknown>> | null): TradeDetail[] {
  if (tradeOrders.length > 0) {
    return tradeOrders.map((order) => {
      const price = order.price
      const action = String(order.action).toUpperCase() === 'SELL' ? 'sell' : 'buy'
      const name = extractTradeName(order.response_payload) || order.symbol
      const amount = price == null ? null : Number((price * order.quantity).toFixed(2))
      const status = resolveTradeDetailStatus(order.status)
      return {
        action,
        action_text: action === 'sell' ? '模拟卖出' : '模拟买入',
        symbol: order.symbol,
        name,
        volume: order.quantity,
        price,
        amount,
        summary: getTradeSummary(action, order.symbol, name, order.quantity, price, amount),
        tool_name: null,
        preview_index: null,
        status,
        ok: status !== 'failed',
      }
    })
  }

  return (executedActions ?? [])
    .filter((action) => ['BUY', 'SELL'].includes(String(action.action ?? '').toUpperCase()))
    .map((action) => {
    const actionName = String(action.action ?? '').toUpperCase()
    const actionType = actionName === 'SELL' ? 'sell' : 'buy'
    const price = action.price == null ? null : Number(action.price)
    const volume = Number(action.quantity ?? 0)
    const symbol = String(action.symbol ?? '--')
    const name = String(action.name ?? '').trim() || symbol
    const amount = price == null ? null : Number((price * volume).toFixed(2))
    const status = resolveTradeDetailStatus(action.status)
    return {
      action: actionType,
      action_text: actionName === 'SELL' ? '模拟卖出' : '模拟买入',
      symbol,
      name,
      volume,
      price,
      amount,
      summary: getTradeSummary(actionType, symbol, name, volume, price, amount),
      tool_name: null,
      preview_index: null,
      status,
      ok: status !== 'failed',
    }
    })
}

function mapRunSummaryToViewModel(summary: RunSummary): AnalysisRunViewModel {
  return {
    id: summary.id,
    analysisType: getRunTypeText(summary),
    startTime: summary.started_at,
    endTime: summary.finished_at,
    duration: getDuration(summary.started_at, summary.finished_at),
    status: summary.status,
    apiCalls: summary.api_call_count,
    tradeCount: summary.executed_trade_count,
    inputTokens: formatTokenValue(summary.input_tokens),
    outputTokens: formatTokenValue(summary.output_tokens),
    totalTokens: formatTokenValue(summary.total_tokens),
    apiDetails: [],
    rawToolPreviews: [],
    tradeDetails: [],
    output: null,
    originalOutput: null,
    revisedOutput: null,
    hasConsistencyRevision: false,
    summary: summary.analysis_summary || '--',
    detailLoaded: false,
    jin10BaseUrl: null,
    jin10SourceSummary: null,
    jin10DiagnosisText: null,
    jin10Metrics: [],
    jin10FailureReason: null,
    selfSelectAdditions: [],
    selfSelectRemovals: [],
  }
}

function mapRunDetailToViewModel(detail: RunDetail): AnalysisRunViewModel {
  const tokenUsage = getTokenUsage(detail)
  const apiDetails = detail.api_details?.length ? detail.api_details : mapApiDetails(detail)
  const rawToolPreviews = Array.isArray(detail.raw_tool_previews) ? detail.raw_tool_previews : []
  const tradeDetails = detail.trade_details?.length ? detail.trade_details : mapTradeDetails(detail.trade_orders, detail.executed_actions)
  const outputSections = getConsistencyOutputSections(detail)
  const jin10SourceInfo = getJin10SourceInfo(detail)
  const selfSelectChanges = getSelfSelectChanges(detail)

  return {
    id: detail.id,
    analysisType: getRunTypeText(detail),
    startTime: detail.started_at,
    endTime: detail.finished_at,
    duration: getDuration(detail.started_at, detail.finished_at),
    status: detail.status,
    apiCalls: apiDetails.length,
    tradeCount: tradeDetails.length,
    inputTokens: tokenUsage.input,
    outputTokens: tokenUsage.output,
    totalTokens: tokenUsage.total,
    apiDetails,
    rawToolPreviews,
    tradeDetails,
    output: outputSections.output,
    originalOutput: outputSections.originalOutput,
    revisedOutput: outputSections.revisedOutput,
    hasConsistencyRevision: outputSections.hasConsistencyRevision,
    summary: detail.analysis_summary || '--',
    detailLoaded: true,
    jin10BaseUrl: jin10SourceInfo.jin10BaseUrl,
    jin10SourceSummary: jin10SourceInfo.jin10SourceSummary,
    jin10DiagnosisText: jin10SourceInfo.jin10DiagnosisText,
    jin10Metrics: jin10SourceInfo.jin10Metrics,
    jin10FailureReason: jin10SourceInfo.jin10FailureReason,
    selfSelectAdditions: selfSelectChanges.additions,
    selfSelectRemovals: selfSelectChanges.removals,
  }
}

function isSameDay(value: string, target: Date) {
  const date = new Date(value)
  return date.getFullYear() === target.getFullYear()
    && date.getMonth() === target.getMonth()
    && date.getDate() === target.getDate()
}

function getLatestRun(runs: AnalysisRunViewModel[]) {
  if (runs.length === 0) {
    return null
  }

  return runs.reduce((latest, current) => {
    const latestTime = new Date(latest.startTime).getTime()
    const currentTime = new Date(current.startTime).getTime()
    return currentTime > latestTime ? current : latest
  })
}

export function useAnalysisRuns(options: {
  listRunsPage: (options?: { limit?: number, date?: string, status?: string, beforeId?: number }) => Promise<RunSummaryPage>
  loadRunDetail: (runId: number, options?: { force?: boolean }) => Promise<RunDetail>
  loadRawToolPreview: (runId: number, previewIndex: number) => Promise<RawToolPreviewDetail>
}) {
  const selectedRun = ref<AnalysisRunViewModel | null>(null)
  const selectedRunLoading = ref(false)
  const renderedOutputHtml = ref('')
  const renderedOriginalOutputHtml = ref('')
  const renderedRevisedOutputHtml = ref('')
  const renderedOutputLoading = ref(false)
  const todayRuns = ref<AnalysisRunViewModel[]>([])
  const historyRuns = ref<AnalysisRunViewModel[]>([])
  const selectedDate = ref('')
  const loading = ref(false)
  const errorMessage = ref('')
  const runCache = new Map<number, AnalysisRunViewModel>()
  const markdownCache = new Map<string, string>()
  const sourceSummaries = ref<RunSummary[]>([])
  const rawToolPreviewRequests = new Map<string, Promise<RawToolPreviewDetail>>()

  let markdownRendererPromise: Promise<((content: string) => string)> | null = null

  const allRuns = computed(() => sourceSummaries.value)

  function shouldIncludeRun(run: AnalysisRunViewModel) {
    return !!run
  }

  function filterVisibleRuns(runs: AnalysisRunViewModel[]) {
    return runs.filter(shouldIncludeRun)
  }

  async function hydrateSelectedRun(runId: number, force = false) {
    selectedRunLoading.value = true

    try {
      const detail = await ensureRunDetail(runId, force)
      if (selectedRun.value?.id === runId) {
        selectedRun.value = detail
      }
    } finally {
      if (selectedRun.value?.id === runId) {
        selectedRunLoading.value = false
      }
    }
  }

  async function syncSelectedRun(runs: AnalysisRunViewModel[]) {
    if (selectedRun.value && runs.some((run) => run.id === selectedRun.value?.id)) {
      selectedRun.value = runs.find((run) => run.id === selectedRun.value?.id) ?? selectedRun.value
      await hydrateSelectedRun(selectedRun.value.id)
      return
    }

    selectedRun.value = getLatestRun(runs)
    if (selectedRun.value) {
      await hydrateSelectedRun(selectedRun.value.id)
      return
    }

    selectedRunLoading.value = false
  }

  async function ensureRunDetail(runId: number, force = false) {
    if (!force && runCache.has(runId)) {
      return runCache.get(runId)!
    }

    const detail = await options.loadRunDetail(runId, { force })
    const mapped = mapRunDetailToViewModel(detail)
    runCache.set(runId, mapped)
    return mapped
  }

  async function refreshRunDetail(runId: number) {
    const detail = await ensureRunDetail(runId, true)
    if (selectedRun.value?.id === runId) {
      selectedRun.value = detail
    }
    return detail
  }

  async function ensureRawToolPreview(runId: number, previewIndex: number): Promise<RawToolPreview> {
    const run = runCache.get(runId)
    const cachedPreview = run?.rawToolPreviews.find((item) => item.preview_index === previewIndex) ?? null
    if (cachedPreview && !cachedPreview.truncated) {
      return cachedPreview
    }

    const requestKey = `${runId}:${previewIndex}`
    const pendingRequest = rawToolPreviewRequests.get(requestKey)
    if (pendingRequest) {
      const detail = await pendingRequest
      return applyRawToolPreviewDetail(runId, detail)
    }

    const request = options.loadRawToolPreview(runId, previewIndex)
    rawToolPreviewRequests.set(requestKey, request)
    try {
      const detail = await request
      return applyRawToolPreviewDetail(runId, detail)
    } finally {
      rawToolPreviewRequests.delete(requestKey)
    }
  }

  function applyRawToolPreviewDetail(runId: number, detail: RawToolPreviewDetail): RawToolPreview {
    const run = runCache.get(runId)
    const nextPreview: RawToolPreview = {
      preview_index: detail.preview_index,
      tool_name: detail.tool_name,
      display_name: detail.display_name,
      summary: detail.summary,
      preview: detail.full_preview,
      truncated: false,
    }

    if (!run) {
      return nextPreview
    }

    const nextRun: AnalysisRunViewModel = {
      ...run,
      rawToolPreviews: run.rawToolPreviews.map((item) => (
        item.preview_index === detail.preview_index ? nextPreview : item
      )),
    }
    runCache.set(runId, nextRun)

    if (selectedRun.value?.id === runId) {
      selectedRun.value = nextRun
    }

    todayRuns.value = todayRuns.value.map((item) => (item.id === runId ? nextRun : item))
    historyRuns.value = historyRuns.value.map((item) => (item.id === runId ? nextRun : item))

    return nextPreview
  }

  async function loadInitialRuns(config: { syncSelection?: boolean } = {}) {
    const { syncSelection = true } = config
    loading.value = true
    errorMessage.value = ''

    try {
      const page = await options.listRunsPage({ limit: RUNS_PAGE_SIZE })
      sourceSummaries.value = page.items
      const today = new Date()
      const todaysSummaries = sourceSummaries.value.filter((item) => isSameDay(item.started_at, today))
      const mappedTodayRuns = todaysSummaries.map(mapRunSummaryToViewModel)

      todayRuns.value = filterVisibleRuns(mappedTodayRuns)

      if (syncSelection) {
        await syncSelectedRun(todayRuns.value)
      }
    } catch (error) {
      errorMessage.value = (error as Error).message
      todayRuns.value = []
      selectedRun.value = null
    } finally {
      loading.value = false
    }
  }

  async function selectRun(run: AnalysisRunViewModel, options?: { force?: boolean }) {
    selectedRun.value = run
    if (run.detailLoaded && !options?.force) {
      selectedRunLoading.value = false
      return
    }

    await hydrateSelectedRun(run.id, options?.force === true)
  }

  async function loadHistoryRuns() {
    if (!selectedDate.value) {
      historyRuns.value = []
      return
    }

    errorMessage.value = ''

    try {
      const page = await options.listRunsPage({
        limit: RUNS_PAGE_SIZE,
        date: selectedDate.value,
      })
      const matched = page.items
      sourceSummaries.value = mergeSourceSummaries(sourceSummaries.value, matched)
      historyRuns.value = filterVisibleRuns(matched.map(mapRunSummaryToViewModel))

      if (selectedDate.value) {
        await syncSelectedRun(historyRuns.value)
      }
    } catch (error) {
      errorMessage.value = (error as Error).message
      historyRuns.value = []
    }
  }

  async function getMarkdownRenderer() {
    if (!markdownRendererPromise) {
      markdownRendererPromise = Promise.all([
        import('dompurify'),
        import('marked'),
      ]).then(([domPurifyModule, markedModule]) => {
        const DOMPurify = domPurifyModule.default
        const { marked } = markedModule
        return (content: string) => {
          const rawHtml = marked.parse(content)
          return DOMPurify.sanitize(typeof rawHtml === 'string' ? rawHtml : '')
        }
      })
    }

    return markdownRendererPromise
  }

  async function renderMarkdownContent(content: string | null) {
    if (!content) {
      return ''
    }

    const cached = markdownCache.get(content)
    if (cached) {
      return cached
    }

    const renderMarkdown = await getMarkdownRenderer()
    const sanitized = renderMarkdown(content)
    markdownCache.set(content, sanitized)
    return sanitized
  }

  async function renderSelectedOutput(run: AnalysisRunViewModel | null) {
    if (!run) {
      renderedOutputHtml.value = ''
      renderedOriginalOutputHtml.value = ''
      renderedRevisedOutputHtml.value = ''
      renderedOutputLoading.value = false
      return
    }

    renderedOutputLoading.value = true
    const [outputHtml, originalHtml, revisedHtml] = await Promise.all([
      renderMarkdownContent(run.output),
      renderMarkdownContent(run.originalOutput),
      renderMarkdownContent(run.revisedOutput),
    ])

    if (selectedRun.value?.id === run.id) {
      renderedOutputHtml.value = outputHtml
      renderedOriginalOutputHtml.value = originalHtml
      renderedRevisedOutputHtml.value = revisedHtml
      renderedOutputLoading.value = false
    }
  }

  watch(
    () => selectedRun.value,
    (run) => {
      void renderSelectedOutput(run)
    },
    { immediate: true },
  )

  function mergeSourceSummaries(existing: RunSummary[], incoming: RunSummary[]) {
    const merged = new Map<number, RunSummary>()
    for (const item of existing) {
      merged.set(item.id, item)
    }
    for (const item of incoming) {
      merged.set(item.id, item)
    }
    return [...merged.values()].sort((a, b) => {
      const timeDelta = new Date(b.started_at).getTime() - new Date(a.started_at).getTime()
      if (timeDelta !== 0) {
        return timeDelta
      }
      return b.id - a.id
    })
  }

  return {
    selectedRun,
    selectedRunLoading,
    todayRuns,
    historyRuns,
    selectedDate,
    loading,
    errorMessage,
    renderedOutputHtml,
    renderedOriginalOutputHtml,
    renderedRevisedOutputHtml,
    renderedOutputLoading,
    loadInitialRuns,
    selectRun,
    refreshRunDetail,
    ensureRawToolPreview,
    loadHistoryRuns,
  }
}
