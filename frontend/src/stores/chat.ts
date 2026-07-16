import { defineStore } from "pinia"; import { ref } from "vue"
import type { ChatSession, ChatMessage } from "@/api/chat"
import { fetchSessionsApi, createSessionApi, deleteSessionApi, fetchMessagesApi, sendMessageStreamApi } from "@/api/chat"

export const useChatStore = defineStore("chat", () => {
  const sessions = ref<ChatSession[]>([]); const currentSessionId = ref<number | null>(null)
  const messages = ref<ChatMessage[]>([]); const streaming = ref(false); const streamContent = ref("")

  async function fetchSessions(nbId: string) { sessions.value = await fetchSessionsApi(nbId) }
  async function createSession(nbId: string) { const s = await createSessionApi(nbId); sessions.value.unshift(s); currentSessionId.value = s.id; messages.value = []; return s }
  async function deleteSession(nbId: string, sid: number) { await deleteSessionApi(nbId, sid); sessions.value = sessions.value.filter((s) => s.id !== sid); if (currentSessionId.value === sid) { currentSessionId.value = null; messages.value = [] } }
  async function loadMessages(nbId: string, sid: number) { currentSessionId.value = sid; messages.value = await fetchMessagesApi(nbId, sid) }

  function sendMessage(nbId: string, sid: number, content: string, callbacks?: { onMessage?: (t: string) => void; onDone?: () => void; onError?: (err: any) => void }, sourceIds?: string[]) {
    streaming.value = true; streamContent.value = ""
    messages.value.push({ id: -Date.now(), role: "user", content, citations: null, created_at: new Date().toISOString() })
    return sendMessageStreamApi(nbId, sid, content,
      (text) => { streamContent.value = text; callbacks?.onMessage?.(text) },
      (citations) => { const last = messages.value[messages.value.length-1]; if (last?.role === "assistant") last.citations = citations },
      () => { streaming.value = false; messages.value.push({ id: -Date.now(), role: "assistant", content: streamContent.value, citations: null, created_at: new Date().toISOString() }); streamContent.value = ""; callbacks?.onDone?.() },
      (err) => { streaming.value = false; callbacks?.onError?.(err) },
      sourceIds,
    )
  }

  return { sessions, currentSessionId, messages, streaming, streamContent, fetchSessions, createSession, deleteSession, loadMessages, sendMessage }
})
