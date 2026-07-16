<template>
  <el-dialog :model-value="visible" title="来源引用" width="500px" @update:model-value="$emit('update:visible', $event)">
    <div v-if="citation" class="citation-detail">
      <div class="citation-field"><span class="field-label">来源文档</span><span class="field-value">{{ citation.source_name }}</span></div>
      <div v-if="citation.page !== null" class="citation-field"><span class="field-label">页码</span><span class="field-value">第 {{ citation.page }} 页</span></div>
      <div class="citation-field"><span class="field-label">引用原文</span><blockquote class="citation-quote">{{ citation.text }}</blockquote></div>
    </div>
  </el-dialog>
</template>

<script setup lang="ts">
import type { CitationItem } from "@/api/chat"
defineProps<{ visible: boolean; citation: CitationItem | null }>(); defineEmits<{ "update:visible": [value: boolean] }>()
</script>

<style scoped lang="scss">
.citation-detail { display: flex; flex-direction: column; gap: 16px; }
.citation-field { display: flex; flex-direction: column; gap: 4px; }
.field-label { font-size: 12px; color: var(--color-text-3); font-weight: 500; }
.field-value { font-size: 14px; }
.citation-quote { margin: 0; padding: 12px 16px; background: var(--color-bg-tab); border-left: 3px solid var(--color-main-1); border-radius: 6px; font-size: 13px; line-height: 1.7; color: var(--color-text-2); }
</style>
