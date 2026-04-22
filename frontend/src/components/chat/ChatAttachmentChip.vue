<template>
  <div class="chat-attachment-chip" :class="{ 'is-image': isImage }">
    <button
      type="button"
      class="chat-attachment-open"
      :class="{ 'is-loading': loadingPreview || opening }"
      :title="attachment.filename"
      @click="openAttachment"
    >
      <img
        v-if="isImage && previewUrl"
        :src="previewUrl"
        :alt="attachment.filename"
        class="chat-attachment-thumb"
      />
      <span v-else-if="isImage" class="chat-attachment-thumb chat-attachment-thumb-placeholder">
        图
      </span>
      <span v-else class="chat-attachment-icon">📄</span>

      <div class="chat-attachment-meta">
        <span class="chat-attachment-name" :title="attachment.filename">{{ attachment.filename }}</span>
        <span class="chat-attachment-size">
          {{ loadingPreview ? '加载中…' : formatSize(attachment.size) }}
        </span>
      </div>
    </button>

    <button
      v-if="removable"
      type="button"
      class="chat-attachment-remove"
      title="移除"
      @click.stop="$emit('remove', attachment.id)"
    >
      ×
    </button>
  </div>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'

import { api } from '@/services/api'
import type { ChatAttachment } from '@/types'

const props = defineProps<{
  attachment: ChatAttachment
  removable?: boolean
}>()

defineEmits<{ (e: 'remove', id: number): void }>()

const isImage = computed(() => props.attachment.mime_type.startsWith('image/'))

const previewUrl = ref('')
const loadingPreview = ref(false)
const opening = ref(false)

function revokePreviewUrl() {
  if (!previewUrl.value) return
  URL.revokeObjectURL(previewUrl.value)
  previewUrl.value = ''
}

async function ensurePreviewUrl(): Promise<string> {
  if (!isImage.value) return ''
  if (previewUrl.value) return previewUrl.value

  loadingPreview.value = true
  try {
    const blob = await api.fetchChatAttachmentBlob(props.attachment.id)
    previewUrl.value = URL.createObjectURL(blob)
    return previewUrl.value
  } finally {
    loadingPreview.value = false
  }
}

async function openAttachment() {
  if (opening.value) return
  opening.value = true
  try {
    let objectUrl = ''
    let shouldRevoke = false

    if (isImage.value) {
      objectUrl = await ensurePreviewUrl()
    } else {
      const blob = await api.fetchChatAttachmentBlob(props.attachment.id)
      objectUrl = URL.createObjectURL(blob)
      shouldRevoke = true
    }

    if (isImage.value) {
      window.open(objectUrl, '_blank', 'noopener,noreferrer')
    } else {
      const link = document.createElement('a')
      link.href = objectUrl
      link.download = props.attachment.filename
      link.rel = 'noopener'
      document.body.append(link)
      link.click()
      link.remove()
    }

    if (shouldRevoke) {
      window.setTimeout(() => URL.revokeObjectURL(objectUrl), 1000)
    }
  } catch (error) {
    window.alert((error as Error).message)
  } finally {
    opening.value = false
  }
}

watch(
  () => props.attachment.id,
  () => {
    revokePreviewUrl()
  },
)

onBeforeUnmount(() => {
  revokePreviewUrl()
})

function formatSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  return `${(bytes / (1024 * 1024)).toFixed(2)} MB`
}
</script>
