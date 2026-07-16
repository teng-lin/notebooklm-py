<template>
  <div class="document-generator">
    <el-form label-position="top">
      <el-form-item label="文档类型"><el-select v-model="docType"><el-option label="笔记" value="notes" /><el-option label="摘要" value="summary" /><el-option label="FAQ" value="faq" /><el-option label="学习指南" value="study_guide" /></el-select></el-form-item>
      <el-form-item label="生成指令"><el-input v-model="prompt" type="textarea" :rows="4" placeholder="描述文档内容和重点" /></el-form-item>
      <el-form-item><el-button type="primary" :loading="generating" @click="handleGenerate">生成文档</el-button></el-form-item>
    </el-form>
    <div v-if="result" class="preview-area">
      <div class="doc-preview markdown-body" v-html="renderedDoc"></div>
      <div class="doc-actions"><el-button @click="handleDownloadMD">下载 MD</el-button><el-button @click="handleDownloadPDF">下载 PDF</el-button></div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, computed } from "vue"; import { ElMessage } from "element-plus"
import { generateContentApi } from "@/api/generation"; import { marked } from "@/utils/marked"
const props = defineProps<{ notebookId: number }>(); const emit = defineEmits<{ back: [] }>()
const docType = ref("notes"); const prompt = ref(""); const generating = ref(false); const result = ref<any>(null)
const renderedDoc = computed(() => result.value?.content ? marked(result.value.content) : "")
async function handleGenerate() {
  if (!prompt.value.trim()) { ElMessage.warning("请输入生成指令"); return }
  generating.value = true
  try { const res = await generateContentApi(props.notebookId, { content_type: "document", prompt: prompt.value, options: { doc_type: docType.value } }); result.value = res; ElMessage.success("文档生成成功") }
  catch (e: any) { ElMessage.error(e.response?.data?.detail || "生成失败") }
  finally { generating.value = false }
}
function handleDownloadMD() { if (result.value?.content) { const blob = new Blob([result.value.content], { type: "text/markdown" }); const url = URL.createObjectURL(blob); window.open(url, "_blank") } }
function handleDownloadPDF() { if (result.value?.local_file_path) window.open(result.value.local_file_path, "_blank") }
</script>

<style scoped lang="scss">
.doc-preview { padding: 20px; background: var(--color-bg-tab); border-radius: var(--radius-card); min-height: 200px; }
.doc-actions { display: flex; gap: 8px; margin-top: 12px; }
</style>
