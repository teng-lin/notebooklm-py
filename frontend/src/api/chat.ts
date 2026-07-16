import request from "./request"

export interface ChatSession { id: number; user_id: number; notebook_id: string; title: string; message_count: number; created_at: string; updated_at: string }
export interface CitationItem { source_id: number; source_name: string; text: string; page: number | null; rect: number[] | null }
export interface ChatMessage { id: number; session_id: number; role: "user" | "assistant"; content: string; citations: CitationItem[] | null; created_at: string }

export function fetchSessionsApi(notebookId: string): Promise<ChatSession[]> { return request.get(`/api/notebooks/${notebookId}/chat/sessions`).then((r) => r.data) }
export function createSessionApi(notebookId: string, title?: string): Promise<ChatSession> { return request.post(`/api/notebooks/${notebookId}/chat/sessions`, { title }).then((r) => r.data) }
export function deleteSessionApi(notebookId: string, sessionId: number): Promise<void> { return request.delete(`/api/notebooks/${notebookId}/chat/sessions/${sessionId}`).then((r) => r.data) }
export function fetchMessagesApi(notebookId: string, sessionId: number): Promise<ChatMessage[]> { return request.get(`/api/notebooks/${notebookId}/chat/sessions/${sessionId}/messages`).then((r) => r.data) }
export function sendMessageApi(notebookId: string, sessionId: number, content: string): Promise<ChatMessage> { return request.post(`/api/notebooks/${notebookId}/chat/sessions/${sessionId}/messages`, { content }).then((r) => r.data) }

export function sendMessageStreamApi(
  notebookId: string, sessionId: number, content: string,
  onMessage: (text: string) => void, onCitations?: (citations: CitationItem[]) => void,
  onDone?: () => void, onError?: (err: any) => void,
): { abort: () => void } {
  const controller = new AbortController(); const token = localStorage.getItem("token")
  fetch(`/api/notebooks/${notebookId}/chat/sessions/${sessionId}/messages/stream`, {
    method: "POST",
    headers: { "Content-Type": "application/json", Authorization: token ? `Bearer ${token}` : "" },
    body: JSON.stringify({ content }), signal: controller.signal,
  }).then(async (res) => {
    if (!res.ok) { const err = await res.json().catch(() => ({ detail: "请求失败" })); onError?.(err); return }
    const reader = res.body?.getReader(); if (!reader) return; const decoder = new TextDecoder(); let buffer = ""
    while (true) {
      const { done, value } = await reader.read(); if (done) break
      buffer += decoder.decode(value, { stream: true })
      const lines = buffer.split("\n"); buffer = lines.pop() || ""
      for (const line of lines) {
        if (!line.startsWith("data: ")) continue
        const data = line.slice(6)
        if (data === "[DONE]") { onDone?.(); return }
        try { const p = JSON.parse(data); if (p.text) onMessage(p.text); if (p.citations) onCitations?.(p.citations); if (p.done) onDone?.() } catch {}
      }
    }
    onDone?.()
  }).catch((err) => { if (err.name !== "AbortError") onError?.(err) })
  return { abort: () => controller.abort() }
}
