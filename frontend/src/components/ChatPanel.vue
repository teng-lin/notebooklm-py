<template>
  <div class="chat-panel">
    <div class="panel-header"><span class="panel-title">AI 问答</span></div>
    <div class="chat-body">
      <div v-if="!currentSessionId && !aiSummary" class="chat-welcome">
        <el-icon :size="48" color="#d0d0d0"><ChatLineSquare /></el-icon>
        <p>正在生成 AI 总结...</p>
      </div>
      <div v-if="aiSummary || summaryLoading || summaryError" class="ai-summary">
        <div class="summary-label">AI 总结</div>
        <div v-if="summaryLoading" class="summary-skeleton"><el-skeleton :rows="3" animated /></div>
        <p v-else-if="summaryError" class="summary-error">{{ summaryError }}</p>
        <p v-else class="summary-text">{{ aiSummary }}</p>
      </div>
      <div v-if="recommendedQuestions.length > 0 && !currentSessionId" class="suggested-questions">
        <div class="suggested-label">推荐问题</div>
        <div v-for="(q, i) in recommendedQuestions" :key="i" class="suggested-item" @click="handleSuggestedQuestion(q)">
          {{ q }}
        </div>
      </div>
      <template v-if="currentSessionId">
        <div ref="messagesRef" class="messages-area">
          <ChatMessage v-for="msg in messages" :key="msg.id" :message="msg" />
          <div v-if="chatStore.streaming" class="streaming-bubble">{{ chatStore.streamContent }}<span class="cursor-blink">|</span></div>
        </div>
      </template>
    </div>
    <div class="chat-input-area">
      <el-input v-model="inputText" type="textarea" :rows="2" resize="none" placeholder="输入问题..." @keydown.enter.exact.prevent="handleSend" :disabled="chatStore.streaming" />
      <el-button type="primary" :loading="chatStore.streaming" @click="handleSend" :disabled="!inputText.trim()">发送</el-button>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted, watch, nextTick } from "vue"
import { ChatLineSquare } from "@element-plus/icons-vue"
import { ElMessage } from "element-plus"
import { useChatStore } from "@/stores/chat"
import { createSessionApi, sendMessageApi } from "@/api/chat"
import ChatMessage from "@/components/ChatMessage.vue"
import request from "@/api/request"

const props = defineProps<{
  notebookId: string
  selectedSourceIds: Set<string>
}>()

const chatStore = useChatStore()
const inputText = ref("")
const messagesRef = ref<HTMLElement>()
const aiSummary = ref("")
const summaryLoading = ref(false)
const summaryError = ref("")
const recommendedQuestions = ref<string[]>([])
const currentSessionId = computed(() => chatStore.currentSessionId)
const messages = computed(() => chatStore.messages)

const FALLBACK_QUESTIONS = ["这些资料的主要结论是什么？", "有哪些关键数据或事实？", "请列出主要要点"]

onMounted(async () => {
  await chatStore.fetchSessions(props.notebookId)
  await generateSummary()
  await loadRecommendedQuestions()
})

async function generateSummary() {
  summaryLoading.value = true
  summaryError.value = ""
  try {
    const session = await createSessionApi(props.notebookId, "AI 总结")
    chatStore.currentSessionId = session.id
    const sourceIds = Array.from(props.selectedSourceIds)
    const result = await sendMessageApi(props.notebookId, session.id, "请用中文总结这些资料的核心要点", sourceIds)
    aiSummary.value = result.content
    chatStore.messages = []
    chatStore.currentSessionId = null
  } catch (e: any) {
    summaryError.value = e?.response?.data?.detail || e?.detail || "总结生成失败"
  } finally {
    summaryLoading.value = false
  }
}

async function loadRecommendedQuestions() {
  try {
    const res = await request.get(`/api/notebooks/${props.notebookId}/suggested-prompts`, { params: { surface: "ask" } })
    recommendedQuestions.value = (res.data.suggestions || []).slice(0, 3).map((s: any) => s.prompt)
  } catch {
    recommendedQuestions.value = FALLBACK_QUESTIONS
  }
}

async function handleSend() {
  const text = inputText.value.trim()
  if (!text || chatStore.streaming) return
  inputText.value = ""
  if (!currentSessionId.value) {
    try {
      const session = await createSessionApi(props.notebookId, text.slice(0, 50))
      chatStore.sessions.unshift(session)
      chatStore.currentSessionId = session.id
    } catch { ElMessage.error("创建对话失败"); return }
  }
  const sourceIds = Array.from(props.selectedSourceIds)
  chatStore.sendMessage(props.notebookId, currentSessionId.value!, text, {
    onError: (err: any) => ElMessage.error(err?.detail || "发送失败"),
  }, sourceIds)
  nextTick(() => { if (messagesRef.value) messagesRef.value.scrollTop = messagesRef.value.scrollHeight })
}

function handleSuggestedQuestion(q: string) {
  inputText.value = q
  handleSend()
}

watch(() => messages.value.length, () => {
  nextTick(() => { if (messagesRef.value) messagesRef.value.scrollTop = messagesRef.value.scrollHeight })
})
</script>

<style scoped>
.chat-panel { display: flex; flex-direction: column; height: 100%; }
.panel-header { padding: 12px 16px; border-bottom: 1px solid var(--baoku-border, #e8e8e8); font-size: 14px; font-weight: 600; }
.chat-body { flex: 1; overflow-y: auto; padding: 16px; }
.chat-welcome { display: flex; flex-direction: column; align-items: center; justify-content: center; height: 100%; gap: 12px; color: var(--baoku-text-3, #999); }
.ai-summary { padding: 16px; border-radius: 8px; background: var(--baoku-surface, #fff); border: 1px solid var(--baoku-border, #e8e8e8); margin-bottom: 16px; }
.summary-label { font-size: 13px; font-weight: 600; margin-bottom: 8px; color: var(--baoku-primary, #ff3650); }
.summary-text { font-size: 14px; line-height: 1.7; margin: 0; }
.summary-error { font-size: 13px; color: var(--el-color-danger, #f56c6c); margin: 0; }
.suggested-questions { margin-bottom: 16px; }
.suggested-label { font-size: 13px; font-weight: 600; margin-bottom: 8px; }
.suggested-item { padding: 10px 12px; border-radius: 8px; background: var(--baoku-surface, #fff); border: 1px solid var(--baoku-border, #e8e8e8); margin-bottom: 6px; font-size: 13px; cursor: pointer; transition: all 0.2s; }
.suggested-item:hover { border-color: var(--baoku-primary, #ff3650); color: var(--baoku-primary, #ff3650); }
.messages-area { display: flex; flex-direction: column; gap: 12px; }
.streaming-bubble { padding: 12px 16px; background: var(--baoku-surface, #fff); border-radius: 8px; font-size: 14px; line-height: 1.7; }
.cursor-blink { animation: blink 1s step-end infinite; color: var(--baoku-primary, #ff3650); }
@keyframes blink { 50% { opacity: 0; } }
.chat-input-area { display: flex; gap: 8px; padding: 12px 16px; border-top: 1px solid var(--baoku-border, #e8e8e8); }
</style>
