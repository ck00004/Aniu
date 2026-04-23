<template>
  <section class="chat-conversation">
    <header class="chat-conversation-head">
      <div class="chat-conversation-title-wrap">
        <h3 class="chat-conversation-title">{{ title }}</h3>

        <span v-if="session" class="chat-conversation-meta">
          {{ session.message_count || messages.length }} 条消息
        </span>
      </div>
    </header>

    <div v-if="errorMessage" class="error-banner">{{ errorMessage }}</div>

    <div ref="scrollRef" class="chat-message-list" @scroll="handleScroll">
      <div
        v-if="session && messages.length > 0 && (hasMoreMessages || loadingOlderMessages)"
        class="chat-load-older"
      >
        <button
          type="button"
          class="button ghost small"
          :disabled="loadingOlderMessages"
          @click="handleLoadOlder"
        >
          {{ loadingOlderMessages ? '加载中...' : '加载更早消息' }}
        </button>
      </div>
      <div v-if="loading" class="empty-state chat-empty-state">加载会话中...</div>
      <div v-else-if="!session" class="empty-state chat-empty-state">
        直接输入并发送消息，即可自动开始一个新对话。
      </div>
      <div v-else-if="messages.length === 0" class="empty-state chat-empty-state">
        {{ readOnly ? '当前持久会话暂无消息。' : '开始聊天吧。' }}
      </div>
      <template v-else>
        <ChatMessageItem
          v-for="(message, index) in messages"
          :key="`${message.role}-${message.id ?? index}`"
          :message="message"
          :streaming="isStreamingAssistant(message, index)"
        />
      </template>
      <div ref="bottomRef" class="chat-scroll-anchor" aria-hidden="true"></div>
    </div>

    <ChatComposer
      v-if="!readOnly"
      v-model="inputValue"
      :session-id="session?.id ?? null"
      :pending-attachments="pendingAttachments"
      :sending="sending"
      :can-send="canSend"
      :ensure-session-ready="ensureSessionReady"
      @submit="$emit('submit')"
      @attach="(attachment) => $emit('attach', attachment)"
      @remove-attachment="(id) => $emit('remove-attachment', id)"
      @upload-error="(msg) => $emit('upload-error', msg)"
    />
  </section>
</template>

<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'

import ChatComposer from './ChatComposer.vue'
import ChatMessageItem from './ChatMessageItem.vue'
import type { ChatAttachment, ChatMessage, ChatSession, PersistentSession } from '@/types'
import { isChatScrollNearBottom, shouldAutoFollowChatScroll } from '@/utils/chatScrollFollow'

const props = defineProps<{
  session: ChatSession | PersistentSession | null
  messages: ChatMessage[]
  modelValue: string
  pendingAttachments: ChatAttachment[]
  sending: boolean
  loading: boolean
  loadingOlderMessages: boolean
  hasMoreMessages: boolean
  canSend: boolean
  errorMessage: string
  readOnly?: boolean
  ensureSessionReady: () => Promise<number | null>
  loadOlderMessages: () => Promise<void>
}>()

const emit = defineEmits<{
  (e: 'update:modelValue', value: string): void
  (e: 'submit'): void
  (e: 'attach', attachment: ChatAttachment): void
  (e: 'remove-attachment', id: number): void
  (e: 'upload-error', message: string): void
}>()

const scrollRef = ref<HTMLElement | null>(null)
const bottomRef = ref<HTMLElement | null>(null)
const title = computed(() => props.session?.title || '\u672a\u9009\u62e9\u4f1a\u8bdd')

const inputValue = computed({
  get: () => props.modelValue,
  set: (value: string) => emit('update:modelValue', value),
})

let scrollFrameId: number | null = null
const shouldFollowScroll = ref(true)

function isStreamingAssistant(message: ChatMessage, index: number): boolean {
  return props.sending && message.role === 'assistant' && index === props.messages.length - 1
}

function shouldAutoFollow(): boolean {
  return shouldAutoFollowChatScroll({
    sending: props.sending,
    messageCount: props.messages.length,
    followScroll: shouldFollowScroll.value,
  })
}

function syncScrollToBottom() {
  const messageList = scrollRef.value
  if (messageList) {
    messageList.scrollTop = messageList.scrollHeight
    return
  }

  bottomRef.value?.scrollIntoView({
    block: 'end',
    inline: 'nearest',
    behavior: 'auto',
  })
}

function scrollToBottom() {
  if (scrollFrameId !== null) {
    window.cancelAnimationFrame(scrollFrameId)
  }

  scrollFrameId = window.requestAnimationFrame(() => {
    scrollFrameId = null
    syncScrollToBottom()
  })
}

async function handleLoadOlder() {
  if (props.loadingOlderMessages || !props.hasMoreMessages) {
    return
  }

  const element = scrollRef.value
  const previousScrollHeight = element?.scrollHeight ?? 0
  const previousScrollTop = element?.scrollTop ?? 0
  shouldFollowScroll.value = false

  await props.loadOlderMessages()
  await nextTick()

  if (!element) {
    return
  }

  const heightDelta = element.scrollHeight - previousScrollHeight
  element.scrollTop = previousScrollTop + heightDelta
  handleScroll()
}

function handleScroll() {
  const element = scrollRef.value
  shouldFollowScroll.value = !element || isChatScrollNearBottom({
    scrollHeight: element.scrollHeight,
    scrollTop: element.scrollTop,
    clientHeight: element.clientHeight,
  })
}

watch(
  () => ({
    messageCount: props.messages.length,
    lastContent: props.messages[props.messages.length - 1]?.content ?? '',
    sending: props.sending,
  }),
  (current, previous) => {
    const startedSending = current.sending && !previous?.sending
    if (startedSending) {
      shouldFollowScroll.value = true
    }

    const hasNewMessage = current.messageCount !== (previous?.messageCount ?? 0)
    const hasStreamUpdate = current.sending && current.lastContent !== (previous?.lastContent ?? '')
    if (!hasNewMessage && !hasStreamUpdate && !startedSending) {
      return
    }

    if (!shouldAutoFollow()) return
    scrollToBottom()
  },
  { flush: 'post' },
)

onMounted(() => {
  if (props.sending && shouldAutoFollow()) {
    shouldFollowScroll.value = true
    scrollToBottom()
  }
})

onBeforeUnmount(() => {
  if (scrollFrameId !== null) {
    window.cancelAnimationFrame(scrollFrameId)
    scrollFrameId = null
  }
})
</script>
