import request from "./request"

export interface ExternalKbConnection { id: number; name: string; provider_type: string; api_base_url: string; auth_type: string; is_active: boolean; last_sync_at: string | null; created_at: string }
export interface ExternalKbCollection { id: number; connection_id: number; remote_id: string; name: string; description: string | null; document_count: number }
export interface ExternalKbDocument { id: number; collection_id: number; remote_id: string; title: string; summary: string | null; file_type: string | null; file_size: number | null; url: string | null }
export interface ImportResult { id: number; source_id: number; status: string }

export function fetchConnectionsApi(): Promise<ExternalKbConnection[]> { return request.get("/api/external-kb/connections").then((r) => r.data) }
export function createConnectionApi(data: { name: string; provider_type: string; api_base_url: string; auth_type: string; auth_credentials?: Record<string, string> }): Promise<ExternalKbConnection> { return request.post("/api/external-kb/connections", data).then((r) => r.data) }
export function updateConnectionApi(id: number, data: Partial<{ name: string; api_base_url: string; auth_type: string; auth_credentials: Record<string, string> }>): Promise<ExternalKbConnection> { return request.put(`/api/external-kb/connections/${id}`, data).then((r) => r.data) }
export function deleteConnectionApi(id: number): Promise<void> { return request.delete(`/api/external-kb/connections/${id}`).then((r) => r.data) }
export function testConnectionApi(id: number): Promise<{ success: boolean; message: string }> { return request.post(`/api/external-kb/connections/${id}/test`).then((r) => r.data) }
export function fetchCollectionsApi(connectionId: number): Promise<ExternalKbCollection[]> { return request.get(`/api/external-kb/connections/${connectionId}/collections`).then((r) => r.data) }
export function fetchDocumentsApi(connectionId: number, collectionId: number): Promise<ExternalKbDocument[]> { return request.get(`/api/external-kb/connections/${connectionId}/collections/${collectionId}/documents`).then((r) => r.data) }
export function searchExternalKbApi(connectionId: number, collectionId: number, query: string): Promise<ExternalKbDocument[]> { return request.get(`/api/external-kb/connections/${connectionId}/collections/${collectionId}/search`, { params: { q: query } }).then((r) => r.data) }
export function importDocumentApi(connectionId: number, documentId: number, notebookId: string): Promise<ImportResult> { return request.post("/api/external-kb/import", { connection_id: connectionId, document_id: documentId, target_notebook_id: notebookId }).then((r) => r.data) }
