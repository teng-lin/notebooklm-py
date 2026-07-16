<template>
  <div class="ppt-generator">
    <GenerationTemplatePicker :templates="templates" :selected="selectedTemplate" @select="selectedTemplate = $event" />
    <el-form label-position="top">
      <el-form-item label="生成指令"><el-input v-model="prompt" type="textarea" :rows="4" placeholder="描述你想要生成的 PPT 主题和内容要点" /></el-form-item>
      <el-form-item><el-button type="primary" :loading="generating" @click="handleGenerate">生成 PPT</el-button></el-form-item>
    </el-form>
    <div v-if="result" class="preview-area">
      <div class="slide-strip">
        <div v-for="(slide, i) in previewSlides" :key="i" class="slide-thumb" :class="{ active: i === currentSlide }" @click="currentSlide = i">
          <div class="slide-mini">{{ slide.title || `第 ${i+1} 页` }}</div>
        </div>
      </div>
      <div class="slide-preview">
        <h4>{{ previewSlides[currentSlide]?.title || "PPT 预览" }}</h4>
        <ul v-if="previewSlides[currentSlide]?.bullets"><li v-for="(b, j) in previewSlides[currentSlide].bullets" :key="j">{{ b }}</li></ul>
      </div>
      <el-button type="primary" @click="handleDownload">下载 PPTX</el-button>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted } from "vue"; import { ElMessage } from "element-plus"
import GenerationTemplatePicker from "@/components/GenerationTemplatePicker.vue"
import { fetchTemplatesApi, generateContentApi } from "@/api/generation"
const props = defineProps<{ notebookId: string }>(); const emit = defineEmits<{ back: [] }>()
const templates = ref<any[]>([]); const selectedTemplate = ref(""); const prompt = ref(""); const generating = ref(false); const result = ref<any>(null)
const previewSlides = ref<any[]>([]); const currentSlide = ref(0)
async function handleGenerate() {
  if (!prompt.value.trim()) { ElMessage.warning("请输入生成指令"); return }
  generating.value = true
  try {
    const res = await generateContentApi(props.notebookId, { content_type: "ppt", prompt: prompt.value, template: selectedTemplate.value || undefined })
    result.value = res
    if (res.ppt_json) previewSlides.value = JSON.parse(res.ppt_json)
    else previewSlides.value = [{ title: "示例 PPT", bullets: ["内容点 1", "内容点 2", "内容点 3"] }]
    ElMessage.success("PPT 生成成功")
  } catch (e: any) { ElMessage.error(e.response?.data?.detail || "生成失败") }
  finally { generating.value = false }
}
function handleDownload() { if (result.value?.local_file_path) window.open(result.value.local_file_path, "_blank"); else ElMessage.info("下载链接暂不可用") }
onMounted(async () => { try { templates.value = await fetchTemplatesApi("ppt") } catch {} })
</script>

<style scoped lang="scss">
.preview-area { display: flex; flex-direction: column; gap: 16px; margin-top: 20px; }
.slide-strip { display: flex; gap: 8px; overflow-x: auto; padding-bottom: 8px; }
.slide-thumb { min-width: 120px; height: 70px; background: var(--color-bg-tab); border-radius: 6px; padding: 8px; cursor: pointer; font-size: 11px; display: flex; align-items: center; justify-content: center; text-align: center; border: 2px solid transparent; &.active { border-color: var(--color-main-1); } }
.slide-preview { padding: 20px; background: var(--color-bg-tab); border-radius: var(--radius-card); min-height: 200px; }
</style>
