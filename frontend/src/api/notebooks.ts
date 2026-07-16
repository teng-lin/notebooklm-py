import request from "./request"

export interface Notebook {
  id: string; title: string; description: string | null
  source_count: number; chat_count: number; last_synced_at: string | null
  created_at: string; updated_at: string; is_owner: boolean; modified_at: string
}

export interface CreateNotebookRequest { title: string; description?: string }
export interface UpdateNotebookRequest { title?: string; description?: string }

export function fetchNotebooksApi(params?: { search?: string; sort?: string; page?: number; page_size?: number }): Promise<{ items: Notebook[]; total: number }> {
  return request.get("/api/notebooks", { params }).then((r) => {
    const data = r.data
    if (Array.isArray(data.notebooks)) {
      return { items: data.notebooks, total: data.notebooks.length }
    }
    return { items: data.items || [], total: data.total || 0 }
  })
}
export function fetchNotebookApi(id: string): Promise<Notebook> {
  return request.get(`/api/notebooks/${id}`).then((r) => r.data)
}
export function createNotebookApi(data: CreateNotebookRequest): Promise<Notebook> {
  return request.post("/api/notebooks", data).then((r) => r.data)
}
export function updateNotebookApi(id: string, data: UpdateNotebookRequest): Promise<Notebook> {
  return request.put(`/api/notebooks/${id}`, data).then((r) => r.data)
}
export function deleteNotebookApi(id: string): Promise<void> {
  return request.delete(`/api/notebooks/${id}`).then((r) => r.data)
}
export function syncNotebookApi(id: string): Promise<Notebook> {
  return request.post(`/api/notebooks/${id}/sync`).then((r) => r.data)
}
