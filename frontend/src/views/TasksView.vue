<template>
<div class="tab-content">
        <section class="content-grid content-grid-primary">
          <!-- 记录选择区域 - 方块卡片式 -->
          <section class="panel run-grid-panel">
            <div class="panel-head">
              <div class="head-main">
                <h2>运行记录</h2>
                <p class="section-kicker">Run History</p>
              </div>
              <div class="panel-head-actions">
                <button
                  class="button ghost small soft-header-button overview-refresh-button"
                  :class="{ 'is-loading': manualRunning && activeManualAction === 'analysis' }"
                  :disabled="manualRunning"
                  :title="manualRunButtonTitle"
                  @click="handleManualRun"
                >
                  {{ manualRunning && activeManualAction === 'analysis' ? '执行中…' : '执行分析' }}
                </button>
                <button
                  class="button ghost small soft-header-button overview-refresh-button manual-trade-button"
                  :class="{ 'is-loading': manualRunning && activeManualAction === 'trade' }"
                  :disabled="manualRunning"
                  :title="manualTradeButtonTitle"
                  @click="handleManualTrade"
                >
                  {{ manualRunning && activeManualAction === 'trade' ? '执行中…' : '执行交易' }}
                </button>
              </div>
            </div>

            <div v-if="analysisError" class="error-banner">{{ analysisError }}</div>

            <div class="runs-container">
              <!-- 今日运行 - 方块网格 -->
              <div class="run-group" v-if="todayRuns.length || livePlaceholderVisible">
                <div class="group-label">
                  <span class="label-text">今日</span>
                </div>
                <div class="run-grid" v-if="todayRuns.length || livePlaceholderVisible">
                  <div
                    v-if="livePlaceholderVisible"
                    class="run-card live-run-card"
                    :class="{ active: liveFocused }"
                    @click="focusLiveCard"
                  >
                    <div class="run-card-status dot-running"></div>
                    <div class="run-card-type">{{ liveRunTypeLabel }}</div>
                    <div class="run-card-time">{{ formatShortTime(liveStartedAtIso) }}</div>
                    <div class="run-card-duration">{{ liveElapsed }}</div>
                  </div>
                   <div
                      v-for="run in todayRuns"
                       :key="run.id"
                       class="run-card"
                       :class="{ active: isTodayRunActive(run.id), 'live-run-card': isTodayRunLive(run.id) }"
                       @click="handleTodayRunSelect(run.id)"
                     >
                     <div class="run-card-status" :class="isTodayRunLive(run.id) ? 'dot-running' : statusTone(run.status)"></div>
                     <div class="run-card-type">{{ run.analysisType }}</div>
                     <div class="run-card-time">{{ formatShortTime(run.startTime) }}</div>
                     <div class="run-card-duration">{{ run.duration }}</div>
                    </div>
                </div>
                <div v-else class="run-grid-empty">
                  今日暂无可展示的运行记录。
                </div>
              </div>
              <div v-else class="run-grid-empty">
                今日暂无运行记录。
              </div>

              <!-- 历史记录 - 日期选择 + 方块网格 -->
              <div class="run-group">
                <div class="group-label">
                  <span class="label-text">历史</span>
                  <div class="group-label-meta">
                    <button type="button" class="button ghost small soft-header-button date-input-trigger" @click="openHistoryDatePicker">
                      <span class="date-input-value">{{ historyDateDisplay }}</span>
                    </button>
                    <input
                      ref="historyDateInput"
                      type="date"
                      v-model="selectedDate"
                      @change="loadHistoryRuns"
                      class="date-input-native"
                      tabindex="-1"
                      aria-hidden="true"
                    />
                  </div>
                </div>
                <div class="run-grid" v-if="historyRuns.length">
                  <div 
                     v-for="run in historyRuns" 
                      :key="run.id"
                      class="run-card"
                      :class="{ active: !liveFocused && selectedRun?.id === run.id }"
                      @click="handleSelectRun(run.id, historyRuns)"
                    >
                     <div class="run-card-status" :class="statusTone(run.status)"></div>
                     <div class="run-card-type">{{ run.analysisType }}</div>
                     <div class="run-card-time">{{ formatShortTime(run.startTime) }}</div>
                     <div class="run-card-duration">{{ run.duration }}</div>
                   </div>
                </div>
                <div v-if="selectedDate && !historyRuns.length" class="run-grid-empty">
                  该日期没有找到运行记录，请切换日期后重试。
                </div>
              </div>
            </div>
          </section>

          <!-- 分析详情内容区域 - 三列布局 -->
          <section class="panel analysis-panel">
            <div class="panel-head">
              <div class="head-main">
                <h2>分析详情</h2>
                <p class="section-kicker">Analysis Detail</p>
              </div>
              <div v-if="selectedRun && !liveVisible" class="panel-head-actions">
                <button
                  type="button"
                  class="button ghost small soft-header-button overview-refresh-button"
                  title="删除当前任务"
                  @click="handleDeleteRun(selectedRun.id)"
                >
                  删除任务
                </button>
              </div>
            </div>

            <!-- 三列详情网格 -->
            <div class="detail-grid" v-if="detailGridVisible">
              <!-- 第一列：运行状态 -->
              <div class="detail-column status-column">
                <h4 class="column-title">运行状态</h4>
                <div class="detail-column-body">
                  <div class="stat-compact">
                    <div class="stat-main">
                      <span class="time-value">{{ formatTime(displayStatusStartTime) }}</span>
                      <span class="duration-value">{{ displayStatusDuration }}</span>
                      <span class="status-dot" :class="displayStatusDotClass"></span>
                    </div>
                    <div v-if="displayTokenBarVisible" class="token-row">
                      <span class="token-item">
                        <i>输入</i>
                        <b>{{ displayInputTokens }}</b>
                      </span>
                      <span class="token-item">
                        <i>输出</i>
                        <b>{{ displayOutputTokens }}</b>
                      </span>
                      <span class="token-item total">
                        <i>总量</i>
                        <b>{{ displayTotalTokens }}</b>
                      </span>
                    </div>
                  </div>
                </div>
              </div>

              <!-- 第二列：接口调用 -->
              <div class="detail-column api-column">
                <h4 class="column-title">接口调用 ({{ displayApiDetails.length }})</h4>
                <div class="detail-column-body">
                  <div
                    v-if="displayApiDetails.length"
                    ref="apiListRef"
                    class="compact-list analysis-compact-list stream-list-shell"
                    @scroll="handleApiListScroll"
                  >
                    <TransitionGroup name="stream-reveal" tag="div" class="stream-list-group">
                      <button
                        v-for="(api, idx) in displayApiDetails"
                        :key="getApiItemKey(api, idx)"
                        type="button"
                        class="compact-item api-item compact-item-button"
                        :class="{ active: !liveVisible && activePreviewIndex === api.preview_index && api.preview_index !== null }"
                        @click="focusPreview(api.preview_index)"
                      >
                        <div class="compact-main api-main">
                          <span class="item-name" :title="api.name">{{ api.name }}</span>
                          <span class="item-summary" :title="api.summary">{{ api.summary }}</span>
                        </div>
                        <span class="compact-item-status-dot" :class="getApiItemStatusClass(api)" aria-hidden="true"></span>
                      </button>
                    </TransitionGroup>
                  </div>
                  <div v-else-if="liveVisible || selectedRun?.detailLoaded" class="detail-empty-state">
                    {{ displayApiEmptyText }}
                  </div>
                </div>
              </div>

              <!-- 第三列：交易执行 -->
              <div class="detail-column trade-column">
                <h4 class="column-title">交易执行 ({{ displayTradeDetails.length }})</h4>
                <div class="detail-column-body">
                  <div
                    v-if="displayTradeDetails.length"
                    ref="tradeListRef"
                    class="compact-list analysis-compact-list stream-list-shell"
                    @scroll="handleTradeListScroll"
                  >
                    <TransitionGroup name="stream-reveal" tag="div" class="stream-list-group">
                      <button
                        v-for="(trade, idx) in displayTradeDetails"
                        :key="getTradeItemKey(trade, idx)"
                        type="button"
                        class="compact-item trade-item compact-item-button"
                        :class="{ active: !liveVisible && activePreviewIndex === trade.preview_index && trade.preview_index !== null }"
                        @click="focusPreview(trade.preview_index)"
                      >
                        <div class="compact-main trade-main">
                          <span class="trade-text-action" :class="trade.action">{{ trade.action_text }}</span>
                          <span class="trade-text-summary" :title="trade.summary">{{ trade.summary }}</span>
                        </div>
                        <span class="compact-item-status-dot" :class="getTradeItemStatusClass(trade)" aria-hidden="true"></span>
                      </button>
                    </TransitionGroup>
                  </div>
                  <div v-else-if="liveVisible || selectedRun?.detailLoaded" class="detail-empty-state">
                    {{ displayTradeEmptyText }}
                  </div>
                </div>
              </div>
            </div>

            <div v-if="selectedRunLoading && !liveVisible" class="detail-empty-state">
              正在加载本次运行详情...
            </div>

            <div v-if="displayJin10SourceVisible" class="jin10-diagnostic-section">
              <div class="source-diagnostic-card">
                <div class="source-diagnostic-title">Jin10 诊断</div>
                <div v-if="displayJin10BaseUrl" class="source-diagnostic-row">
                  <span class="source-diagnostic-label">当前使用的 Jin10 地址</span>
                  <span class="source-diagnostic-value" :title="displayJin10BaseUrl">{{ displayJin10BaseUrl }}</span>
                </div>
                <div v-if="displayJin10SourceSummary" class="source-diagnostic-row">
                  <span class="source-diagnostic-label">本轮 Jin10 新闻来源</span>
                  <span class="source-diagnostic-value" :title="displayJin10SourceSummary">{{ displayJin10SourceSummary }}</span>
                </div>
              </div>
            </div>

            <div v-if="displaySelfSelectChangesVisible" class="self-select-section">
              <div class="self-select-card">
                <div class="self-select-title">本轮自选股变更</div>
                <div
                  v-if="displaySelfSelectAdditions.length || displaySelfSelectRemovals.length"
                  class="self-select-groups"
                >
                  <div v-if="displaySelfSelectAdditions.length" class="self-select-group">
                    <div class="self-select-group-title">新增自选 ({{ displaySelfSelectAdditions.length }})</div>
                    <div class="self-select-chip-list">
                      <span
                        v-for="(item, idx) in displaySelfSelectAdditions"
                        :key="`add-${idx}-${item.rawQuery}`"
                        class="self-select-chip add"
                        :title="item.rawQuery"
                      >
                        {{ item.target }}
                      </span>
                    </div>
                  </div>
                  <div v-if="displaySelfSelectRemovals.length" class="self-select-group">
                    <div class="self-select-group-title">移除自选 ({{ displaySelfSelectRemovals.length }})</div>
                    <div class="self-select-chip-list">
                      <span
                        v-for="(item, idx) in displaySelfSelectRemovals"
                        :key="`remove-${idx}-${item.rawQuery}`"
                        class="self-select-chip remove"
                        :title="item.rawQuery"
                      >
                        {{ item.target }}
                      </span>
                    </div>
                  </div>
                </div>
                <div v-else class="detail-empty-state self-select-empty">
                  本轮没有发生自选股新增或移除操作。
                </div>
              </div>
            </div>

             <!-- 分析输出内容 / 原始返回联动预览 / 实时结论 -->
             <div class="output-section" v-if="liveVisible || selectedRun?.output || activePreview">
               <div class="output-surface" @click="handleOutputSurfaceClick">
                  <div
                    v-if="liveVisible && liveOutputIsPlaceholder"
                    ref="liveOutputRef"
                    class="live-output-content"
                    :class="{ 'is-placeholder': liveOutputIsPlaceholder }"
                    @scroll="handleLiveOutputScroll"
                  >
                    {{ liveOutputText }}
                  </div>
                   <div
                     v-else-if="liveVisible"
                     ref="liveOutputRef"
                     class="markdown-content live-markdown-content"
                     @scroll="handleLiveOutputScroll"
                     v-html="liveOutputHtml"
                   ></div>
                   <div v-else-if="activePreview" class="raw-output-content" :class="{ 'is-loading': activePreviewLoading }">
                     {{ activePreviewText }}
                   </div>
                 <div v-else-if="renderedOutputLoading" class="detail-empty-state">
                   正在渲染分析输出...
                 </div>
                 <div v-else class="markdown-content" v-html="renderedOutputHtml"></div>
               </div>
              </div>

            <div v-if="runErrorVisible" class="error-banner">
              {{ runErrorMessage }}
            </div>

            <!-- 无数据提示 -->
            <div v-if="!detailGridVisible && !selectedRunLoading" class="empty-state">
              <p>当前没有可展示的运行详情。完成一次任务执行后，这里会显示完整分析结果。</p>
            </div>
          </section>
        </section>
      </div>
