<template>
  <div class="tab-content">
    <section class="content-grid content-grid-primary">
      <section class="panel memories-panel">
        <div class="panel-head">
          <div class="head-main">
            <h2>记忆梦境</h2>
            <p class="section-kicker">Memory Dreams</p>
          </div>
          <div class="panel-head-actions">
            <button
              class="button ghost small"
              :disabled="loading"
              @click="handleRefresh"
            >
              {{ loading ? '刷新中…' : '刷新' }}
            </button>
            <button
              class="button primary small"
              :disabled="dreamRunning"
              title="手动整理昨天的运行报告与记忆（每晚也会自动执行）"
              @click="handleTriggerDream"
            >
              {{ dreamRunning ? '整理中…' : '立即整理记忆' }}
            </button>
          </div>
        </div>

        <div v-if="errorMessage" class="error-banner">{{ errorMessage }}</div>
        <div v-if="noticeMessage" class="notice">{{ noticeMessage }}</div>

        <!-- 概览卡片 -->
        <div class="memories-overview-grid">
          <div class="metric-card">
            <span class="meta-label">记忆条目</span>
            <strong class="memories-metric-value">{{ memoriesTotal }}</strong>
            <span class="memories-metric-hint">可长期复用的交易经验</span>
          </div>
          <div class="metric-card">
            <span class="meta-label">最近梦境</span>
            <strong class="memories-metric-value">{{ latestDreamLabel }}</strong>
            <span class="memories-metric-hint">{{ latestDreamHint }}</span>
          </div>
          <div class="metric-card">
            <span class="meta-label">记忆活动</span>
            <strong class="memories-metric-value">{{ activitiesTotal }}</strong>
            <span class="memories-metric-hint">读取 / 写入 / 修改 / 删除审计</span>
          </div>
        </div>

        <!-- 分区切换 -->
        <nav class="memories-tabs">
          <button
            v-for="tab in memoryTabs"
            :key="tab.id"
            class="memories-tab-button"
            :class="{ active: activeTab === tab.id }"
            type="button"
            @click="switchTab(tab.id)"
          >
            {{ tab.name }}
          </button>
        </nav>

        <!-- 记忆仓库 -->
        <div v-if="activeTab === 'items'" class="memories-section">
          <div class="memories-toolbar">
            <input
              v-model="searchQuery"
              class="memories-search-input"
              type="search"
              placeholder="搜索记忆内容或原因…"
              @input="scheduleSearchReload"
            />
            <button class="button primary small" type="button" @click="openCreateEditor">
              新建记忆
            </button>
          </div>

          <div v-if="memories.length === 0 && !loading" class="memories-empty">
            {{ searchQuery ? '没有匹配的记忆。' : '还没有记忆。运行任务时模型会自动沉淀经验，也可以手动新建。' }}
          </div>

          <ul v-else class="memories-list">
            <li v-for="memory in memories" :key="memory.id" class="memories-item">
              <div class="memories-item-main">
                <p class="memories-item-content">{{ memory.content }}</p>
                <p v-if="memory.reason" class="memories-item-reason">{{ memory.reason }}</p>
                <div class="memories-item-meta">
                  <span class="status-chip tone-soft">v{{ memory.version }}</span>
                  <span class="memories-item-time">更新于 {{ formatMemoryTime(memory.updated_at) }}</span>
                </div>
              </div>
              <div class="memories-item-actions">
                <button class="button ghost small" type="button" @click="openEditEditor(memory)">
                  编辑
                </button>
                <button
                  class="button ghost small memories-danger-button"
                  type="button"
                  @click="handleRemoveMemory(memory)"
                >
                  删除
                </button>
              </div>
            </li>
          </ul>

          <div v-if="hasMoreMemories" class="memories-more">
            <button class="button ghost small" :disabled="loading" @click="loadMoreMemories">
              加载更多
            </button>
          </div>
        </div>

        <!-- 梦境记录 -->
        <div v-if="activeTab === 'dreams'" class="memories-section">
          <div v-if="dreams.length === 0 && !loading" class="memories-empty">
            还没有梦境记录。每晚到达整理时间会自动执行，也可以点击"立即整理记忆"。
          </div>

          <ul v-else class="memories-list">
            <li v-for="dream in dreams" :key="dream.id" class="memories-item memories-dream-item">
              <button
                class="memories-dream-head"
                type="button"
                @click="toggleDreamDetail(dream.id)"
              >
                <span class="status-chip" :class="dreamTone(dream.status)">
                  {{ dreamStatusText(dream.status) }}
                </span>
                <span class="memories-dream-date">整理 {{ dream.target_date }}</span>
                <span class="memories-dream-summary">
                  {{ dreamSummary(dream) }}
                </span>
                <span class="memories-dream-chevron" aria-hidden="true">
                  {{ expandedDreamId === dream.id ? '▲' : '▼' }}
                </span>
              </button>
              <div v-if="expandedDreamId === dream.id" class="memories-dream-detail">
                <div v-if="dream.failure_reason" class="error-banner">
                  {{ dream.failure_reason }}
                </div>
                <pre v-if="dream.result" class="memories-dream-result">{{ dream.result }}</pre>
                <div class="memories-item-meta">
                  <span>创建 {{ formatMemoryTime(dream.created_at) }}</span>
                  <span v-if="dream.started_at">开始 {{ formatMemoryTime(dream.started_at) }}</span>
                  <span v-if="dream.finished_at">结束 {{ formatMemoryTime(dream.finished_at) }}</span>
                </div>
                <div class="memories-item-actions">
                  <button
                    class="button ghost small memories-danger-button"
                    type="button"
                    :disabled="dream.status === 'running'"
                    @click="handleRemoveDream(dream)"
                  >
                    删除记录
                  </button>
                </div>
              </div>
            </li>
          </ul>

          <div v-if="hasMoreDreams" class="memories-more">
            <button class="button ghost small" :disabled="loading" @click="loadMoreDreams">
              加载更多
            </button>
          </div>
        </div>

        <!-- 记忆活动 -->
        <div v-if="activeTab === 'activities'" class="memories-section">
          <div v-if="activities.length === 0 && !loading" class="memories-empty">
            还没有记忆活动记录。
          </div>

          <ul v-else class="memories-activity-list">
            <li v-for="activity in activities" :key="activity.id" class="memories-activity-row">
              <span class="status-chip" :class="activityTone(activity.action)">
                {{ activityText(activity.action) }}
              </span>
              <span class="memories-activity-detail">
                <template v-if="activity.action === 'read'">
                  检索
                  <template v-if="activity.keywords && activity.keywords.length">
                    「{{ activity.keywords.join('、') }}」
                  </template>
                  <template v-else>记忆</template>
                  命中 {{ activity.result_count ?? 0 }} 条
                </template>
                <template v-else>
                  记忆 #{{ activity.memory_id ?? '--' }}
                </template>
              </span>
              <span class="memories-item-time">{{ formatMemoryTime(activity.created_at) }}</span>
            </li>
          </ul>

          <div v-if="hasMoreActivities" class="memories-more">
            <button class="button ghost small" :disabled="loading" @click="loadMoreActivities">
              加载更多
            </button>
          </div>
        </div>
      </section>
    </section>

    <!-- 新建/编辑对话框 -->
    <div v-if="editorOpen" class="memories-editor-backdrop" @click.self="closeEditor">
      <div class="panel memories-editor">
        <div class="panel-head">
          <div class="head-main">
            <h2>{{ editingMemory ? '编辑记忆' : '新建记忆' }}</h2>
            <p class="section-kicker">
              {{ editingMemory ? `当前版本 v${editingMemory.version}` : 'Memory' }}
            </p>
          </div>
          <button class="button ghost small" type="button" @click="closeEditor">关闭</button>
        </div>

        <div v-if="errorMessage" class="error-banner">{{ errorMessage }}</div>

        <label class="memories-field">
          <span class="meta-label">记忆内容</span>
          <textarea
            v-model="draft.content"
            class="memories-textarea"
            rows="4"
            maxlength="2000"
            placeholder="简洁的投资经验教训，例如：放量滞涨时应减仓而非追高"
          ></textarea>
        </label>
        <label class="memories-field">
          <span class="meta-label">形成原因 / 适用场景（可选）</span>
          <textarea
            v-model="draft.reason"
            class="memories-textarea"
            rows="2"
            maxlength="1000"
            placeholder="例如：2026-10 白酒板块多次验证的量价背离信号"
          ></textarea>
        </label>

        <div class="memories-editor-actions">
          <button class="button ghost" type="button" @click="closeEditor">取消</button>
          <button
            class="button primary"
            type="button"
            :disabled="saving || !draft.content.trim()"
            @click="handleSaveDraft"
          >
            {{ saving ? '保存中…' : '保存' }}
          </button>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted } from 'vue'

