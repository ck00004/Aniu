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

test('tasks view renders Jin10 diagnosis text from prefetched analysis metadata', () => {
  const viewSource = readFileSync(new URL('../src/views/TasksView.vue', import.meta.url), 'utf-8')
  const composableSource = readFileSync(new URL('../src/composables/useAnalysisRuns.ts', import.meta.url), 'utf-8')

  assert.match(viewSource, /本轮 Jin10 新闻诊断/)
  assert.match(viewSource, /displayJin10DiagnosisText/)
  assert.match(viewSource, /诊断失败原因/)
  assert.match(viewSource, /displayJin10Metrics/)
  assert.match(viewSource, /source-diagnostic-metric-label/)
  assert.match(composableSource, /analysis_text/)
  assert.match(composableSource, /analysis_meta/)
  assert.match(composableSource, /label: '抓取条数'/)
  assert.match(composableSource, /request_count/)
  assert.match(composableSource, /chunk_count/)
  assert.match(composableSource, /jin10FailureReason/)
})
