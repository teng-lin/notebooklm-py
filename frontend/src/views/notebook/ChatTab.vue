<template>
  <div class="chat-tab">
    <div class="chat-sidebar">
      <div class="sidebar-header"><h3 class="sidebar-title">对话历史</h3><el-button type="primary" size="small" class="new-chat-btn" @click="handleNewSession">+ 新建对话</el-button></div>
      <div class="session-list">
        <div v-for="session in sessions" :key="session.id" class="session-item" :class="{ active: session.id === currentSessionId }" @click="switchSession(session.id)">
          <div class="session-info"><span class="session-title">{{ session.title || "新对话" }}</span><span class="session-meta">{{ session.message_count }} 条消息</span></div>
          <el-button text size="small" type="danger" class="session-del" @click.stop="handleDeleteSession(session.id)"><el-icon><Delete /></el-icon></el-button>
        </div>
        <div v-if="sessions.length === 0" class="session-empty">暂无对话记录</div>
      </div>
    </div>
    <div class="chat-main">
      <div v-if="!currentSessionId" class="chat-welcome">
        <el-icon :size="64" color="#d0d0d0"><ChatLineSquare /></el-icon><h2>开始新对话</h2><p>基于知识库内容，向 AI 提问</p>
        <el-button type="primary" @click="handleNewSession">开始对话</el-button>
      </div>
      <template v-else>
        <div ref="messagesRef" class="messages-area">
          <ChatMessage v-for="msg in messages" :key="msg.id" :message="msg" @citation-click="handleCitationClick" />
          <div v-if="chatStore.streaming" class="chat-message assistant">
            <div class="message-avatar"><el-avatar :size="32"><el-icon><MagicStick /></el-icon></el-avatar></div>
            <div class="message-content"><div class="message-bubble streaming">{{ chatStore.streamContent }}<span class="cursor-blink">|</span></div></div>
          </div>
        </div>
        <ChatInput :disabled="chatStore.streaming" @send="handleSend" />
      </template>
    </div>
    <CitationPopup :visible="showCitation" :citation="selectedCitation" @update:visible="showCitation = $event" />
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted, nextTick, watch } from "vue"; import { useRoute } from "vue-router"
import { Delete, ChatLineSquare, MagicStick } from "@element-plus/icons-vue"; import { ElMessage, ElMessageBox } from "element-plus"
import { useChatStore } from "@/stores/chat"; import type { CitationItem } from "@/api/chat"
import ChatMessage from "@/components/ChatMessage.vue"; import ChatInput from "@/components/ChatInput.vue"; import CitationPopup from "@/components/CitationPopup.vue"

const route = useRoute(); const chatStore = useChatStore()
const notebookId = computed(() => Number(route.params.id)); const currentSessionId = computed(() => chatStore.currentSessionId)
const sessions = computed(() => chatStore.sessions); const messages = computed(() => chatStore.messages)
const messagesRef = ref<HTMLElement>(); const showCitation = ref(false); const selectedCitation = ref<CitationItem | null>(null)

function scrollToBottom() { nextTick(() => { if (messagesRef.value) messagesRef.value.scrollTop = messagesRef.value.scrollHeight }) }
watch([() => messages.value.length, () => chatStore.streamContent], () => scrollToBottom(), { flush: "post" })
async function handleNewSession() { try { await chatStore.createSession(notebookId.value) } catch { ElMessage.error("创建对话失败") } }
async function switchSession(sid: number) { try { await chatStore.loadMessages(notebookId.value, sid); scrollToBottom() } catch { ElMessage.error("加载消息失败") } }
async function handleDeleteSession(sid: number) { try { await ElMessageBox.confirm("确定删除此对话？", "确认"); await chatStore.deleteSession(notebookId.value, sid) } catch {} }
function handleSend(c: string) { if (!currentSessionId.value) return; chatStore.sendMessage(notebookId.value, currentSessionId.value, c, { onError: (err: any) => ElMessage.error(err?.detail || "发送失败") }); scrollToBottom() }
function handleCitationClick(citation: CitationItem) { selectedCitation.value = citation; showCitation.value = true }
onMounted(async () => {
  await chatStore.fetchSessions(notebookId.value); const sid = route.params.sid
  if (sid && typeof sid === "string") { const id = Number(sid); if (sessions.value.some((s) => s.id === id)) { await chatStore.loadMessages(notebookId.value, id); scrollToBottom() } }
})
</script>

<style scoped lang="scss">
.chat-tab { display: flex; height: calc(100vh - 48px - 64px - 40px - 48px); margin: -24px; }
.chat-sidebar { width: 280px; flex-shrink: 0; background: var(--color-bg-1); border-right: 1px solid var(--color-divider-1); display: flex; flex-direction: column; }
.sidebar-header { padding: 16px; border-bottom: 1px solid var(--color-divider-1); display: flex; align-items: center; justify-content: space-between; }
.sidebar-title { font-size: 14px; font-weight: 600; }
.new-chat-btn { border-radius: var(--radius-button); background: var(--color-main-1); border-color: var(--color-main-1); }
.session-list { flex: 1; overflow-y: auto; padding: 8px; }
.session-item { display: flex; align-items: center; padding: 10px 12px; border-radius: 8px; cursor: pointer; margin-bottom: 4px; &:hover { background: var(--color-bg-tab); } &.active { background: rgba(255,54,80,.08); } .session-del { opacity: 0; flex-shrink: 0; } &:hover .session-del { opacity: 1; } }
.session-info { flex: 1; min-width: 0; }
.session-title { display: block; font-size: 13px; font-weight: 500; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.session-meta { display: block; font-size: 11px; color: var(--color-text-3); margin-top: 2px; }
.session-empty { padding: 24px; text-align: center; color: var(--color-text-3); font-size: 13px; }
.chat-main { flex: 1; display: flex; flex-direction: column; background: var(--color-bg-tab); }
.chat-welcome { flex: 1; display: flex; flex-direction: column; align-items: center; justify-content: center; gap: 12px; h2 { font-size: 20px; font-weight: 600; } p { font-size: 14px; color: var(--color-text-3); margin-bottom: 8px; } }
.messages-area { flex: 1; overflow-y: auto; padding: 16px 24px; }
.message-bubble.streaming { background: var(--color-bg-1); border: 1px solid var(--color-divider-1); border-radius: 2px 12px 12px; padding: 12px 16px; font-size: 14px; line-height: 1.7; max-width: 75%; }
.cursor-blink { animation: blink 1s step-end infinite; color: var(--color-main-1); }
@keyframes blink { 50% { opacity: 0; } }
</style>
