import request from "./request"

export interface Source {
  id: number; remote_id: string; filename: string; original_filename: string
  file_type: string; file_size: number; page_count: number
  local_path: string | null; source_url: string | null; summary: string | null
  status: string; created_at: string; updated_at: string
}

export function fetchSourcesApi(notebookId: number, params?: { page?: number; page_size?: number }): Promise<{ items: Source[]; total: number }> {
  return request.get(`/api/notebooks/${notebookId}/sources`, { params }).then((r) => r.data)
}

export function uploadSourceApi(notebookId: number, file: File, onProgress?: (pct: number) => void): Promise<Source> {
  const form = new FormData()
  form.append("file", file)
  return request.post(`/api/notebooks/${notebookId}/sources/upload`, form, {
    headers: { "Content-Type": "multipart/form-data" },
    onUploadProgress: (e) => { if (e.total && onProgress) onProgress(Math.round((e.loaded / e.total) * 100)) },
  }).then((r) => r.data)
}

export function addSourceUrlApi(notebookId: number, url: string): Promise<Source> {
  return request.post(`/api/notebooks/${notebookId}/sources/url`, { url }).then((r) => r.data)
}

export function deleteSourceApi(notebookId: number, sourceId: number): Promise<void> {
  return request.delete(`/api/notebooks/${notebookId}/sources/${sourceId}`).then((r) => r.data)
}

export function renameSourceApi(notebookId: number, sourceId: number, filename: string): Promise<Source> {
  return request.put(`/api/notebooks/${notebookId}/sources/${sourceId}`, { filename }).then((r) => r.data)
}
