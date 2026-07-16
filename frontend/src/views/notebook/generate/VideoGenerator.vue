<template>
  <div class="video-generator">
    <el-form label-position="top">
      <el-form-item label="旁白文本"><el-input v-model="narration" type="textarea" :rows="4" placeholder="输入视频旁白文本（每行一个场景）" /></el-form-item>
      <el-form-item label="分辨率"><el-select v-model="resolution"><el-option label="720p" value="720p" /><el-option label="1080p" value="1080p" /></el-select></el-form-item>
      <el-form-item><el-button type="primary" :loading="generating" @click="handleGenerate">生成视频</el-button></el-form-item>
    </el-form>
    <div v-if="result" class="preview-area">
      <video v-if="result.video_file_path" :src="result.video_file_path" controls style="width:100%" />
      <el-button @click="handleDownloadMP4">下载 MP4</el-button>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref } from "vue"; import { ElMessage } from "element-plus"
import { generateContentApi } from "@/api/generation"
const props = defineProps<{ notebookId: number }>(); const emit = defineEmits<{ back: [] }>()
const narration = ref(""); const resolution = ref("720p"); const generating = ref(false); const result = ref<any>(null)
async function handleGenerate() {
  if (!narration.value.trim()) { ElMessage.warning("请输入旁白文本"); return }
  generating.value = true
  try { const res = await generateContentApi(props.notebookId, { content_type: "video", prompt: narration.value, options: { resolution: resolution.value } }); result.value = res; ElMessage.success("视频生成成功") }
  catch (e: any) { ElMessage.error(e.response?.data?.detail || "生成失败") }
  finally { generating.value = false }
}
function handleDownloadMP4() { if (result.value?.video_file_path) window.open(result.value.video_file_path, "_blank") }
</script>
