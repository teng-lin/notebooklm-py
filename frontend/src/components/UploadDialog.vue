<template>
  <el-dialog :model-value="visible" title="上传资料" width="520px" :close-on-click-modal="false" @update:model-value="$emit('update:visible', $event)">
    <div class="upload-container">
      <div class="drag-zone" :class="{ 'drag-over': isDragOver }" @dragover.prevent="isDragOver = true" @dragleave.prevent="isDragOver = false" @drop.prevent="handleDrop" @click="triggerFileInput">
        <el-icon :size="48" color="#d0d0d0"><UploadFilled /></el-icon>
        <p class="drag-text">{{ isDragOver ? "释放文件以上传" : "拖拽文件到此处，或点击选择文件" }}</p>
        <p class="drag-hint">支持 PDF、Word、TXT 格式，单个文件最大 50MB</p>
      </div>
      <input ref="fileInputRef" type="file" accept=".pdf,.docx,.doc,.txt" style="display: none" multiple @change="handleFileChange" />
      <div class="url-input-group">
        <el-divider><span class="divider-text">或通过链接添加</span></el-divider>
        <div class="url-row">
          <el-input v-model="urlInput" placeholder="输入网页链接" clearable />
          <el-button type="primary" :disabled="!urlInput.trim()" :loading="addingUrl" @click="handleAddUrl">添加</el-button>
        </div>
      </div>
      <div v-if="uploadProgress.length > 0" class="progress-list">
        <div v-for="item in uploadProgress" :key="item.name" class="progress-item">
          <span class="progress-name">{{ item.name }}</span>
          <el-progress :percentage="item.percent" :status="item.status" :stroke-width="6" />
        </div>
      </div>
    </div>
  </el-dialog>
</template>

<script setup lang="ts">
import { ref } from "vue"
import { UploadFilled } from "@element-plus/icons-vue"
import { ElMessage } from "element-plus"
import { uploadSourceApi, addSourceUrlApi } from "@/api/sources"

const props = defineProps<{ visible: boolean; notebookId: string }>()
const emit = defineEmits<{ "update:visible": [value: boolean]; uploaded: [] }>()

const isDragOver = ref(false); const fileInputRef = ref<HTMLInputElement>(); const urlInput = ref(""); const addingUrl = ref(false)
interface UPI { name: string; percent: number; status: "success" | "exception" | "" }
const uploadProgress = ref<UPI[]>([])

function triggerFileInput() { fileInputRef.value?.click() }
function handleDrop(e: DragEvent) { isDragOver.value = false; const files = e.dataTransfer?.files; if (files) uploadFiles(Array.from(files)) }
function handleFileChange(e: Event) { const input = e.target as HTMLInputElement; if (input.files) uploadFiles(Array.from(input.files)); input.value = "" }

function uploadFiles(files: File[]) {
  for (const file of files) {
    const item: UPI = { name: file.name, percent: 0, status: "" }
    uploadProgress.value.push(item)
    uploadSourceApi(props.notebookId, file, (pct) => { item.percent = pct })
      .then(() => { item.status = "success"; emit("uploaded"); ElMessage.success(`${file.name} 上传成功`) })
      .catch((e) => { item.status = "exception"; ElMessage.error(`${file.name} 上传失败: ${e.response?.data?.detail || e.message}`) })
  }
}

async function handleAddUrl() {
  const url = urlInput.value.trim()
  if (!url) return; addingUrl.value = true
  try { await addSourceUrlApi(props.notebookId, url); ElMessage.success("链接已添加"); urlInput.value = ""; emit("uploaded") }
  catch (e: any) { ElMessage.error(e.response?.data?.detail || "添加失败") }
  finally { addingUrl.value = false }
}
</script>

<style scoped lang="scss">
.upload-container { display: flex; flex-direction: column; gap: 20px; }
.drag-zone { display: flex; flex-direction: column; align-items: center; justify-content: center; padding: 40px 20px; border: 2px dashed var(--color-divider-1); border-radius: var(--radius-card); cursor: pointer; transition: all 0.2s; gap: 12px; &:hover, &.drag-over { border-color: var(--color-main-1); background: rgba(255,54,80,.04); } }
.drag-text { font-size: 14px; color: var(--color-text-2); }
.drag-hint { font-size: 12px; color: var(--color-text-4); }
.url-input-group { .divider-text { font-size: 12px; color: var(--color-text-4); } }
.url-row { display: flex; gap: 8px; }
.progress-list { display: flex; flex-direction: column; gap: 12px; }
.progress-item { display: flex; flex-direction: column; gap: 4px; }
.progress-name { font-size: 13px; color: var(--color-text-1); }
</style>
