import { ref } from 'vue'

import { api } from '../services/api.ts'
import type { ChatMessage, PersistentSession } from '../types.ts'

const MESSAGE_PAGE_SIZE = 50

function prependUniqueMessages(current: ChatMessage[], older: ChatMessage[]): ChatMessage[] {
  if (!older.length) return current
  const existingIds = new Set(
    current
      .map((item) => item.id)
      .filter((value): value is number => typeof value === 'number'),
  )
  const dedupedOlder = older.filter(
    (item) => typeof item.id !== 'number' || !existingIds.has(item.id),
  )
  return [...dedupedOlder, ...current]
}

export function usePersistentSession() {
  const session = ref<PersistentSession | null>(null)
  const messages = ref<ChatMessage[]>([])
  const loading = ref(false)
  const loadingOlderMessages = ref(false)
  const errorMessage = ref('')
  const hasMoreMessages = ref(false)
  const nextBeforeId = ref<number | null>(null)

  async function loadSession(): Promise<void> {
    loading.value = true
    errorMessage.value = ''
    try {
      const payload = await api.getPersistentSessionMessages({ limit: MESSAGE_PAGE_SIZE })
      session.value = payload.session
      messages.value = payload.messages
      hasMoreMessages.value = payload.has_more
      nextBeforeId.value = payload.next_before_id
    } catch (error) {
      errorMessage.value = (error as Error).message
    } finally {
      loading.value = false
    }
  }

  async function refreshSummaryOnly(): Promise<void> {
    try {
      session.value = await api.getPersistentSession()
    } catch (error) {
      errorMessage.value = (error as Error).message
    }
  }

  async function loadOlderMessages(): Promise<void> {
    if (loading.value || loadingOlderMessages.value || !hasMoreMessages.value || nextBeforeId.value === null) {
      return
    }

    loadingOlderMessages.value = true
    try {
      const payload = await api.getPersistentSessionMessages({
        limit: MESSAGE_PAGE_SIZE,
        beforeId: nextBeforeId.value,
      })
      session.value = payload.session
      messages.value = prependUniqueMessages(messages.value, payload.messages)
      hasMoreMessages.value = payload.has_more
      nextBeforeId.value = payload.next_before_id
    } catch (error) {
      errorMessage.value = (error as Error).message
    } finally {
      loadingOlderMessages.value = false
    }
  }

  function clear() {
    session.value = null
    messages.value = []
    errorMessage.value = ''
    hasMoreMessages.value = false
    nextBeforeId.value = null
  }

  return {
    session,
    messages,
    loading,
    loadingOlderMessages,
    errorMessage,
    hasMoreMessages,
    loadSession,
    refreshSummaryOnly,
    loadOlderMessages,
    clear,
  }
}
