import { computed, ref } from 'vue'

import { api } from '../services/api.ts'
import type { ChatSession } from '../types.ts'

const sessions = ref<ChatSession[]>([])
const currentSessionId = ref<number | null>(null)
const loading = ref(false)
const errorMessage = ref('')

function sortSessions(list: ChatSession[]): ChatSession[] {
  return [...list].sort((a, b) => {
    const ta = new Date(a.last_message_at ?? a.updated_at).getTime()
    const tb = new Date(b.last_message_at ?? b.updated_at).getTime()
    return tb - ta
  })
}

function mergeSession(nextSession: ChatSession): void {
  const exists = sessions.value.some((item) => item.id === nextSession.id)
  sessions.value = sortSessions(
    exists
      ? sessions.value.map((item) => (item.id === nextSession.id ? nextSession : item))
      : [nextSession, ...sessions.value],
  )
}

export function useChatSessions() {
  const currentSession = computed<ChatSession | null>(() => {
    if (currentSessionId.value === null) return null
    return sessions.value.find((item) => item.id === currentSessionId.value) ?? null
  })

  async function loadSessions(): Promise<void> {
    loading.value = true
    errorMessage.value = ''
    try {
      const list = await api.listChatSessions()
      sessions.value = sortSessions(list)
    } catch (error) {
      errorMessage.value = (error as Error).message
    } finally {
      loading.value = false
    }
  }

  async function createSession(title?: string): Promise<ChatSession> {
    const created = await api.createChatSession(title)
    mergeSession(created)
    currentSessionId.value = created.id
    return created
  }

  async function deleteSession(sessionId: number): Promise<void> {
    await api.deleteChatSession(sessionId)
    sessions.value = sessions.value.filter((item) => item.id !== sessionId)
    if (currentSessionId.value === sessionId) {
      currentSessionId.value = sessions.value[0]?.id ?? null
    }
  }

  async function renameSession(sessionId: number, title: string): Promise<void> {
    const updated = await api.renameChatSession(sessionId, title)
    mergeSession(updated)
  }

  function selectSession(sessionId: number | null): void {
    currentSessionId.value = sessionId
  }

  function touchSession(sessionId: number, patch: Partial<ChatSession> = {}): void {
    const touchedAt = new Date().toISOString()
    sessions.value = sortSessions(
      sessions.value.map((item) =>
        item.id === sessionId
          ? {
            ...item,
            ...patch,
            updated_at: patch.updated_at ?? touchedAt,
            last_message_at: patch.last_message_at ?? touchedAt,
          }
          : item,
      ),
    )
  }

  return {
    sessions,
    currentSession,
    currentSessionId,
    loading,
    errorMessage,
    loadSessions,
    createSession,
    deleteSession,
    renameSession,
    selectSession,
    touchSession,
  }
}
