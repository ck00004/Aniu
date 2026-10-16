import { computed, ref, watch } from 'vue'

import { api, getStoredToken } from '../services/api.ts'
import type { ChatAttachment, ChatMessage, ChatToolCall } from '../types.ts'
import { parseSseChunk } from '../utils/sse.ts'

const MESSAGE_PAGE_SIZE = 50

interface StreamEvent {
  type: string
  ts?: number
  [key: string]: unknown
}

function prependUniqueMessages(current: ChatMessage[], older: ChatMessage[]): ChatMessage[] {
  if (!older.length) return current

  const existingIds = new Set(
    current
      .map((item) => item.id)
      .filter((value): value is number => typeof value === 'number'),
  )
  const dedupedOlder = older.filter((item) => (
    typeof item.id !== 'number' || !existingIds.has(item.id)
  ))
  return [...dedupedOlder, ...current]
}

interface StreamChatResult {
  failedMessage: string | null
}

export function useChatSession() {
  const messages = ref<ChatMessage[]>([])
  const input = ref('')
  const pendingAttachments = ref<ChatAttachment[]>([])
  const sending = ref(false)
  const loading = ref(false)
  const loadingOlderMessages = ref(false)
  const errorMessage = ref('')
  const activeSessionId = ref<number | null>(null)
  const hasMoreMessages = ref(false)
  const nextBeforeId = ref<number | null>(null)

  const canSend = computed(
    () =>
      !sending.value
      && (input.value.trim().length > 0 || pendingAttachments.value.length > 0),
  )

  async function loadSession(sessionId: number | null): Promise<void> {
    activeSessionId.value = sessionId
    messages.value = []
    errorMessage.value = ''
    pendingAttachments.value = []
    hasMoreMessages.value = false
    nextBeforeId.value = null
    if (sessionId === null) return
    loading.value = true
    try {
      const payload = await api.getChatSessionMessages(sessionId, {
        limit: MESSAGE_PAGE_SIZE,
      })
      messages.value = payload.messages
      hasMoreMessages.value = payload.has_more
      nextBeforeId.value = payload.next_before_id
    } catch (error) {
      errorMessage.value = (error as Error).message
    } finally {
      loading.value = false
    }
  }

  async function loadOlderMessages(): Promise<void> {
    const sessionId = activeSessionId.value
    if (
      sessionId === null
      || loading.value
      || loadingOlderMessages.value
      || !hasMoreMessages.value
      || nextBeforeId.value === null
    ) {
      return
    }

    loadingOlderMessages.value = true
    try {
      const payload = await api.getChatSessionMessages(sessionId, {
        limit: MESSAGE_PAGE_SIZE,
        beforeId: nextBeforeId.value,
      })
      messages.value = prependUniqueMessages(messages.value, payload.messages)
      hasMoreMessages.value = payload.has_more
      nextBeforeId.value = payload.next_before_id
    } catch (error) {
      errorMessage.value = (error as Error).message
    } finally {
      loadingOlderMessages.value = false
    }
  }

  function clearMessagesLocally(): void {
    messages.value = []
    errorMessage.value = ''
  }

  function applyEventToAssistant(assistant: ChatMessage, event: StreamEvent) {
    const toolCalls: ChatToolCall[] = assistant.tool_calls ?? []
    switch (event.type) {
      case 'llm_request': {
        if (!assistant.content) {
          assistant.content = '正在思考…'
        }
        break
      }
      case 'final_started': {
        assistant.content = ''
        break
      }
      case 'final_delta': {
        const delta = String(event.delta || '')
        if (delta) assistant.content += delta
        break
      }
      case 'final_finished': {
        const content = String(event.content || '')
        if (content) assistant.content = content
        break
      }
      case 'llm_message': {
        const content = String(event.content || '')
        if (content) assistant.content = content
        break
      }
      case 'tool_call': {
        const toolName = String(event.tool_name || '')
        const toolCallId = String(event.tool_call_id || '')
        const status = String(event.status || 'running') as 'running' | 'done'
        const existing = toolCalls.find(
          (item) => item.status === 'running'
            && (
              (toolCallId && item.tool_call_id === toolCallId)
              || (!toolCallId && item.tool_name === toolName)
            ),
        )
        if (status === 'running' && !existing) {
          toolCalls.push({
            tool_call_id: toolCallId || undefined,
            tool_name: toolName,
            status: 'running',
            arguments: event.arguments,
            started_at: Number(event.ts || Date.now() / 1000),
          })
        } else if (status === 'done' && existing) {
          existing.status = 'done'
          existing.ok = event.ok as boolean | undefined
          existing.summary = event.summary as string | undefined
          existing.finished_at = Number(event.ts || Date.now() / 1000)
        } else if (status === 'done') {
          toolCalls.push({
            tool_call_id: toolCallId || undefined,
            tool_name: toolName,
            status: 'done',
            ok: event.ok as boolean | undefined,
            summary: event.summary as string | undefined,
            arguments: event.arguments,
            started_at: Number(event.ts || Date.now() / 1000),
            finished_at: Number(event.ts || Date.now() / 1000),
          })
        }
        assistant.tool_calls = toolCalls
        break
      }
      case 'llm_final':
      case 'completed': {
        const content = String(event.content || event.message || '')
        if (content) assistant.content = content
        break
      }
      case 'failed': {
        const reason = String(event.message || '聊天失败')
        assistant.content = assistant.content
          ? `${assistant.content}\n\n执行失败：${reason}`
          : `执行失败：${reason}`
        break
      }
    }
  }

  async function streamChat(
    sessionId: number,
    content: string,
    attachmentIds: number[],
    assistant: ChatMessage,
  ): Promise<StreamChatResult> {
    const token = getStoredToken()
    const headers: Record<string, string> = {
      'Content-Type': 'application/json',
      Accept: 'text/event-stream',
    }
    if (token) headers.Authorization = `Bearer ${token}`

    const response = await fetch(api.chatSessionStreamUrl(), {
      method: 'POST',
      headers,
      body: JSON.stringify({
        session_id: sessionId,
        content,
        attachment_ids: attachmentIds,
      }),
      cache: 'no-store',
    })
    if (response.status === 401) {
      throw new Error('认证已过期，请重新登录。')
    }
    if (!response.ok || !response.body) {
      throw new Error(`聊天流连接失败 (${response.status})`)
    }

    const reader = response.body.getReader()
    const decoder = new TextDecoder('utf-8')
    let buffer = ''
    let failed: string | null = null

    // 流式事件只写入发起请求时捕获的 assistant 对象，且仅当它仍在
    // 当前消息列表中（用户未切换会话）时才应用；切换会话后由后端
    // 持久化结果兜底，切回时 loadSession 会拉取落库内容。
    const streamTargetActive = () =>
      activeSessionId.value === sessionId && messages.value.includes(assistant)

    try {
      while (true) {
        const { value, done } = await reader.read()
        if (done) break
        buffer += decoder.decode(value, { stream: true })
        let idx = buffer.indexOf('\n\n')
        while (idx >= 0) {
          const chunk = buffer.slice(0, idx)
          buffer = buffer.slice(idx + 2)
          const event = parseSseChunk<StreamEvent>(chunk)
          if (event) {
            if (streamTargetActive()) {
              applyEventToAssistant(assistant, event)
            }
            if (event.type === 'failed') {
              failed = String(event.message || '聊天失败')
            }
          }
          idx = buffer.indexOf('\n\n')
        }
      }
    } catch (error) {
      // 流建立后中断：后端已持久化用户消息与"执行中断"的助手消息，
      // 本地回滚会与服务器状态分叉，保留本地部分内容并仅提示错误。
      return {
        failedMessage: (error as Error).message || '聊天流中断',
      }
    }

    return {
      failedMessage: failed,
    }
  }

  async function sendMessage(): Promise<{ sessionId: number } | null> {
    const sessionId = activeSessionId.value
    if (sessionId === null) {
      errorMessage.value = '请先选择或新建一个对话。'
      return null
    }
    const content = input.value.trim()
    const attachments = [...pendingAttachments.value]
    if (!content && attachments.length === 0) return null
    if (sending.value) return null

    errorMessage.value = ''
    sending.value = true
    const userMessage: ChatMessage = {
      role: 'user',
      content,
      attachments: attachments.length ? attachments : undefined,
    }
    const assistant: ChatMessage = { role: 'assistant', content: '', tool_calls: [] }
    messages.value = [...messages.value, userMessage, assistant]
    input.value = ''
    pendingAttachments.value = []

    try {
      const { failedMessage } = await streamChat(
        sessionId,
        content,
        attachments.map((item) => item.id),
        assistant,
      )
      if (failedMessage) {
        errorMessage.value = failedMessage
      }
      // 流式期间用户切换走又切回：本地 assistant 对象已不在列表中，
      // 重新加载会话以显示后端落库的完整结果。
      if (activeSessionId.value === sessionId && !messages.value.includes(assistant)) {
        await loadSession(sessionId)
      }
      return { sessionId }
    } catch (error) {
      // 连接阶段失败：消息尚未持久化，按引用回滚本地消息是安全的。
      // （不能按位置 slice，否则会误删切换后新会话的消息。）
      errorMessage.value = (error as Error).message
      messages.value = messages.value.filter(
        (item) => item !== userMessage && item !== assistant,
      )
      input.value = content
      pendingAttachments.value = attachments
      return null
    } finally {
      sending.value = false
    }
  }

  function addAttachment(attachment: ChatAttachment): void {
    if (pendingAttachments.value.some((item) => item.id === attachment.id)) return
    pendingAttachments.value = [...pendingAttachments.value, attachment]
  }

  function removeAttachment(attachmentId: number): void {
    pendingAttachments.value = pendingAttachments.value.filter(
      (item) => item.id !== attachmentId,
    )
  }

  watch(activeSessionId, (sessionId, previousSessionId) => {
    errorMessage.value = ''
    if (sessionId === previousSessionId) {
      return
    }
    messages.value = []
    loadingOlderMessages.value = false
    hasMoreMessages.value = false
    nextBeforeId.value = null
    pendingAttachments.value = []
  }, { flush: 'sync' })

  return {
    messages,
    input,
    pendingAttachments,
    sending,
    loading,
    loadingOlderMessages,
    errorMessage,
    canSend,
    activeSessionId,
    hasMoreMessages,
    loadSession,
    loadOlderMessages,
    sendMessage,
    clearMessagesLocally,
    addAttachment,
    removeAttachment,
  }
}
