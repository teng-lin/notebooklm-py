<template>
  <div class="chat-input-area">
    <div class="input-wrapper">
      <el-input ref="inputRef" v-model="content" type="textarea" :rows="3" :disabled="disabled" placeholder="输入你的问题... (Enter 发送，Shift+Enter 换行)" class="chat-textarea" @keydown="handleKeydown" />
      <el-button type="primary" class="send-btn" :disabled="!content.trim() || disabled" :loading="disabled" @click="handleSend"><el-icon><Promotion /></el-icon></el-button>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref } from "vue"; import { Promotion } from "@element-plus/icons-vue"
defineProps<{ disabled?: boolean }>(); const emit = defineEmits<{ send: [content: string] }>()
const content = ref("")
function handleKeydown(e: KeyboardEvent) { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); handleSend() } }
function handleSend() { const text = content.value.trim(); if (!text) return; emit("send", text); content.value = "" }
</script>

<style scoped lang="scss">
.chat-input-area { padding: 16px 24px; background: var(--color-bg-1); border-top: 1px solid var(--color-divider-1); }
.input-wrapper { max-width: 800px; margin: 0 auto; position: relative; }
.chat-textarea { :deep(.el-textarea__inner) { border-radius: var(--radius-input); padding-right: 50px; resize: none; font-size: 14px; line-height: 1.6; } }
.send-btn { position: absolute; right: 8px; bottom: 8px; border-radius: 50%; width: 36px; height: 36px; padding: 0; display: flex; align-items: center; justify-content: center; background: var(--color-main-1); border-color: var(--color-main-1); }
</style>
