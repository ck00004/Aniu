import { computed, ref } from 'vue'

import { api } from '../services/api.ts'
import type { MemoryActivity, MemoryDream, MemoryItem } from '../types.ts'

const PAGE_SIZE = 20
const ACTIVITY_PAGE_SIZE = 30

export type MemoryTab = 'dreams' | 'items' | 'activities'

export interface MemoryDraft {
  content: string
  reason: string
}

export function useMemories() {
  const activeTab = ref<MemoryTab>('items')
  const loading = ref(false)
  const errorMessage = ref('')
  const noticeMessage = ref('')

  // 记忆仓库
  const memories = ref<MemoryItem[]>([])
  const memoriesTotal = ref(0)
  const memoriesOffset = ref(0)
  const searchQuery = ref('')
  const searchDebounceTimer = ref<number | null>(null)

  // 编辑/新建
  const editingMemory = ref<MemoryItem | null>(null)
  const draft = ref<MemoryDraft>({ content: '', reason: '' })
  const editorOpen = ref(false)
  const saving = ref(false)
  const versionConflict = ref(false)

  // 梦境
  const dreams = ref<MemoryDream[]>([])
  const dreamsTotal = ref(0)
  const dreamsOffset = ref(0)
  const expandedDreamId = ref<number | null>(null)
  const dreamRunning = ref(false)
  const backfillDate = ref('')
  const backfillMaxDate = ref('')

  function refreshBackfillMaxDate(): void {
    const yesterday = new Date(Date.now() - 24 * 60 * 60 * 1000)
    backfillMaxDate.value = yesterday.toISOString().slice(0, 10)
  }

  refreshBackfillMaxDate()

  // 活动
  const activities = ref<MemoryActivity[]>([])
  const activitiesTotal = ref(0)
  const activitiesOffset = ref(0)

  // 已加载条数 vs 总数（追加式分页；offset 是最后一页的起点，
  // 用 offset+length 会重复计数）
  const hasMoreMemories = computed(
    () => memories.value.length < memoriesTotal.value,
  )
  const hasMoreDreams = computed(
    () => dreams.value.length < dreamsTotal.value,
  )
  const hasMoreActivities = computed(
    () => activities.value.length < activitiesTotal.value,
  )

  function resetMessages(): void {
    errorMessage.value = ''
    noticeMessage.value = ''
    versionConflict.value = false
  }

  async function loadMemories(): Promise<void> {
    loading.value = true
    try {
      const payload = await api.listMemories({
        query: searchQuery.value.trim() || undefined,
        limit: PAGE_SIZE,
        offset: memoriesOffset.value,
      })
      memories.value = payload.items
      memoriesTotal.value = payload.total
    } catch (error) {
      errorMessage.value = (error as Error).message
    } finally {
      loading.value = false
    }
  }

  async function loadMoreMemories(): Promise<void> {
    if (!hasMoreMemories.value || loading.value) return
    memoriesOffset.value += PAGE_SIZE
    try {
      const payload = await api.listMemories({
        query: searchQuery.value.trim() || undefined,
        limit: PAGE_SIZE,
        offset: memoriesOffset.value,
      })
      memories.value = [...memories.value, ...payload.items]
      memoriesTotal.value = payload.total
    } catch (error) {
      memoriesOffset.value -= PAGE_SIZE
      errorMessage.value = (error as Error).message
    }
  }

  function scheduleSearchReload(): void {
    if (searchDebounceTimer.value !== null) {
      window.clearTimeout(searchDebounceTimer.value)
    }
    searchDebounceTimer.value = window.setTimeout(() => {
      searchDebounceTimer.value = null
      memoriesOffset.value = 0
      void loadMemories()
    }, 300)
  }

  function openCreateEditor(): void {
    resetMessages()
    editingMemory.value = null
    draft.value = { content: '', reason: '' }
    editorOpen.value = true
  }

  function openEditEditor(memory: MemoryItem): void {
    resetMessages()
    editingMemory.value = memory
    draft.value = {
      content: memory.content,
      reason: memory.reason ?? '',
    }
    editorOpen.value = true
  }

  function closeEditor(): void {
    editorOpen.value = false
    editingMemory.value = null
    draft.value = { content: '', reason: '' }
    versionConflict.value = false
  }

  async function saveDraft(): Promise<boolean> {
    const content = draft.value.content.trim()
    if (!content) {
      errorMessage.value = '记忆内容不能为空。'
      return false
    }
    saving.value = true
    resetMessages()
    try {
      if (editingMemory.value === null) {
        await api.createMemory(content, draft.value.reason.trim() || null)
        noticeMessage.value = '记忆已创建。'
      } else {
        await api.updateMemory(editingMemory.value.id, {
          content,
          reason: draft.value.reason.trim() || null,
          expected_version: editingMemory.value.version,
        })
        noticeMessage.value = '记忆已更新。'
      }
      closeEditor()
      memoriesOffset.value = 0
      await loadMemories()
      return true
    } catch (error) {
      const message = (error as Error).message || ''
      if (message.includes('版本冲突') || message.includes('409')) {
        versionConflict.value = true
        errorMessage.value = `${message}（请关闭后重新编辑最新版本）`
      } else {
        errorMessage.value = message
      }
      return false
    } finally {
      saving.value = false
    }
  }

  async function removeMemory(memory: MemoryItem): Promise<boolean> {
    resetMessages()
    try {
      await api.deleteMemory(memory.id, memory.version)
      noticeMessage.value = '记忆已删除。'
      memoriesOffset.value = 0
      await loadMemories()
      return true
    } catch (error) {
      const message = (error as Error).message || ''
      if (message.includes('版本冲突') || message.includes('409')) {
        errorMessage.value = `${message}（请刷新后重试）`
      } else {
        errorMessage.value = message
      }
      return false
    }
  }

  async function loadDreams(): Promise<void> {
    loading.value = true
    try {
      const payload = await api.listMemoryDreams({
        limit: PAGE_SIZE,
        offset: dreamsOffset.value,
      })
      dreams.value = payload.items
      dreamsTotal.value = payload.total
    } catch (error) {
      errorMessage.value = (error as Error).message
    } finally {
      loading.value = false
    }
  }

  async function loadMoreDreams(): Promise<void> {
    if (!hasMoreDreams.value || loading.value) return
    dreamsOffset.value += PAGE_SIZE
    try {
      const payload = await api.listMemoryDreams({
        limit: PAGE_SIZE,
        offset: dreamsOffset.value,
      })
      dreams.value = [...dreams.value, ...payload.items]
      dreamsTotal.value = payload.total
    } catch (error) {
      dreamsOffset.value -= PAGE_SIZE
      errorMessage.value = (error as Error).message
    }
  }

  async function triggerDream(targetDate?: string): Promise<boolean> {
    if (dreamRunning.value) return false
    dreamRunning.value = true
    resetMessages()
    try {
      const payload = await api.runMemoryDream(targetDate)
      noticeMessage.value = payload.started
        ? (targetDate
          ? `已启动 ${targetDate} 的梦境整理，稍后刷新查看结果。`
          : '梦境整理已启动，稍后刷新查看结果。')
        : '梦境任务已在执行或排队中。'
      dreamsOffset.value = 0
      await loadDreams()
      return true
    } catch (error) {
      errorMessage.value = (error as Error).message
      return false
    } finally {
      dreamRunning.value = false
    }
  }

  async function backfillDream(): Promise<boolean> {
    const targetDate = backfillDate.value.trim()
    if (!targetDate) {
      resetMessages()
      errorMessage.value = '请先选择要补整理的日期。'
      return false
    }
    // 只允许补整理昨天及更早：当天的数据尚未完整，且会占用当晚的
    // 自动整理（调度器发现当日已有梦境记录时会跳过触发）。
    refreshBackfillMaxDate()
    if (targetDate > backfillMaxDate.value) {
      resetMessages()
      errorMessage.value = '只能补整理昨天及更早的日期；当天数据尚未完整，且会占用当晚的自动整理。'
      return false
    }
    const succeeded = await triggerDream(targetDate)
    if (succeeded) {
      backfillDate.value = ''
    }
    return succeeded
  }

  async function removeDream(dream: MemoryDream): Promise<boolean> {
    resetMessages()
    try {
      await api.deleteMemoryDream(dream.id)
      noticeMessage.value = '梦境记录已删除。'
      dreamsOffset.value = 0
      await loadDreams()
      return true
    } catch (error) {
      errorMessage.value = (error as Error).message
      return false
    }
  }

  function toggleDreamDetail(dreamId: number): void {
    expandedDreamId.value = expandedDreamId.value === dreamId ? null : dreamId
  }

  async function loadActivities(): Promise<void> {
    loading.value = true
    try {
      const payload = await api.listMemoryActivities({
        limit: ACTIVITY_PAGE_SIZE,
        offset: activitiesOffset.value,
      })
      activities.value = payload.items
      activitiesTotal.value = payload.total
    } catch (error) {
      errorMessage.value = (error as Error).message
    } finally {
      loading.value = false
    }
  }

  async function loadMoreActivities(): Promise<void> {
    if (!hasMoreActivities.value || loading.value) return
    activitiesOffset.value += ACTIVITY_PAGE_SIZE
    try {
      const payload = await api.listMemoryActivities({
        limit: ACTIVITY_PAGE_SIZE,
        offset: activitiesOffset.value,
      })
      activities.value = [...activities.value, ...payload.items]
      activitiesTotal.value = payload.total
    } catch (error) {
      activitiesOffset.value -= ACTIVITY_PAGE_SIZE
      errorMessage.value = (error as Error).message
    }
  }

  async function loadAll(): Promise<void> {
    resetMessages()
    memoriesOffset.value = 0
    dreamsOffset.value = 0
    activitiesOffset.value = 0
    await Promise.allSettled([loadMemories(), loadDreams(), loadActivities()])
  }

  async function switchTab(tab: MemoryTab): Promise<void> {
    if (activeTab.value === tab) return
    activeTab.value = tab
    resetMessages()
    if (tab === 'items' && memories.value.length === 0) {
      await loadMemories()
    } else if (tab === 'dreams' && dreams.value.length === 0) {
      await loadDreams()
    } else if (tab === 'activities' && activities.value.length === 0) {
      await loadActivities()
    }
  }

  function refreshCurrentTab(): Promise<void> {
    if (activeTab.value === 'items') {
      memoriesOffset.value = 0
      return loadMemories()
    }
    if (activeTab.value === 'dreams') {
      dreamsOffset.value = 0
      return loadDreams()
    }
    activitiesOffset.value = 0
    return loadActivities()
  }

  return {
    activeTab,
    loading,
    errorMessage,
    noticeMessage,
    memories,
    memoriesTotal,
    memoriesOffset,
    searchQuery,
    hasMoreMemories,
    editingMemory,
    draft,
    editorOpen,
    saving,
    versionConflict,
    dreams,
    dreamsTotal,
    hasMoreDreams,
    expandedDreamId,
    dreamRunning,
    backfillDate,
    backfillMaxDate,
    activities,
    activitiesTotal,
    hasMoreActivities,
    loadMemories,
    loadMoreMemories,
    scheduleSearchReload,
    openCreateEditor,
    openEditEditor,
    closeEditor,
    saveDraft,
    removeMemory,
    loadDreams,
    loadMoreDreams,
    triggerDream,
    backfillDream,
    removeDream,
    toggleDreamDetail,
    loadActivities,
    loadMoreActivities,
    loadAll,
    switchTab,
    refreshCurrentTab,
  }
}
