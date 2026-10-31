import assert from 'node:assert/strict'
import test from 'node:test'
import { readFileSync } from 'node:fs'

test('tasks view does not render load-more buttons for today or history runs', () => {
  const source = readFileSync(new URL('../src/views/TasksView.vue', import.meta.url), 'utf-8')

  assert.doesNotMatch(source, /加载更多/)
  assert.doesNotMatch(source, /todayHasMore/)
  assert.doesNotMatch(source, /historyHasMore/)
  assert.doesNotMatch(source, /loadMoreTodayRuns/)
  assert.doesNotMatch(source, /loadMoreHistoryRuns/)
})

test('tasks view keeps the today run group visible while a live placeholder card exists', () => {
  const source = readFileSync(new URL('../src/views/TasksView.vue', import.meta.url), 'utf-8')

  assert.match(source, /todayRuns\.length \|\| livePlaceholderVisible/)
})

test('analysis runs composable loads up to 100 runs per request without load-more pagination', () => {
  const source = readFileSync(new URL('../src/composables/useAnalysisRuns.ts', import.meta.url), 'utf-8')

  assert.match(source, /const RUNS_PAGE_SIZE = 100/)
  assert.match(source, /listRunsPage\(\{ limit: RUNS_PAGE_SIZE \}\)/)
  assert.doesNotMatch(source, /async function loadMoreTodayRuns/)
  assert.doesNotMatch(source, /async function loadMoreHistoryRuns/)
})

test('tasks view renders original and revised analysis panels after consistency correction', () => {
  const viewSource = readFileSync(new URL('../src/views/TasksView.vue', import.meta.url), 'utf-8')
  const composableSource = readFileSync(new URL('../src/composables/useAnalysisRuns.ts', import.meta.url), 'utf-8')

  assert.match(viewSource, /原始分析/)
  assert.match(viewSource, /修正后结论/)
  assert.match(viewSource, /displaySplitOutputVisible/)
  assert.match(composableSource, /original_final_answer/)
  assert.match(composableSource, /CONSISTENCY_REVISION_MARKER/)
})

test('tasks view renders per-source diagnosis text from prefetched analysis metadata', () => {
  const viewSource = readFileSync(new URL('../src/views/TasksView.vue', import.meta.url), 'utf-8')
  const composableSource = readFileSync(new URL('../src/composables/useAnalysisRuns.ts', import.meta.url), 'utf-8')

  assert.match(viewSource, /本轮 \{\{ source\.label \}\} 新闻诊断/)
  assert.match(viewSource, /displaySourceDiagnostics/)
  assert.match(viewSource, /诊断失败原因/)
  assert.match(viewSource, /source.metrics.length/)
  assert.match(viewSource, /source-diagnostic-metric-label/)
  assert.match(composableSource, /analysis_text/)
  assert.match(composableSource, /analysis_meta/)
  assert.match(composableSource, /label: '抓取条数'/)
  assert.match(composableSource, /request_count/)
  assert.match(composableSource, /chunk_count/)
  assert.match(composableSource, /getSourceDiagnostics/)
})

test('analysis summary builder keeps source labels in run detail mapping flow', () => {
  const viewSource = readFileSync(new URL('../src/views/TasksView.vue', import.meta.url), 'utf-8')
  const composableSource = readFileSync(new URL('../src/composables/useAnalysisRuns.ts', import.meta.url), 'utf-8')

  assert.match(viewSource, /trade-source-chip/)
  assert.match(viewSource, /trade\.source_labels\?\.length/)
  assert.match(composableSource, /const tradeSourceLabels = getTradeSourceLabels\(detail\)/)
  assert.match(composableSource, /mapTradeDetails\(detail\.trade_orders, detail\.executed_actions, tradeSourceLabels\)/)
})

test('tasks view renders the backend execution report card', () => {
  const source = readFileSync(new URL('../src/views/TasksView.vue', import.meta.url), 'utf-8')

  assert.ok(source.includes('执行结果（后端权威记录）'))
  assert.ok(source.includes('displayBackendExecutionReport'))
  assert.ok(source.includes('声明未获执行确认'))
  assert.ok(source.includes('数据门禁拦截'))
  assert.ok(source.includes('backendExecutionReport'))
  // 权威口径提示
  assert.ok(source.includes('以后端记录为准'))
})

test('analysis runs composable parses backend execution report payload', () => {
  const source = readFileSync(
    new URL('../src/composables/useAnalysisRuns.ts', import.meta.url),
    'utf-8',
  )

  assert.ok(source.includes('interface BackendExecutionReportViewModel'))
  assert.ok(source.includes('getBackendExecutionReport'))
  assert.ok(source.includes("execution_report"))
  assert.ok(source.includes('not_called_claims'))
  assert.ok(source.includes('blocked_actions'))
  assert.ok(source.includes('missing_required_tools'))
  // 状态枚举映射
  assert.ok(source.includes('SUBMITTED'))
  assert.ok(source.includes('VERIFIED'))
  assert.ok(source.includes('NOOP'))
  assert.ok(source.includes('NOT_CALLED'))
})

test('tasks view provides retry button for failed actions', () => {
  const source = readFileSync(new URL('../src/views/TasksView.vue', import.meta.url), 'utf-8')

  assert.ok(source.includes('handleRetryAction'))
  assert.ok(source.includes('retryingActionIds'))
  assert.ok(source.includes("action.status === 'failed_terminal'"))
  assert.ok(source.includes('api.retryRunAction'))
  // 重试后刷新运行详情
  assert.ok(source.includes('loadRunDetail'))
})

test('api service exposes retryRunAction', () => {
  const source = readFileSync(new URL('../src/services/api.ts', import.meta.url), 'utf-8')

  assert.ok(source.includes('retryRunAction'))
  assert.ok(source.includes('/runs/actions/${actionId}/retry'))
})