</template>

<script setup lang="ts">
import { computed, nextTick, onMounted, onBeforeUnmount, ref, watch } from 'vue'

import { useAnalysisRuns } from '@/composables/useAnalysisRuns'
import { useRunStream } from '@/composables/useRunStream'
import { api } from '@/services/api'
import { useAppStore } from '@/stores/legacy'
import { formatShortTime, formatTime, statusTone } from '@/utils/formatters'
import type { ApiDetail, TradeDetail } from '@/types'

const LIVE_MARKDOWN_RENDER_MIN_INTERVAL_MS = 32
const LIVE_OUTPUT_LOADING_HTML = '<p class="live-output-loading">正在生成最终结论...</p>'

let liveMarkdownRendererPromise: Promise<(content: string) => string> | null = null

async function getLiveMarkdownRenderer() {
  if (!liveMarkdownRendererPromise) {
    liveMarkdownRendererPromise = Promise.all([
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

  return liveMarkdownRendererPromise
}

const store = useAppStore()

const {
  selectedRun,
  selectedRunLoading,
  todayRuns,
  historyRuns,
  selectedDate,
  errorMessage: analysisError,
  renderedOutputHtml,
  renderedOutputLoading,
  loadInitialRuns,
  selectRun,
  refreshRunDetail,
  ensureRawToolPreview,
  loadHistoryRuns,
} = useAnalysisRuns({
  listRunsPage: api.listRunsPage,
  loadRunDetail: store.loadRunDetail,
  loadRawToolPreview: api.getRunRawToolPreview,
})

onMounted(() => {
  loadInitialRuns({ syncSelection: !liveFocused.value })
  if (!store.schedules.length) {
    store.loadSchedule().catch(() => {})
  }
})

const runStream = useRunStream()
const {
  liveActive,
  liveElapsed,
  liveOutputText,
  manualRunning,
  liveRunTypeLabel,
  liveRunId,
  liveFocused,
  liveStartedAtIso,
  liveVisible,
  pendingPostRunId,
} = runStream

const activeManualAction = ref<'analysis' | 'trade' | null>(null)

const livePlaceholderVisible = computed(() => {
  if (!liveActive.value) return false
  if (liveRunId.value === null) return true
  return !todayRuns.value.some((item) => item.id === liveRunId.value)
})

const preMarketScheduleId = computed(() => {
  const match = store.schedules.find((item) => item.name === '盘前分析')
  return match?.id ?? null
})

const manualRunTypeText = computed(() => {
  const match = preMarketScheduleId.value === null
    ? null
    : store.schedules.find((item) => item.id === preMarketScheduleId.value)

  return match?.run_type === 'trade' ? '交易任务' : '分析任务'
})

const tradeScheduleId = computed(() => {
  const match = store.schedules.find((item) => item.run_type === 'trade')
  return match?.id ?? null
})

const manualTradeRunTypeText = computed(() => '交易任务')

const manualRunButtonTitle = computed(() =>
  preMarketScheduleId.value === null
    ? '未找到盘前分析任务，将使用默认调度执行'
    : '手动执行一次盘前分析',
)

const manualTradeButtonTitle = computed(() =>
  tradeScheduleId.value === null
    ? '未找到交易任务，将使用默认手动交易模板执行'
    : '手动执行一次交易任务',
)

async function startManualRun(options: {
  scheduleId?: number
  runType?: 'analysis' | 'trade'
  action: 'analysis' | 'trade'
  runTypeLabel: string
}) {
  if (manualRunning.value) return
  const startedAt = Date.now()
  manualRunning.value = true
  activeManualAction.value = options.action
  liveFocused.value = true
  try {
    const { run_id } = await api.runNowStream(options.scheduleId, options.runType)
    runStream.start(run_id, {
      startedAt,
      runTypeLabel: options.runTypeLabel,
    }).catch((err) => {
      console.error('[TasksView] stream start failed', err)
    })
  } catch (error) {
    console.error('[TasksView] manual run failed', error)
    manualRunning.value = false
    activeManualAction.value = null
    liveFocused.value = false
  }
}

async function handleManualRun() {
  await startManualRun({
    scheduleId: preMarketScheduleId.value ?? undefined,
    action: 'analysis',
    runTypeLabel: manualRunTypeText.value,
  })
}

async function handleManualTrade() {
  await startManualRun({
    scheduleId: tradeScheduleId.value ?? undefined,
    runType: tradeScheduleId.value == null ? 'trade' : undefined,
    action: 'trade',
    runTypeLabel: manualTradeRunTypeText.value,
  })
}

async function handleDeleteRun(runId: number) {
  const confirmed = window.confirm(`确定删除任务 #${runId} 吗？`)
  if (!confirmed) return

  try {
    await api.deleteRun(runId)
  } catch (error) {
    const message = (error as Error).message || '删除任务失败'
    if (message.includes('运行中的任务不可删除')) {
      const forceConfirmed = window.confirm(
        `任务 #${runId} 仍显示为进行中。若它其实已经卡死，可以强制删除。是否继续？`,
      )
      if (!forceConfirmed) return
      try {
        await api.deleteRun(runId, true)
      } catch (forceError) {
        console.error('[TasksView] force delete run failed', forceError)
        window.alert((forceError as Error).message || '强制删除任务失败')
        return
      }
    } else {
      console.error('[TasksView] delete run failed', error)
      window.alert(message)
      return
    }
  }

  if (selectedRun.value?.id === runId) {
    liveFocused.value = false
  }
  await Promise.all([
    loadInitialRuns({ syncSelection: true }),
    selectedDate.value ? loadHistoryRuns() : Promise.resolve(),
    store.refreshAfterRunCompletion(),
  ])
}

function focusLiveCard() {
  liveFocused.value = true
  clearPreviewFocus()
}

function isTodayRunActive(runId: number) {
  if (liveFocused.value && liveRunId.value === runId) {
    return true
  }
  return !liveFocused.value && selectedRun.value?.id === runId
}

function isTodayRunLive(runId: number) {
  return liveActive.value && liveRunId.value === runId
}

function handleTodayRunSelect(runId: number) {
  if (liveRunId.value === runId && runStream.state.status !== 'idle') {
    focusLiveCard()
    return
  }
  handleSelectRun(runId, todayRuns.value)
}

async function reconcileFinishedRun() {
  const runId = pendingPostRunId.value
  if (runId === null) return
  pendingPostRunId.value = null

  const results = await Promise.allSettled([
    loadInitialRuns({ syncSelection: false }),
    refreshRunDetail(runId),
    store.refreshAfterRunCompletion(),
  ])
  const failed = results.find((result): result is PromiseRejectedResult => result.status === 'rejected')
  if (failed) {
    console.error('[TasksView] post-run refresh failed', failed.reason)
  }
}

watch(
  () => runStream.state.status,
  (status) => {
    if (status !== 'connecting' && status !== 'running') {
      activeManualAction.value = null
    }
    if (status === 'completed' || status === 'failed') {
      void reconcileFinishedRun()
    }
  },
)

onMounted(() => {
  // Replay the post-completion reconcile when returning to this view
  // after a run finished while the component was unmounted.
  if (pendingPostRunId.value !== null) {
    void reconcileFinishedRun()
  }
})

onBeforeUnmount(() => {
  // Intentionally NOT stopping runStream here: the singleton keeps the SSE
  // connection alive across route changes so returning to this tab
  // still shows the live progress and the post-run refresh.
})

const historyDateInput = ref<HTMLInputElement | null>(null)
const activePreviewIndex = ref<number | null>(null)
const activePreviewLoading = ref(false)
const activePreviewError = ref('')
const apiListRef = ref<HTMLElement | null>(null)
const tradeListRef = ref<HTMLElement | null>(null)
const liveOutputRef = ref<HTMLElement | null>(null)
const liveRenderedOutputHtml = ref('')
const shouldFollowApiScroll = ref(true)
const shouldFollowTradeScroll = ref(true)
const shouldFollowLiveOutputScroll = ref(true)

let liveMarkdownFrameId: number | null = null
let liveMarkdownLatestContent = ''
let liveMarkdownLastRenderAt = 0
let liveMarkdownRenderTicket = 0

const detailGridVisible = computed(() => liveVisible.value || !!selectedRun.value)

const displayStatusStartTime = computed(() =>
  liveVisible.value ? liveStartedAtIso.value : selectedRun.value?.startTime ?? null,
)

const displayStatusDuration = computed(() =>
  liveVisible.value ? liveElapsed.value : selectedRun.value?.duration ?? '--',
)

const displayTokenBarVisible = computed(() => liveVisible.value || !!selectedRun.value)

const displayTokenSource = computed(() => {
  if (!liveVisible.value) {
    return selectedRun.value
  }

  if (selectedRun.value?.id === liveRunId.value) {
    return selectedRun.value
  }

  return null
})

const displayInputTokens = computed(() => displayTokenSource.value?.inputTokens ?? '--')

const displayOutputTokens = computed(() => displayTokenSource.value?.outputTokens ?? '--')

const displayTotalTokens = computed(() => displayTokenSource.value?.totalTokens ?? '--')

const displayJin10SourceVisible = computed(
  () => !liveVisible.value && (!!selectedRun.value?.jin10BaseUrl || !!selectedRun.value?.jin10SourceSummary),
)

const displayJin10BaseUrl = computed(() => selectedRun.value?.jin10BaseUrl ?? '')

const displayJin10SourceSummary = computed(() => selectedRun.value?.jin10SourceSummary ?? '')

const displaySelfSelectChangesVisible = computed(
  () => !liveVisible.value && !!selectedRun.value?.detailLoaded,
)

const displaySelfSelectAdditions = computed(() => selectedRun.value?.selfSelectAdditions ?? [])

const displaySelfSelectRemovals = computed(() => selectedRun.value?.selfSelectRemovals ?? [])

const displayStatusDotClass = computed(() => {
  if (liveVisible.value) {
    if (runStream.state.status === 'failed' || runStream.state.status === 'error') {
      return 'dot-tone-error'
    }
    if (runStream.state.status === 'completed') {
      return 'dot-tone-success'
    }
    return 'dot-running'
  }

  return `dot-${statusTone(selectedRun.value?.status ?? 'idle')}`
})

const displayApiDetails = computed<ApiDetail[]>(() => {
  if (liveVisible.value) {
    return runStream.state.apiDetails
  }

  return selectedRun.value?.apiDetails ?? []
})

const displayTradeDetails = computed<TradeDetail[]>(() => {
  if (liveVisible.value) {
    return runStream.state.tradeDetails
  }

  return selectedRun.value?.tradeDetails ?? []
})

const liveRunStillRunning = computed(
  () => runStream.state.status === 'connecting' || runStream.state.status === 'running',
)

const displayApiEmptyText = computed(() =>
  liveVisible.value
    ? liveRunStillRunning.value
      ? '正在等待接口调用...'
      : '本次分析没有生成可展示的接口调用记录。'
    : '本次分析没有生成可展示的接口调用记录。',
)

const displayTradeEmptyText = computed(() =>
  liveVisible.value
    ? liveRunStillRunning.value
      ? '当前暂无交易执行记录。'
      : '本次分析没有生成可展示的模拟操作。'
    : '本次分析没有生成可展示的模拟操作。',
)

const liveOutputIsPlaceholder = computed(
  () => liveVisible.value && !runStream.state.finalStarted,
)

const liveOutputHtml = computed(() => {
  if (liveOutputIsPlaceholder.value) {
    return ''
  }
  return liveRenderedOutputHtml.value || LIVE_OUTPUT_LOADING_HTML
})

const runErrorVisible = computed(() => {
  if (liveVisible.value) {
    return runStream.state.status === 'failed' || runStream.state.status === 'error'
  }

  return selectedRun.value?.status === 'failed'
})

const runErrorMessage = computed(() => {
  if (liveVisible.value) {
    return runStream.state.errorMessage || '当前实时执行失败，请优先检查后端运行日志、模型配置或事件流连接状态。'
  }

  return '当前记录执行失败，请优先检查后端运行日志、模型配置或妙想接口状态。'
})

function getApiItemKey(api: ApiDetail, idx: number) {
  return api.stream_key || api.tool_call_id || `${api.tool_name}-${api.preview_index ?? 'live'}-${idx}`
}

function getApiItemStatusClass(api: ApiDetail) {
  if (api.status === 'running') {
    return 'is-running'
  }
  if (api.status === 'failed' || api.ok === false) {
    return 'is-failed'
  }
  return 'is-success'
}

function getTradeItemStatusClass(trade: TradeDetail) {
  if (trade.status === 'running') {
    return 'is-running'
  }
  if (trade.status === 'failed' || trade.ok === false) {
    return 'is-failed'
  }
  return 'is-success'
}

function getTradeItemKey(trade: TradeDetail, idx: number) {
  return trade.stream_key || `${trade.action}-${trade.symbol}-${trade.preview_index ?? 'live'}-${idx}`
}

const activePreview = computed(() => {
  if (typeof activePreviewIndex.value !== 'number') {
    return null
  }
  if (!selectedRun.value) {
    return null
  }
  return selectedRun.value.rawToolPreviews.find((item) => item.preview_index === activePreviewIndex.value) ?? null
})

const activePreviewText = computed(() => {
  if (activePreviewError.value) {
    return activePreviewError.value
  }
  if (activePreviewLoading.value && activePreview.value?.truncated) {
    return '正在加载完整原文...'
  }
  return activePreview.value?.preview ?? ''
})

const historyDateDisplay = computed(() => {
  if (!selectedDate.value) {
    return '年/月/日'
  }

  const [year, month, day] = selectedDate.value.split('-')
  if (!year || !month || !day) {
    return '年/月/日'
  }

  return `${year}/${month}/${day}`
})

function openHistoryDatePicker() {
  const input = historyDateInput.value
  if (!input) {
    return
  }

  if ('showPicker' in input && typeof input.showPicker === 'function') {
    input.showPicker()
    return
  }

  input.focus()
  input.click()
}

function handleSelectRun(runId: number, runs: typeof todayRuns.value) {
  const target = runs.find((item) => item.id === runId)
  if (!target) {
    return
  }
  liveFocused.value = false
  void selectRun(target, { force: target.id === liveRunId.value })
}

async function focusPreview(index: number | null) {
  if (typeof index !== 'number') {
    return
  }

  if (activePreviewIndex.value === index) {
    clearPreviewFocus()
    return
  }

  activePreviewIndex.value = index
  activePreviewError.value = ''

  const runId = selectedRun.value?.id
  if (typeof runId !== 'number') {
    return
  }

  const preview = selectedRun.value?.rawToolPreviews.find((item) => item.preview_index === index)
  if (!preview || !preview.truncated) {
    activePreviewLoading.value = false
    return
  }

  activePreviewLoading.value = true
  try {
    await ensureRawToolPreview(runId, index)
  } catch (error) {
    console.error('[TasksView] raw preview load failed', error)
    if (activePreviewIndex.value === index) {
      activePreviewError.value = (error as Error).message || '完整原文加载失败'
    }
  } finally {
    if (activePreviewIndex.value === index) {
      activePreviewLoading.value = false
    }
  }
}

function clearPreviewFocus() {
  activePreviewIndex.value = null
  activePreviewLoading.value = false
  activePreviewError.value = ''
}

function isNearBottom(element: HTMLElement) {
  return element.scrollHeight - element.scrollTop - element.clientHeight <= 28
}

async function scrollContainerToBottom(
  elementRef: typeof apiListRef,
  followRef: typeof shouldFollowApiScroll,
) {
  await nextTick()
  const element = elementRef.value
  if (!element || !followRef.value) {
    return
  }
  window.requestAnimationFrame(() => {
    element.scrollTop = element.scrollHeight
  })
}

function handleApiListScroll() {
  const element = apiListRef.value
  shouldFollowApiScroll.value = !element || isNearBottom(element)
}

function handleTradeListScroll() {
  const element = tradeListRef.value
  shouldFollowTradeScroll.value = !element || isNearBottom(element)
}

function handleLiveOutputScroll() {
  const element = liveOutputRef.value
  shouldFollowLiveOutputScroll.value = !element || isNearBottom(element)
}

function clearLiveMarkdownRenderFrame() {
  if (liveMarkdownFrameId !== null) {
    window.cancelAnimationFrame(liveMarkdownFrameId)
    liveMarkdownFrameId = null
  }
}

async function renderLiveMarkdown(content: string, ticket: number) {
  const renderMarkdown = await getLiveMarkdownRenderer()
  if (ticket !== liveMarkdownRenderTicket) {
    return
  }
  liveRenderedOutputHtml.value = content
    ? renderMarkdown(content)
    : LIVE_OUTPUT_LOADING_HTML
}

function flushLiveMarkdownRender(frameTime: number) {
  liveMarkdownFrameId = null

  if (frameTime - liveMarkdownLastRenderAt < LIVE_MARKDOWN_RENDER_MIN_INTERVAL_MS) {
    liveMarkdownFrameId = window.requestAnimationFrame(flushLiveMarkdownRender)
    return
  }

  liveMarkdownLastRenderAt = frameTime
  liveMarkdownRenderTicket += 1
  const ticket = liveMarkdownRenderTicket
  const content = liveMarkdownLatestContent

  void renderLiveMarkdown(content, ticket).finally(() => {
    if (ticket !== liveMarkdownRenderTicket) {
      return
    }
    if (content !== liveMarkdownLatestContent && liveMarkdownFrameId === null) {
      liveMarkdownFrameId = window.requestAnimationFrame(flushLiveMarkdownRender)
    }
  })
}

function scheduleLiveMarkdownRender(content: string) {
  liveMarkdownLatestContent = content
  if (liveMarkdownFrameId !== null) {
    return
  }
  liveMarkdownFrameId = window.requestAnimationFrame(flushLiveMarkdownRender)
}

function resetLiveMarkdownRenderState() {
  clearLiveMarkdownRenderFrame()
  liveMarkdownRenderTicket += 1
  liveMarkdownLatestContent = ''
  liveMarkdownLastRenderAt = 0
  liveRenderedOutputHtml.value = ''
}

function handleOutputSurfaceClick(event: MouseEvent) {
  if (!activePreview.value) {
    return
  }
  const target = event.target
  if (!(target instanceof HTMLElement)) {
    return
  }
  if (target.closest('.compact-item-button')) {
    return
  }
  if (target.closest('.raw-output-content')) {
    return
  }
  clearPreviewFocus()
}

function handleKeydown(event: KeyboardEvent) {
  if (event.key === 'Escape' && activePreviewIndex.value !== null) {
    clearPreviewFocus()
  }
}

watch(
  () => selectedRun.value?.id,
  () => {
    clearPreviewFocus()
  },
  { immediate: true },
)

watch(
  () => displayApiDetails.value.length,
  (nextLength, previousLength = 0) => {
    if (!liveVisible.value || nextLength <= previousLength) {
      return
    }
    void scrollContainerToBottom(apiListRef, shouldFollowApiScroll)
  },
)

watch(
  () => displayTradeDetails.value.length,
  (nextLength, previousLength = 0) => {
    if (!liveVisible.value || nextLength <= previousLength) {
      return
    }
    void scrollContainerToBottom(tradeListRef, shouldFollowTradeScroll)
  },
)

watch(
  () => liveOutputIsPlaceholder.value ? liveOutputText.value : liveOutputHtml.value,
  (nextValue, previousValue = '') => {
    if (!liveVisible.value || nextValue === previousValue) {
      return
    }
    void scrollContainerToBottom(liveOutputRef, shouldFollowLiveOutputScroll)
  },
)

watch(
  () => [liveVisible.value, runStream.state.finalStarted, runStream.state.finalAnswer] as const,
  ([visible, finalStarted, content]) => {
    if (!visible || !finalStarted) {
      resetLiveMarkdownRenderState()
      return
    }
    void getLiveMarkdownRenderer()
    scheduleLiveMarkdownRender(content)
  },
  { immediate: true },
)

watch(
  () => liveVisible.value,
  (visible) => {
    if (!visible) {
      return
    }
    shouldFollowApiScroll.value = true
    shouldFollowTradeScroll.value = true
    shouldFollowLiveOutputScroll.value = true
    void scrollContainerToBottom(apiListRef, shouldFollowApiScroll)
    void scrollContainerToBottom(tradeListRef, shouldFollowTradeScroll)
    void scrollContainerToBottom(liveOutputRef, shouldFollowLiveOutputScroll)
  },
)

onMounted(() => {
  window.addEventListener('keydown', handleKeydown)
})

onBeforeUnmount(() => {
  window.removeEventListener('keydown', handleKeydown)
  resetLiveMarkdownRenderState()
})

</script>

<style scoped>
.manual-trade-button {
  margin-left: 5px;
}

.compact-item-button {
  width: 100%;
  display: flex;
  align-items: center;
  gap: 6px;
  min-height: 25px;
  padding: 5px 8px;
  box-sizing: border-box;
  border: none;
  background: rgba(0, 0, 0, 0.2);
  border-radius: 4px;
  text-align: left;
  cursor: pointer;
  font: inherit;
  font-size: 10px;
  line-height: 1.25;
  color: inherit;
  appearance: none;
  -webkit-appearance: none;
}

.compact-item-button:hover {
  background: rgba(255, 255, 255, 0.04);
}

.compact-item-button.active {
  box-shadow: none;
}

.compact-item-button.active .item-name,
.compact-item-button.active .trade-text-action {
  color: #f6fbff;
}

.compact-item-button.active .item-summary,
.compact-item-button.active .trade-text-summary {
  color: #a9c7e8;
}

.compact-item-button .item-name,
.compact-item-button .trade-text-action {
  font-size: 10.5px;
  line-height: 1.2;
}

.compact-item-button .item-summary,
.compact-item-button .trade-text-summary {
  font-size: 10px;
  line-height: 1.2;
}

.analysis-compact-list {
  height: calc(25px * 3 + 4px * 2);
  min-height: calc(25px * 3 + 4px * 2);
}

.stream-list-shell {
  padding-right: 2px;
}

.stream-list-group {
  display: flex;
  flex-direction: column;
  gap: 4px;
  min-height: 100%;
}

.stream-reveal-enter-active {
  transition:
    opacity 0.28s cubic-bezier(0.16, 1, 0.3, 1),
    transform 0.28s cubic-bezier(0.16, 1, 0.3, 1),
    filter 0.28s cubic-bezier(0.16, 1, 0.3, 1),
    box-shadow 0.28s cubic-bezier(0.16, 1, 0.3, 1);
  transform-origin: left center;
}

.stream-reveal-enter-from {
  opacity: 0;
  transform: translateY(10px) scale(0.92);
  filter: saturate(0.72) brightness(0.95);
  box-shadow: 0 0 0 rgba(15, 23, 42, 0);
}

.stream-reveal-enter-to {
  opacity: 1;
  transform: translateY(0) scale(1);
  filter: saturate(1) brightness(1);
  box-shadow: 0 10px 22px rgba(15, 23, 42, 0.18);
}

.stream-reveal-move {
  transition: transform 0.24s cubic-bezier(0.22, 1, 0.36, 1);
}

.compact-item-status-dot {
  width: 7px;
  height: 7px;
  flex: 0 0 auto;
  align-self: center;
  border-radius: 999px;
  background: rgba(74, 222, 128, 0.92);
  box-shadow: 0 0 8px rgba(74, 222, 128, 0.5);
}

.compact-item-status-dot.is-running {
  background: #31d69f;
  box-shadow: 0 0 10px rgba(49, 214, 159, 0.68);
  animation: live-pulse 1.4s ease-in-out infinite;
}

.compact-item-status-dot.is-success {
  background: rgba(74, 222, 128, 0.96);
  box-shadow: 0 0 8px rgba(74, 222, 128, 0.42);
}

.compact-item-status-dot.is-failed {
  background: rgba(248, 113, 113, 0.96);
  box-shadow: 0 0 8px rgba(248, 113, 113, 0.42);
}

.trade-text-summary {
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  display: block;
  -webkit-line-clamp: unset;
  -webkit-box-orient: unset;
}

.output-surface {
  box-sizing: border-box;
  display: flex;
  flex-direction: column;
  padding: 12px;
  overflow: hidden;
  height: 700px;
  border: 1px solid rgba(145, 170, 214, 0.12);
  border-radius: 8px;
  background: rgba(15, 23, 42, 0.4);
}

.markdown-content {
  flex: 1 1 auto;
  min-height: 0;
  overflow-y: auto;
  scrollbar-width: thin;
  scrollbar-color: transparent transparent;
}

.markdown-content:hover {
  scrollbar-color: rgba(145, 170, 214, 0.3) rgba(15, 23, 42, 0.05);
}

.markdown-content::-webkit-scrollbar {
  width: 5px;
}

.markdown-content::-webkit-scrollbar-track {
  background: transparent;
}

.markdown-content::-webkit-scrollbar-thumb {
  background: rgba(145, 170, 214, 0.25);
  border-radius: 10px;
}

.markdown-content:hover::-webkit-scrollbar-thumb {
  background: rgba(145, 170, 214, 0.45);
}

.markdown-content :deep(p:first-child) {
  margin-top: 0;
}

.markdown-content :deep(p:last-child) {
  margin-bottom: 0;
}

.live-markdown-content :deep(.live-output-loading) {
  margin: 0;
  color: #8ea4c5;
}

.raw-output-content {
  flex: 1 1 auto;
  min-height: 0;
  margin: 0;
  padding: 0;
  overflow: auto;
  scrollbar-width: thin;
  scrollbar-color: transparent transparent;
  color: #e2e8f0;
  font-size: 12px;
  line-height: 1.6;
  white-space: pre-wrap;
  word-break: break-word;
}

.raw-output-content:hover {
  scrollbar-color: rgba(145, 170, 214, 0.3) rgba(15, 23, 42, 0.05);
}

.raw-output-content::-webkit-scrollbar {
  width: 5px;
}

.raw-output-content::-webkit-scrollbar-track {
  background: transparent;
}

.raw-output-content::-webkit-scrollbar-thumb {
  background: rgba(145, 170, 214, 0.25);
  border-radius: 10px;
}

.raw-output-content:hover::-webkit-scrollbar-thumb {
  background: rgba(145, 170, 214, 0.45);
}

.output-surface > .detail-empty-state {
  flex: 1 1 auto;
  min-height: 0;
}

.jin10-diagnostic-section {
  margin-top: 12px;
}

.source-diagnostic-card {
  padding: 10px;
  border: 1px solid rgba(145, 170, 214, 0.14);
  border-radius: 8px;
  background: rgba(15, 23, 42, 0.32);
}

.source-diagnostic-title {
  margin-bottom: 8px;
  color: #dbeafe;
  font-size: 11px;
  font-weight: 600;
  letter-spacing: 0.06em;
}

.source-diagnostic-row {
  display: flex;
  flex-direction: column;
  gap: 3px;
}

.source-diagnostic-row + .source-diagnostic-row {
  margin-top: 8px;
}

.source-diagnostic-label {
  color: #8ea4c5;
  font-size: 10px;
  line-height: 1.2;
}

.source-diagnostic-value {
  color: #f8fafc;
  font-size: 11px;
  line-height: 1.45;
  word-break: break-word;
}

.self-select-section {
  margin-top: 12px;
}

.self-select-card {
  padding: 12px;
  border: 1px solid rgba(145, 170, 214, 0.12);
  border-radius: 8px;
  background: rgba(15, 23, 42, 0.34);
}

.self-select-title {
  margin-bottom: 10px;
  color: #e2e8f0;
  font-size: 12px;
  font-weight: 600;
  letter-spacing: 0.04em;
}

.self-select-groups {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
  gap: 12px;
}

.self-select-group {
  padding: 10px;
  border: 1px solid rgba(145, 170, 214, 0.1);
  border-radius: 8px;
  background: rgba(2, 6, 23, 0.22);
}

.self-select-group-title {
  margin-bottom: 8px;
  color: #cbd5e1;
  font-size: 11px;
  font-weight: 600;
}

.self-select-chip-list {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}

.self-select-chip {
  display: inline-flex;
  align-items: center;
  min-height: 26px;
  padding: 0 10px;
  border-radius: 999px;
  font-size: 11px;
  line-height: 1.4;
  word-break: break-word;
}

.self-select-chip.add {
  color: #d1fae5;
  background: rgba(16, 185, 129, 0.16);
  border: 1px solid rgba(16, 185, 129, 0.22);
}

.self-select-chip.remove {
  color: #fee2e2;
  background: rgba(239, 68, 68, 0.16);
  border: 1px solid rgba(239, 68, 68, 0.22);
}

.self-select-empty {
  min-height: auto;
  padding: 0;
}
</style>
