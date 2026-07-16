<template>
  <div class="mindmap-generator">
    <GenerationTemplatePicker :templates="templates" :selected="selectedTemplate" @select="selectedTemplate = $event" />
    <el-form label-position="top">
      <el-form-item label="生成指令"><el-input v-model="prompt" type="textarea" :rows="3" placeholder="描述思维导图的主题" /></el-form-item>
      <el-form-item>
        <el-button type="primary" :loading="generating" @click="handleGenerate">生成脑图</el-button>
      </el-form-item>
    </el-form>
    <div v-if="result" class="preview-area">
      <div class="mindmap-canvas" ref="canvasRef"></div>
      <div class="actions"><el-button @click="handleDownloadJSON">下载 JSON</el-button><el-button @click="handleDownloadPNG">下载 PNG</el-button></div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted } from "vue"; import { ElMessage } from "element-plus"
import GenerationTemplatePicker from "@/components/GenerationTemplatePicker.vue"
import { fetchTemplatesApi, generateContentApi } from "@/api/generation"
const props = defineProps<{ notebookId: string }>(); const emit = defineEmits<{ back: [] }>()
const templates = ref<any[]>([]); const selectedTemplate = ref(""); const prompt = ref(""); const generating = ref(false); const result = ref<any>(null); const canvasRef = ref<HTMLElement>()
async function handleGenerate() {
  if (!prompt.value.trim()) { ElMessage.warning("请输入生成指令"); return }
  generating.value = true
  try { const res = await generateContentApi(props.notebookId, { content_type: "mindmap", prompt: prompt.value, template: selectedTemplate.value || undefined }); result.value = res; ElMessage.success("脑图生成成功") }
  catch (e: any) { ElMessage.error(e.response?.data?.detail || "生成失败") }
  finally { generating.value = false }
}
function handleDownloadJSON() { if (result.value?.mindmap_data) { const blob = new Blob([result.value.mindmap_data], { type: "application/json" }); const url = URL.createObjectURL(blob); window.open(url, "_blank") } }
function handleDownloadPNG() { if (result.value?.local_file_path) window.open(result.value.local_file_path, "_blank") }
onMounted(async () => { try { templates.value = await fetchTemplatesApi("mindmap") } catch {} })
</script>

<style scoped lang="scss">
.mindmap-canvas { min-height: 300px; background: var(--color-bg-tab); border-radius: var(--radius-card); padding: 20px; }
.actions { display: flex; gap: 8px; margin-top: 12px; }
</style>
