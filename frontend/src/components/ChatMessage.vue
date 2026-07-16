<template>
  <div class="chat-message" :class="[message.role]">
    <div class="message-avatar">
      <el-avatar :size="32" v-if="message.role === 'assistant'"><el-icon><MagicStick /></el-icon></el-avatar>
      <el-avatar :size="32" v-else icon="UserFilled" />
    </div>
    <div class="message-content">
      <div class="message-bubble">
        <div v-if="message.role === 'assistant'" class="markdown-body" v-html="renderedContent" />
        <div v-else class="user-text">{{ message.content }}</div>
      </div>
      <div v-if="message.citations && message.citations.length > 0" class="citations">
        <span class="citations-label">来源引用：</span>
        <span v-for="(cit, i) in message.citations" :key="i" class="citation-link" @click="$emit('citation-click', cit)">[{{ i+1 }}] {{ cit.source_name }}</span>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed } from "vue"; import { MagicStick } from "@element-plus/icons-vue"
import type { ChatMessage, CitationItem } from "@/api/chat"; import { marked } from "@/utils/marked"
const props = defineProps<{ message: ChatMessage }>(); defineEmits<{ "citation-click": [citation: CitationItem] }>()
const renderedContent = computed(() => props.message.role === "assistant" ? marked(props.message.content) : props.message.content)
</script>

<style scoped lang="scss">
.chat-message { display: flex; gap: 12px; padding: 16px 0; max-width: 800px; margin: 0 auto;
  &.user { flex-direction: row-reverse; .message-bubble { background: var(--color-bg-tab); border-radius: 12px 12px 2px; } }
  &.assistant { .message-bubble { background: var(--color-bg-1); border: 1px solid var(--color-divider-1); border-radius: 2px 12px 12px; } }
}
.message-avatar { flex-shrink: 0; }
.message-content { max-width: 75%; display: flex; flex-direction: column; gap: 6px; }
.message-bubble { padding: 12px 16px; font-size: 14px; line-height: 1.7; }
.user-text { white-space: pre-wrap; word-break: break-word; }
.markdown-body {
  :deep(p) { margin-bottom: 8px; &:last-child { margin-bottom: 0; } }
  :deep(code) { background: var(--color-bg-tab); padding: 2px 6px; border-radius: 4px; font-size: 13px; }
  :deep(pre) { background: var(--color-bg-tab); padding: 12px; border-radius: 8px; overflow-x: auto; margin: 8px 0; }
  :deep(ul), :deep(ol) { padding-left: 20px; margin-bottom: 8px; }
}
.citations { display: flex; flex-wrap: wrap; gap: 4px; align-items: center; }
.citations-label { font-size: 12px; color: var(--color-text-3); }
.citation-link { font-size: 12px; color: var(--color-text-focus); cursor: pointer; padding: 2px 6px; border-radius: 4px; background: rgba(26,117,255,.08); &:hover { background: rgba(26,117,255,.15); } }
</style>
