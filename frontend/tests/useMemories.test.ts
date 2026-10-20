import assert from 'node:assert/strict'
import test from 'node:test'

import { useMemories } from '../src/composables/useMemories.ts'
import { api } from '../src/services/api.ts'
import type { MemoryActivity, MemoryDream, MemoryItem } from '../src/types.ts'

function createMemoryItem(overrides: Partial<MemoryItem> = {}): MemoryItem {
  return {
    id: 1,
    content: '放量滞涨应减仓',
    reason: '量价背离信号',
    version: 1,
    created_at: '2026-10-16T08:00:00Z',
    updated_at: '2026-10-16T08:00:00Z',
    ...overrides,
  }
}

function createMemoryDream(overrides: Partial<MemoryDream> = {}): MemoryDream {
  return {
    id: 1,
    target_date: '2026-10-15',
    status: 'completed',
    result: '整理完成：新增 2 条记忆。',
    failure_reason: null,
    created_at: '2026-10-16T00:30:00Z',
    updated_at: '2026-10-16T00:31:00Z',
    started_at: '2026-10-16T00:30:00Z',
    finished_at: '2026-10-16T00:31:00Z',
    ...overrides,
  }
}

function createMemoryActivity(overrides: Partial<MemoryActivity> = {}): MemoryActivity {
  return {
    id: 1,
    action: 'read',
    keywords: ['白酒'],
    result_count: 2,
    memory_id: null,
    created_at: '2026-10-16T09:00:00Z',
    ...overrides,
  }
}

function installApiMocks(overrides: Record<string, unknown> = {}) {
  const calls: Array<{ method: string, args: unknown[] }> = []
  const defaults: Record<string, unknown> = {
    listMemories: async (options = {}) => {
      calls.push({ method: 'listMemories', args: [options] })
      return {
        total: 1,
        items: [createMemoryItem()],
      }
    },
    createMemory: async (content: string, reason: string | null) => {
      calls.push({ method: 'createMemory', args: [content, reason] })
      return createMemoryItem({ id: 2, content, reason, version: 1 })
    },
    updateMemory: async (memoryId: number, payload: Record<string, unknown>) => {
      calls.push({ method: 'updateMemory', args: [memoryId, payload] })
      return createMemoryItem({
        id: memoryId,
        content: String(payload.content ?? ''),
        version: 2,
      })
    },
    deleteMemory: async (memoryId: number, expectedVersion: number) => {
      calls.push({ method: 'deleteMemory', args: [memoryId, expectedVersion] })
      return { deleted: true }
    },
    listMemoryActivities: async (options = {}) => {
      calls.push({ method: 'listMemoryActivities', args: [options] })
      return {
        total: 1,
        items: [createMemoryActivity()],
      }
    },
    listMemoryDreams: async (options = {}) => {
      calls.push({ method: 'listMemoryDreams', args: [options] })
      return {
        total: 1,
        items: [createMemoryDream()],
      }
    },
    getMemoryDream: async (dreamId: number) => {
      calls.push({ method: 'getMemoryDream', args: [dreamId] })
      return createMemoryDream({ id: dreamId })
    },
    runMemoryDream: async (targetDate?: string) => {
      calls.push({ method: 'runMemoryDream', args: [targetDate] })
      return { dream: createMemoryDream(), started: true }
    },
    deleteMemoryDream: async (dreamId: number) => {
      calls.push({ method: 'deleteMemoryDream', args: [dreamId] })
      return { deleted: true }
    },
    ...overrides,
  }

  const originals: Record<string, unknown> = {}
  for (const [key, value] of Object.entries(defaults)) {
    originals[key] = (api as Record<string, unknown>)[key]
    ;(api as Record<string, unknown>)[key] = value
  }

  return {
    calls,
    restore() {
      for (const [key, value] of Object.entries(originals)) {
        ;(api as Record<string, unknown>)[key] = value
      }
    },
  }
}

test('loadAll 填充记忆/梦境/活动三组数据', async () => {
  const mocks = installApiMocks()
  try {
    const manager = useMemories()
    await manager.loadAll()

    assert.equal(manager.memories.value.length, 1)
    assert.equal(manager.memoriesTotal.value, 1)
    assert.equal(manager.dreams.value.length, 1)
    assert.equal(manager.activities.value.length, 1)
    assert.equal(manager.errorMessage.value, '')
  } finally {
    mocks.restore()
  }
})

