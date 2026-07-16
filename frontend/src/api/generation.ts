import request from "./request"

export interface GeneratedContent {
  id: number; notebook_id: number; content_type: string; title: string | null; prompt: string | null
  engine: string; status: string; local_file_path: string | null; thumbnail_path: string | null; file_size: number | null; error_message: string | null
  ppt_page_count: number | null; ppt_template: string | null; ppt_json: string | null; ppt_preview_images: string | null
  mindmap_data: string | null; mindmap_layout: string | null
  infographic_template: string | null; infographic_blocks: string | null
  audio_file_path: string | null; duration_seconds: number | null; audio_speakers: string | null; audio_transcript: string | null
  video_file_path: string | null; video_duration_seconds: number | null; video_resolution: string | null; video_scenes: string | null; video_narration: string | null
  doc_page_count: number | null; doc_sections: string | null; doc_format: string | null
  created_at: string
}

export interface GenerateRequest { content_type: string; prompt: string; template?: string; options?: Record<string, any> }
export interface TemplateInfo { id: string; name: string; description: string; thumbnail_url?: string }

export function fetchGeneratedContentsApi(notebookId: string): Promise<GeneratedContent[]> { return request.get(`/api/notebooks/${notebookId}/generated`).then((r) => r.data) }
export function generateContentApi(notebookId: string, data: GenerateRequest): Promise<GeneratedContent> { return request.post(`/api/notebooks/${notebookId}/generate`, data).then((r) => r.data) }
export function fetchTemplatesApi(contentType: string): Promise<TemplateInfo[]> { return request.get("/api/generation/templates", { params: { content_type: contentType } }).then((r) => r.data) }
export function fetchGeneratedDetailApi(notebookId: string, generatedId: number): Promise<GeneratedContent> { return request.get(`/api/notebooks/${notebookId}/generated/${generatedId}`).then((r) => r.data) }
export function deleteGeneratedApi(notebookId: string, generatedId: number): Promise<void> { return request.delete(`/api/notebooks/${notebookId}/generated/${generatedId}`).then((r) => r.data) }
