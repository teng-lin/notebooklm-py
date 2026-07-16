<template>
  <div class="google-bind-page">
    <div class="bind-card">
      <div class="bind-icon"><el-icon :size="48"><Platform /></el-icon></div>
      <h2 class="bind-title">绑定 Google 账号</h2>
      <p class="bind-desc">绑定 Google 账号后，NotebookLM 将使用你的 Google 授权来调用知识库和生成服务。</p>
      <div v-if="!bound" class="bind-actions">
        <el-input v-model="authCode" placeholder="请输入 Google OAuth 授权码" class="code-input" />
        <el-button type="primary" class="bind-btn" :loading="binding" :disabled="!authCode.trim()" @click="handleBind">绑定 Google 账号</el-button>
        <el-button text class="skip-btn" @click="router.push('/')">跳过，稍后再说</el-button>
      </div>
      <div v-else class="bind-success">
        <el-icon :size="48" color="#67c23a"><SuccessFilled /></el-icon>
        <p>绑定成功！</p>
        <el-button type="primary" @click="router.push('/')">进入首页</el-button>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref } from "vue"
import { useRouter } from "vue-router"
import { Platform, SuccessFilled } from "@element-plus/icons-vue"
import { ElMessage } from "element-plus"
import { useAuthStore } from "@/stores/auth"

const router = useRouter()
const authStore = useAuthStore()
const authCode = ref("")
const binding = ref(false)
const bound = ref(!!authStore.user?.google_bound)

async function handleBind() {
  binding.value = true
  try { await authStore.bindGoogle(authCode.value.trim()); bound.value = true; ElMessage.success("Google 账号绑定成功") }
  catch (e: any) { ElMessage.error(e.response?.data?.detail || "绑定失败，请检查授权码") }
  finally { binding.value = false }
}
</script>

<style scoped lang="scss">
.google-bind-page {
  min-height: 100vh; display: flex; align-items: center; justify-content: center;
  background: var(--color-bg-tab); padding: 24px;
}
.bind-card {
  width: 100%; max-width: 440px; background: var(--color-bg-1);
  border-radius: var(--radius-card); box-shadow: var(--shadow-card); padding: 48px 32px; text-align: center;
}
.bind-icon { margin-bottom: 16px; color: var(--color-main-1); }
.bind-title { font-size: 22px; font-weight: 600; margin-bottom: 12px; }
.bind-desc { font-size: 14px; color: var(--color-text-2); line-height: 1.6; margin-bottom: 32px; }
.bind-actions { display: flex; flex-direction: column; gap: 16px; align-items: center; }
.code-input { width: 100%; }
.bind-btn { width: 100%; height: 44px; font-size: 16px; border-radius: var(--radius-button); background: var(--color-main-1); border-color: var(--color-main-1); }
.skip-btn { color: var(--color-text-3); font-size: 13px; }
.bind-success { display: flex; flex-direction: column; align-items: center; gap: 16px; p { font-size: 16px; color: var(--color-text-1); } }
</style>