test('loadMemories 透传搜索关键词并处理错误', async () => {
  const mocks = installApiMocks({
    listMemories: async (options = {}) => {
      if (options.query === '白酒') {
        return { total: 0, items: [] }
      }
      throw new Error('网络错误')
    },
  })
  try {
    const manager = useMemories()
    manager.searchQuery.value = '白酒'
    await manager.loadMemories()
    assert.equal(manager.memories.value.length, 0)

    manager.searchQuery.value = ''
    await manager.loadMemories()
    assert.equal(manager.errorMessage.value, '网络错误')
  } finally {
    mocks.restore()
  }
})

test('saveDraft 新建记忆后刷新列表', async () => {
  const mocks = installApiMocks()
  try {
    const manager = useMemories()
    manager.openCreateEditor()
    manager.draft.value = { content: '高开低走不追高', reason: '教训' }

    const saved = await manager.saveDraft()
    assert.equal(saved, true)
    assert.equal(mocks.calls.find((c) => c.method === 'createMemory')?.args[0], '高开低走不追高')
    assert.equal(manager.editorOpen.value, false)
    assert.equal(manager.noticeMessage.value, '记忆已创建。')
  } finally {
    mocks.restore()
  }
})

test('saveDraft 空内容被拒绝且不发起请求', async () => {
  const mocks = installApiMocks()
  try {
    const manager = useMemories()
    manager.openCreateEditor()
    manager.draft.value = { content: '   ', reason: '' }

    const saved = await manager.saveDraft()
    assert.equal(saved, false)
    assert.equal(manager.errorMessage.value, '记忆内容不能为空。')
    assert.equal(mocks.calls.length, 0)
  } finally {
    mocks.restore()
  }
})

test('saveDraft 编辑携带乐观锁版本号', async () => {
  const mocks = installApiMocks()
  try {
    const manager = useMemories()
    await manager.loadMemories()
    const memory = manager.memories.value[0]!
    manager.openEditEditor(memory)
    manager.draft.value = { content: '更新后的内容', reason: '' }

    const saved = await manager.saveDraft()
    assert.equal(saved, true)
    const updateCall = mocks.calls.find((c) => c.method === 'updateMemory')
    assert.deepEqual(updateCall?.args[1], {
      content: '更新后的内容',
      reason: null,
      expected_version: memory.version,
    })
  } finally {
    mocks.restore()
  }
})

test('saveDraft 版本冲突被识别并提示', async () => {
  const mocks = installApiMocks({
    updateMemory: async () => {
      throw new Error('记忆 1 版本冲突：当前版本 2，请求携带 expected_version=1。请重新读取后重试。')
    },
  })
  try {
    const manager = useMemories()
    await manager.loadMemories()
    manager.openEditEditor(manager.memories.value[0]!)
    manager.draft.value = { content: '新内容', reason: '' }

    const saved = await manager.saveDraft()
    assert.equal(saved, false)
    assert.equal(manager.versionConflict.value, true)
    assert.match(manager.errorMessage.value, /版本冲突/)
  } finally {
    mocks.restore()
  }
})

test('removeMemory 携带版本号并在冲突时提示刷新', async () => {
  const mocks = installApiMocks({
    deleteMemory: async () => {
      throw new Error('记忆 1 版本冲突：请重新读取后重试。')
    },
  })
  try {
    const manager = useMemories()
    await manager.loadMemories()
    const removed = await manager.removeMemory(manager.memories.value[0]!)
    assert.equal(removed, false)
    assert.match(manager.errorMessage.value, /刷新后重试/)
  } finally {
    mocks.restore()
  }
})

test('triggerDream 触发后刷新梦境列表', async () => {
  const mocks = installApiMocks()
  try {
    const manager = useMemories()
    const triggered = await manager.triggerDream()
    assert.equal(triggered, true)
    assert.equal(manager.dreamRunning.value, false)
    assert.equal(manager.noticeMessage.value.includes('已启动'), true)
    assert.equal(manager.dreams.value.length, 1)
  } finally {
    mocks.restore()
  }
})

test('triggerDream 失败时透出错误', async () => {
  const mocks = installApiMocks({
    runMemoryDream: async () => {
      throw new Error('未配置大模型接口')
    },
  })
  try {
    const manager = useMemories()
    const triggered = await manager.triggerDream()
    assert.equal(triggered, false)
    assert.equal(manager.errorMessage.value, '未配置大模型接口')
  } finally {
    mocks.restore()
  }
})

