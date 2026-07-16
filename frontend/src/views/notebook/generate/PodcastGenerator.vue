<template>
  <div class="podcast-generator">
    <el-form label-position="top">
      <el-form-item label="说话人数量"><el-radio-group v-model="speakerCount"><el-radio :value="1">单人</el-radio><el-radio :value="2">双人对话</el-radio></el-radio-group></el-form-item>
      <el-form-item v-if="speakerCount === 2" label="说话人 1 名称"><el-input v-model="speaker1Name" placeholder="例如: 主持人" maxlength="20" /></el-form-item>
      <el-form-item v-if="speakerCount === 2" label="说话人 2 名称"><el-input v-model="speaker2Name" placeholder="例如: 嘉宾" maxlength="20" /></el-form-item>
      <el-form-item label="主题/方向"><el-input v-model="prompt" type="textarea" :rows="3" placeholder="描述播客的主题和风格" /></el-form-item>
      <el-form-item><el-button type="primary" :loading="generating" @click="handleGenerate">生成播客</el-button></el-form-item>
    </el-form>
    <div v-if="result" class="preview-area">
      <audio v-if="result.audio_file_path" :src="result.audio_file_path" controls style="width:100%" />
      <div v-if="result.audio_transcript" class="transcript"><h4>文字稿</h4><pre>{{ result.audio_transcript }}</pre></div>
      <el-button @click="handleDownloadMP3">下载 MP3</el-button>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref } from "vue"; import { ElMessage } from "element-plus"
import { generateContentApi } from "@/api/generation"
const props = defineProps<{ notebookId: number }>(); const emit = defineEmits<{ back: [] }>()
const speakerCount = ref(2); const speaker1Name = ref("主持人"); const speaker2Name = ref("嘉宾"); const prompt = ref(""); const generating = ref(false); const result = ref<any>(null)
async function handleGenerate() {
  if (!prompt.value.trim()) { ElMessage.warning("请输入主题"); return }
  generating.value = true
  try {
    const res = await generateContentApi(props.notebookId, { content_type: "podcast", prompt: prompt.value, options: { speaker_count: speakerCount.value, speaker1: speaker1Name.value, speaker2: speaker2Name.value } })
    result.value = res; ElMessage.success("播客生成成功")
  } catch (e: any) { ElMessage.error(e.response?.data?.detail || "生成失败") }
  finally { generating.value = false }
}
function handleDownloadMP3() { if (result.value?.audio_file_path) window.open(result.value.audio_file_path, "_blank") }
</script>

<style scoped lang="scss">
.transcript { margin-top: 16px; h4 { font-size: 14px; font-weight: 600; margin-bottom: 8px; } pre { background: var(--color-bg-tab); padding: 16px; border-radius: var(--radius-card); font-size: 13px; line-height: 1.7; white-space: pre-wrap; max-height: 300px; overflow-y: auto; } }
</style>
