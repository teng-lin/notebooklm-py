import request from "./request"

export interface Note {
  id: number
  title: string | null
  content: string
  created_at: string
  updated_at: string
}

export function fetchNotesApi(notebookId: string): Promise<{ items: Note[]; total: number }> {
  return request.get(`/api/notebooks/${notebookId}/notes`).then((r) => r.data)
}

export function createNoteApi(notebookId: string, data: { title?: string; content: string }): Promise<Note> {
  return request.post(`/api/notebooks/${notebookId}/notes`, data).then((r) => r.data)
}

export function updateNoteApi(notebookId: string, noteId: number, data: { title?: string; content?: string }): Promise<Note> {
  return request.put(`/api/notebooks/${notebookId}/notes/${noteId}`, data).then((r) => r.data)
}

export function deleteNoteApi(notebookId: string, noteId: number): Promise<void> {
  return request.delete(`/api/notebooks/${notebookId}/notes/${noteId}`).then((r) => r.data)
}