test('switchTab 懒加载对应分区数据', async () => {
  const mocks = installApiMocks()
  try {
    const manager = useMemories()
    // 初始 tab 是 items，只加载记忆
    await manager.switchTab('items')
    assert.equal(manager.activeTab.value, 'items')

    // 切到梦境（未加载过）→ 触发 listMemoryDreams
    await manager.switchTab('dreams')
    assert.equal(manager.activeTab.value, 'dreams')
    assert.equal(manager.dreams.value.length, 1)

    // 再切回梦境（已有数据）→ 不重复请求
    const dreamCalls = mocks.calls.filter((c) => c.method === 'listMemoryDreams')
    await manager.switchTab('items')
    await manager.switchTab('dreams')
    assert.equal(
      mocks.calls.filter((c) => c.method === 'listMemoryDreams').length,
      dreamCalls.length,
    )
  } finally {
    mocks.restore()
  }
})

test('toggleDreamDetail 展开/收起梦境详情', async () => {
  const mocks = installApiMocks()
  try {
    const manager = useMemories()
    await manager.loadDreams()
    const dreamId = manager.dreams.value[0]!.id

    manager.toggleDreamDetail(dreamId)
    assert.equal(manager.expandedDreamId.value, dreamId)

    manager.toggleDreamDetail(dreamId)
    assert.equal(manager.expandedDreamId.value, null)
  } finally {
    mocks.restore()
  }
})

test('loadMoreMemories 追加分页并在失败时回退偏移', async () => {
  let listCalls = 0
  const mocks = installApiMocks({
    listMemories: async (options = {}) => {
      listCalls += 1
      if (listCalls === 1) {
        return {
          total: 25,
          items: Array.from({ length: 20 }, (_, i) => createMemoryItem({ id: i + 1 })),
        }
      }
      if (listCalls === 2) {
        return {
          total: 25,
          items: [createMemoryItem({ id: 21 })],
        }
      }
      throw new Error('分页失败')
    },
  })
  try {
    const manager = useMemories()
    await manager.loadMemories()
    assert.equal(manager.hasMoreMemories.value, true)

    await manager.loadMoreMemories()
    assert.equal(manager.memories.value.length, 21)

    // 第三次（失败）→ 偏移回退且错误可见
    await manager.loadMoreMemories()
    assert.equal(manager.memories.value.length, 21)
    assert.equal(manager.errorMessage.value, '分页失败')
  } finally {
    mocks.restore()
  }
})

test('triggerDream 指定日期时透传目标日期', async () => {
  const mocks = installApiMocks()
  try {
    const manager = useMemories()
    const triggered = await manager.triggerDream('2026-10-16')
    assert.equal(triggered, true)
    const call = mocks.calls.find(({ method }) => method === 'runMemoryDream')
    assert.ok(call)
    assert.deepEqual(call.args, ['2026-10-16'])
    assert.equal(manager.noticeMessage.value.includes('2026-10-16'), true)
  } finally {
    mocks.restore()
  }
})

test('backfillDream 未选日期时拒绝且不发起请求', async () => {
  const mocks = installApiMocks()
  try {
    const manager = useMemories()
    manager.backfillDate.value = ''
    const succeeded = await manager.backfillDream()
    assert.equal(succeeded, false)
    assert.equal(manager.errorMessage.value.includes('选择'), true)
    assert.equal(
      mocks.calls.some(({ method }) => method === 'runMemoryDream'),
      false,
    )
  } finally {
    mocks.restore()
  }
})

test('backfillDream 拒绝今天及未来日期（会占用当晚自动整理）', async () => {
  const mocks = installApiMocks()
  const today = new Date().toISOString().slice(0, 10)
  try {
    const manager = useMemories()
    manager.backfillDate.value = today
    const succeeded = await manager.backfillDream()
    assert.equal(succeeded, false)
    assert.equal(manager.errorMessage.value.includes('昨天及更早'), true)

    manager.backfillDate.value = '2999-01-01'
    const futureAttempt = await manager.backfillDream()
    assert.equal(futureAttempt, false)
    assert.equal(
      mocks.calls.some(({ method }) => method === 'runMemoryDream'),
      false,
    )
  } finally {
    mocks.restore()
  }
})

test('backfillDream 历史日期发起补整理并清空选择', async () => {
  const mocks = installApiMocks()
  const yesterday = new Date(Date.now() - 24 * 60 * 60 * 1000)
    .toISOString()
    .slice(0, 10)
  try {
    const manager = useMemories()
    manager.backfillDate.value = yesterday
    const succeeded = await manager.backfillDream()
    assert.equal(succeeded, true)
    const call = mocks.calls.find(({ method }) => method === 'runMemoryDream')
    assert.ok(call)
    assert.deepEqual(call.args, [yesterday])
    assert.equal(manager.backfillDate.value, '')
    assert.equal(manager.noticeMessage.value.includes(yesterday), true)
  } finally {
    mocks.restore()
  }
})
