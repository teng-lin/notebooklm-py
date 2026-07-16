import request from "./request"

export interface ChatSession { id: number; title: string | null; message_count: number; created_at: string; updated_at: string }
export interface CitationItem { source_id: string; source_name: string }
export interface ChatMessage { id: number; role: "user" | "assistant"; content: string; citations: CitationItem[] | null; created_at: string }

export function fetchSessionsApi(notebookId: string): Promise<ChatSession[]> {
  return request.get(`/api/notebooks/${notebookId}/chat/sessions`).then((r) => r.data.items || [])
}
export function createSessionApi(notebookId: string, title?: string): Promise<ChatSession> {
  return request.post(`/api/notebooks/${notebookId}/chat/sessions`, { title }).then((r) => r.data)
}
export function deleteSessionApi(notebookId: string, sessionId: number): Promise<void> {
  return request.delete(`/api/notebooks/${notebookId}/chat/sessions/${sessionId}`).then((r) => r.data)
}
export function fetchMessagesApi(notebookId: string, sessionId: number): Promise<ChatMessage[]> {
  return request.get(`/api/notebooks/${notebookId}/chat/sessions/${sessionId}/messages`).then((r) => r.data.items || [])
}
export function sendMessageApi(notebookId: string, sessionId: number, content: string, sourceIds?: string[]): Promise<ChatMessage> {
  return request.post(`/api/notebooks/${notebookId}/chat/sessions/${sessionId}/messages`, { content, source_ids: sourceIds }).then((r) => r.data)
}

export function sendMessageStreamApi(
  notebookId: string, sessionId: number, content: string,
  onMessage: (text: string) => void, onCitations?: (citations: CitationItem[]) => void,
  onDone?: () => void, onError?: (err: any) => void,
  sourceIds?: string[],
): { abort: () => void } {
  const controller = new AbortController()
  const token = localStorage.getItem("token")
  fetch(`/api/notebooks/${notebookId}/chat/sessions/${sessionId}/messages`, {
    method: "POST",
    headers: { "Content-Type": "application/json", Authorization: token ? `Bearer ${token}` : "" },
    body: JSON.stringify({ content, source_ids: sourceIds }), signal: controller.signal,
  }).then(async (res) => {
    if (!res.ok) { const err = await res.json().catch(() => ({ detail: "请求失败" })); onError?.(err); return }
    const data = await res.json()
    if (data.content) onMessage(data.content)
    if (data.citations) onCitations?.(data.citations)
    onDone?.()
  }).catch((err) => { if (err.name !== "AbortError") onError?.(err) })
  return { abort: () => controller.abort() }
}
