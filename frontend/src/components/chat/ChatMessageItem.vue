<template>
  <article class="chat-message" :class="`role-${message.role}`">
    <div class="chat-message-role">{{ roleLabel }}</div>
    <div class="chat-message-body">
      <div v-if="toolCalls.length" class="chat-tool-call-card">
        <button
          type="button"
          class="chat-tool-call-toggle"
          :aria-expanded="toolCallsExpanded"
          @click="toolCallsExpanded = !toolCallsExpanded"
        >
          <span>工具调用 {{ toolCalls.length }} 项</span>
          <span class="chat-tool-call-toggle-icon">{{ toolCallsExpanded ? '收起' : '展开' }}</span>
        </button>

        <ul v-show="toolCallsExpanded" class="chat-tool-calls">
          <li
            v-for="(tool, ti) in toolCalls"
            :key="`${tool.tool_name}-${ti}`"
            :class="toolStatusClass(tool)"
          >
            <span class="chat-tool-name">{{ tool.tool_name }}</span>
            <span class="chat-tool-status">{{ toolStatusText(tool) }}</span>
            <span v-if="tool.summary" class="chat-tool-summary">{{ tool.summary }}</span>
          </li>
        </ul>
      </div>

      <div v-if="attachmentList.length" class="chat-message-attachments">
        <ChatAttachmentChip
          v-for="attachment in attachmentList"
          :key="attachment.id"
          :attachment="attachment"
        />
      </div>

      <MarkdownMessage v-if="displayContent" :content="displayContent" />
      <div v-else-if="streaming" class="chat-message-text is-placeholder">正在生成…</div>
    </div>
  </article>
</template>

<script setup lang="ts">
import { computed, defineAsyncComponent, defineComponent, h, ref, watch } from 'vue'

import ChatAttachmentChip from './ChatAttachmentChip.vue'
import type { ChatMessage, ChatToolCall } from '@/types'

const MarkdownMessage = defineAsyncComponent({
  loader: () => import('./MarkdownMessage.vue'),
  delay: 0,
  loadingComponent: defineComponent({
    name: 'MarkdownMessageFallback',
    props: {
      content: {
        type: String,
        required: true,
      },
    },
    setup(fallbackProps) {
      return () => h('div', { class: 'chat-message-text' }, fallbackProps.content)
    },
  }),
})

const props = defineProps<{
  message: ChatMessage
  streaming?: boolean
}>()

const attachmentList = computed(() => props.message.attachments ?? [])
const toolCalls = computed(() => props.message.tool_calls ?? [])
const roleLabel = computed(() => (props.message.role === 'user' ? '我' : 'Aniu'))

const displayContent = computed(() => {
  if (props.message.content) return props.message.content
  if (props.streaming) return '正在生成…'
  return ''
})

const toolCallsExpanded = ref(Boolean(props.streaming))

watch(
  toolCalls,
  (value) => {
    if (value.length && props.streaming) {
      toolCallsExpanded.value = true
    }
  },
  { deep: true },
)

function toolStatusClass(tool: ChatToolCall): string {
  if (tool.status === 'running') return 'is-running'
  return tool.ok === false ? 'is-failed' : 'is-done'
}

function toolStatusText(tool: ChatToolCall): string {
  if (tool.status === 'running') return '调用中'
  return tool.ok === false ? '失败' : '完成'
}
</script>
