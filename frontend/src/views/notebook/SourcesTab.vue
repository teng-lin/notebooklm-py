<template>
  <div class="sources-tab">
    <div class="sources-header">
      <div class="source-type-switch">
        <el-radio-group v-model="sourceType" size="small">
          <el-radio-button value="local">本地资料</el-radio-button>
          <el-radio-button value="external">外部知识库</el-radio-button>
        </el-radio-group>
      </div>
      <div class="sources-actions">
        <el-button type="primary" size="small" @click="showUpload = true"><el-icon><Upload /></el-icon> 上传</el-button>
      </div>
    </div>
    <template v-if="sourceType === 'local'">
      <SourceList :sources="sources" empty-text="暂无资料，点击上方按钮上传" :show-delete="true" :show-rename="true" @select="()=>{}" @delete="handleDelete" @rename="handleRename" />
    </template>
    <template v-else>
      <ExternalKbPanel :notebook-id="notebookId" />
    </template>

    <UploadDialog :visible="showUpload" :notebook-id="notebookId" @update:visible="showUpload = $event" @uploaded="fetchSources" />

    <el-dialog v-model="showDeleteConfirm" title="删除资料" width="380px">
      <p>确定要删除「{{ deleteTarget?.original_filename || deleteTarget?.filename }}」吗？</p>
      <template #footer>
        <el-button @click="showDeleteConfirm = false">取消</el-button>
        <el-button type="danger" :loading="deleting" @click="confirmDelete">删除</el-button>
      </template>
    </el-dialog>
    <el-dialog v-model="showRenameDialog" title="重命名" width="380px">
      <el-input v-model="renameValue" maxlength="200" />
      <template #footer>
        <el-button @click="showRenameDialog = false">取消</el-button>
        <el-button type="primary" :loading="renaming" @click="confirmRename">确定</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted } from "vue"
import { useRoute } from "vue-router"
import { Upload } from "@element-plus/icons-vue"
import { ElMessage } from "element-plus"
import type { Source } from "@/api/sources"
import { fetchSourcesApi, deleteSourceApi, renameSourceApi } from "@/api/sources"
import SourceList from "@/components/SourceList.vue"
import UploadDialog from "@/components/UploadDialog.vue"
import ExternalKbPanel from "@/components/ExternalKbPanel.vue"

const route = useRoute()
const notebookId = computed(() => route.params.id as string)
const sourceType = ref("local")
const sources = ref<Source[]>([])
const loading = ref(false)
const showUpload = ref(false)
const showDeleteConfirm = ref(false); const deleteTarget = ref<Source | null>(null); const deleting = ref(false)
const showRenameDialog = ref(false); const renameTarget = ref<Source | null>(null); const renameValue = ref(""); const renaming = ref(false)

async function fetchSources() { loading.value = true; try { const res = await fetchSourcesApi(notebookId.value); sources.value = res.items } finally { loading.value = false } }

function handleDelete(source: Source) { deleteTarget.value = source; showDeleteConfirm.value = true }
async function confirmDelete() {
  if (!deleteTarget.value) return; deleting.value = true
  try { await deleteSourceApi(notebookId.value, deleteTarget.value.id); sources.value = sources.value.filter((s) => s.id !== deleteTarget.value!.id); ElMessage.success("已删除"); showDeleteConfirm.value = false }
  catch (e: any) { ElMessage.error(e.response?.data?.detail || "删除失败") }
  finally { deleting.value = false }
}

function handleRename(source: Source) { renameTarget.value = source; renameValue.value = source.original_filename || source.filename; showRenameDialog.value = true }
async function confirmRename() {
  if (!renameTarget.value || !renameValue.value.trim()) return; renaming.value = true
  try { const updated = await renameSourceApi(notebookId.value, renameTarget.value.id, renameValue.value.trim()); const idx = sources.value.findIndex((s) => s.id === updated.id); if (idx !== -1) sources.value[idx] = updated; ElMessage.success("已重命名"); showRenameDialog.value = false }
  catch (e: any) { ElMessage.error(e.response?.data?.detail || "重命名失败") }
  finally { renaming.value = false }
}

onMounted(() => { fetchSources() })
</script>

<style scoped lang="scss">
.sources-tab { max-width: 900px; margin: 0 auto; }
.sources-header { display: flex; align-items: center; justify-content: space-between; margin-bottom: 20px; }
.source-type-switch {
  :deep(.el-radio-button__inner) { border-radius: var(--radius-tab); font-size: 13px; }
  :deep(.el-radio-button:first-child .el-radio-button__inner) { border-radius: var(--radius-tab) 0 0 var(--radius-tab); }
  :deep(.el-radio-button:last-child .el-radio-button__inner) { border-radius: 0 var(--radius-tab) var(--radius-tab) 0; }
}
</style>