import { useMemories } from '@/composables/useMemories'
import type { MemoryTab } from '@/composables/useMemories'
import type { MemoryDream, MemoryItem } from '@/types.ts'
import { formatTime } from '@/utils/formatters.ts'

const {
  activeTab,
  loading,
  errorMessage,
  noticeMessage,
  memories,
  memoriesTotal,
  searchQuery,
  hasMoreMemories,
  editingMemory,
  draft,
  editorOpen,
  saving,
  dreams,
  hasMoreDreams,
  expandedDreamId,
  dreamRunning,
  activities,
  activitiesTotal,
  hasMoreActivities,
  loadMoreMemories,
  scheduleSearchReload,
  openCreateEditor,
  openEditEditor,
  closeEditor,
  saveDraft,
  removeMemory,
  loadMoreDreams,
  triggerDream,
  removeDream,
  toggleDreamDetail,
  loadMoreActivities,
  loadAll,
  switchTab,
  refreshCurrentTab,
} = useMemories()

const memoryTabs: Array<{ id: MemoryTab, name: string }> = [
  { id: 'items', name: '记忆仓库' },
  { id: 'dreams', name: '梦境记录' },
  { id: 'activities', name: '记忆活动' },
]

const latestDream = computed<MemoryDream | null>(() => dreams.value[0] ?? null)

