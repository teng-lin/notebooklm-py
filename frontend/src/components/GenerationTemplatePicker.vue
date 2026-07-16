<template>
  <div class="template-picker">
    <h4 class="picker-title">选择模板</h4>
    <div class="template-grid">
      <div v-for="tpl in templates" :key="tpl.id" class="template-card" :class="{ selected: selected === tpl.id }" @click="$emit('select', tpl.id)">
        <div class="template-thumb"><img v-if="tpl.thumbnail_url" :src="tpl.thumbnail_url" :alt="tpl.name" /><el-icon v-else :size="32"><Picture /></el-icon></div>
        <span class="template-name">{{ tpl.name }}</span>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { Picture } from "@element-plus/icons-vue"; import type { TemplateInfo } from "@/api/generation"
defineProps<{ templates: TemplateInfo[]; selected?: string }>(); defineEmits<{ select: [templateId: string] }>()
</script>

<style scoped lang="scss">
.template-picker { margin-bottom: 20px; }
.picker-title { font-size: 14px; font-weight: 600; margin-bottom: 12px; }
.template-grid { display: grid; grid-template-columns: repeat(3, 1fr); gap: 12px; }
.template-card { display: flex; flex-direction: column; align-items: center; gap: 8px; padding: 16px 12px; border: 2px solid var(--color-divider-1); border-radius: var(--radius-card); cursor: pointer; transition: all .2s; &:hover { border-color: var(--color-text-3); } &.selected { border-color: var(--color-main-1); background: rgba(255,54,80,.04); } }
.template-thumb { width: 80px; height: 60px; display: flex; align-items: center; justify-content: center; background: var(--color-bg-tab); border-radius: 8px; overflow: hidden; img { width: 100%; height: 100%; object-fit: cover; } .el-icon { color: var(--color-text-4); } }
.template-name { font-size: 13px; font-weight: 500; text-align: center; }
</style>
