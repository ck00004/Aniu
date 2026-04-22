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

  assert.match(source, /todayRuns\.length \|\| todaySuccessCount \|\| todayFailedCount \|\| livePlaceholderVisible/)
})

test('analysis runs composable loads up to 100 runs per request without load-more pagination', () => {
  const source = readFileSync(new URL('../src/composables/useAnalysisRuns.ts', import.meta.url), 'utf-8')

  assert.match(source, /const RUNS_PAGE_SIZE = 100/)
  assert.match(source, /listRunsPage\(\{ limit: RUNS_PAGE_SIZE \}\)/)
  assert.doesNotMatch(source, /async function loadMoreTodayRuns/)
  assert.doesNotMatch(source, /async function loadMoreHistoryRuns/)
})