const latestDreamLabel = computed(() => {
  if (latestDream.value === null) return '—'
  return latestDream.value.target_date
})

const latestDreamHint = computed(() => {
  if (latestDream.value === null) return '每晚自动整理昨日经验'
  return `${dreamStatusText(latestDream.value.status)} · ${formatTime(latestDream.value.updated_at)}`
})

function dreamStatusText(status: MemoryDream['status']): string {
  if (status === 'completed') return '已完成'
  if (status === 'running') return '进行中'
  if (status === 'failed') return '失败'
  return '排队中'
}

function dreamTone(status: MemoryDream['status']): string {
  if (status === 'completed') return 'tone-success'
  if (status === 'running') return 'tone-info'
  if (status === 'failed') return 'tone-danger'
  return 'tone-soft'
}

function dreamSummary(dream: MemoryDream): string {
  if (dream.status === 'failed') return dream.failure_reason ?? '整理失败'
  const text = (dream.result ?? '').replace(/\s+/g, ' ').trim()
  if (!text) return dream.status === 'running' ? '正在整理…' : '暂无结果'
  return text.length > 80 ? `${text.slice(0, 80)}…` : text
}

function activityText(action: string): string {
  const mapping: Record<string, string> = {
    read: '读取',
    create: '新建',
    update: '更新',
    delete: '删除',
  }
  return mapping[action] ?? action
}

function activityTone(action: string): string {
  if (action === 'create') return 'tone-success'
  if (action === 'update') return 'tone-info'
  if (action === 'delete') return 'tone-danger'
  return 'tone-soft'
}

function formatMemoryTime(value: string): string {
  return formatTime(value)
}

async function handleRefresh(): Promise<void> {
  await refreshCurrentTab()
}

async function handleTriggerDream(): Promise<void> {
  await triggerDream()
}

async function handleSaveDraft(): Promise<void> {
  await saveDraft()
}

async function handleRemoveMemory(memory: MemoryItem): Promise<void> {
  const confirmed = window.confirm('确定删除这条记忆吗？')
  if (!confirmed) return
  await removeMemory(memory)
}

async function handleRemoveDream(dream: MemoryDream): Promise<void> {
  const confirmed = window.confirm(`确定删除 ${dream.target_date} 的梦境记录吗？`)
  if (!confirmed) return
  await removeDream(dream)
}

onMounted(() => {
  void loadAll()
})
</script>
