<template>
  <div class="infographic-generator">
    <GenerationTemplatePicker :templates="templates" :selected="selectedTemplate" @select="selectedTemplate = $event" />
    <el-form label-position="top">
      <el-form-item label="内容描述"><el-input v-model="prompt" type="textarea" :rows="3" placeholder="描述信息图的内容" /></el-form-item>
      <el-form-item><el-button type="primary" :loading="generating" @click="handleGenerate">生成信息图</el-button></el-form-item>
    </el-form>
    <div v-if="result" class="preview-area">
      <div class="infographic-preview"><img v-if="result.local_file_path" :src="result.local_file_path" alt="信息图预览" /></div>
      <el-button @click="handleDownloadPNG">下载 PNG</el-button>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted } from "vue"; import { ElMessage } from "element-plus"
import GenerationTemplatePicker from "@/components/GenerationTemplatePicker.vue"
import { fetchTemplatesApi, generateContentApi } from "@/api/generation"
const props = defineProps<{ notebookId: number }>(); const emit = defineEmits<{ back: [] }>()
const templates = ref<any[]>([]); const selectedTemplate = ref(""); const prompt = ref(""); const generating = ref(false); const result = ref<any>(null)
async function handleGenerate() {
  if (!prompt.value.trim()) { ElMessage.warning("请输入内容描述"); return }
  generating.value = true
  try { const res = await generateContentApi(props.notebookId, { content_type: "infographic", prompt: prompt.value, template: selectedTemplate.value || undefined }); result.value = res; ElMessage.success("信息图生成成功") }
  catch (e: any) { ElMessage.error(e.response?.data?.detail || "生成失败") }
  finally { generating.value = false }
}
function handleDownloadPNG() { if (result.value?.local_file_path) window.open(result.value.local_file_path, "_blank") }
onMounted(async () => { try { templates.value = await fetchTemplatesApi("infographic") } catch {} })
</script>

<style scoped lang="scss">
.infographic-preview { img { max-width: 100%; border-radius: var(--radius-card); } }
</style>
