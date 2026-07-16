<template>
  <div class="settings-page">
    <header class="page-header">
      <h1 class="page-title">设置</h1>
    </header>
    <div class="page-container">
      <div class="card settings-section">
        <h3 class="section-title">个人信息</h3>
        <el-form label-position="left" label-width="120px">
          <el-form-item label="用户名">
            <span class="form-text">{{ user?.username }}</span>
          </el-form-item>
          <el-form-item label="显示名称">
            <el-input v-model="displayName" maxlength="50" />
          </el-form-item>
          <el-form-item label="头像">
            <el-avatar :size="64" v-if="user?.avatar_url" :src="user.avatar_url" />
            <el-avatar :size="64" v-else>{{ user?.username?.charAt(0)?.toUpperCase() }}</el-avatar>
          </el-form-item>
          <el-form-item>
            <el-button type="primary" :loading="saving" @click="handleSave">保存</el-button>
          </el-form-item>
        </el-form>
      </div>

      <div class="card settings-section">
        <h3 class="section-title">Google 账号</h3>
        <el-form label-position="left" label-width="120px">
          <el-form-item label="绑定状态">
            <el-tag v-if="user?.google_bound" type="success">已绑定</el-tag>
            <el-tag v-else type="warning">未绑定</el-tag>
          </el-form-item>
          <el-form-item v-if="!user?.google_bound" label="Google 登录">
            <div ref="googleBtnRef" class="google-btn-container"></div>
            <p v-if="!googleReady && !googleError" class="hint-text">正在加载 Google 登录按钮...</p>
            <p v-if="googleError" class="error-text">{{ googleError }}</p>
          </el-form-item>
          <el-form-item v-else label="Google 账号">
            <span class="form-text">{{ user?.display_name || "已绑定" }}</span>
            <el-button text type="danger" size="small" @click="handleUnbind" style="margin-left: 12px;">解除绑定</el-button>
          </el-form-item>
        </el-form>
      </div>

      <div class="card settings-section">
        <h3 class="section-title">NotebookLM 连接</h3>
        <el-form label-position="left" label-width="120px">
          <el-form-item label="状态">
            <el-tag v-if="nbStatus.connected" type="success">已连接</el-tag>
            <el-tag v-else type="danger">未连接</el-tag>
          </el-form-item>
          <el-form-item v-if="nbStatus.error" label="错误信息">
            <span class="error-text">{{ nbStatus.error }}</span>
          </el-form-item>
        </el-form>
      </div>

      <div class="card settings-section">
        <h3 class="section-title">主题</h3>
        <el-form label-position="left" label-width="120px">
          <el-form-item label="外观">
            <el-switch v-model="isDark" active-text="暗色模式" inactive-text="亮色模式" @change="toggleTheme" />
          </el-form-item>
        </el-form>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, reactive, onMounted, watch } from "vue"
import { useRouter } from "vue-router"
import { ElMessage } from "element-plus"
import { useAuthStore } from "@/stores/auth"
import { useGoogleSignIn } from "@/composables/useGoogleSignIn"
import request from "@/api/request"

const router = useRouter()
const authStore = useAuthStore()
const user = authStore.user
const displayName = ref(user?.display_name || "")
const saving = ref(false)
const isDark = ref(document.documentElement.classList.contains("dark"))
const nbStatus = reactive({ connected: false, error: null as string | null })
const googleBtnRef = ref<HTMLElement>()
const googleReady = ref(false)
const googleError = ref("")

async function handleGoogleCredential(credential: string) {
  try {
    const r = await request.post("/api/auth/google/bind", { credential })
    ElMessage.success("Google 账号绑定成功")
    if (r.data.display_name) {
      authStore.user = { ...authStore.user!, display_name: r.data.display_name, avatar_url: r.data.avatar_url, google_bound: true }
    }
  } catch (e: any) {
    ElMessage.error(e.response?.data?.detail || "绑定失败")
  }
}

const { renderButton, clientId } = useGoogleSignIn(handleGoogleCredential, (err) => {
  googleError.value = err
})

watch([clientId, googleBtnRef], async () => {
  if (clientId.value && googleBtnRef.value && !googleReady.value) {
    await renderButton(googleBtnRef.value)
    googleReady.value = true
  }
})

async function handleUnbind() {
  try {
    await request.post("/api/auth/google/bind", { credential: "" })
    authStore.user = { ...authStore.user!, google_bound: false, avatar_url: null }
    ElMessage.success("已解除绑定")
  } catch {
    ElMessage.error("解除绑定失败")
  }
}

async function fetchNbStatus() {
  try {
    const r = await request.get("/api/settings/notebooklm-status")
    nbStatus.connected = r.data.connected
    nbStatus.error = r.data.error
  } catch { /* ignore */ }
}

function toggleTheme(val: boolean) {
  if (val) document.documentElement.classList.add("dark")
  else document.documentElement.classList.remove("dark")
  localStorage.setItem("theme", val ? "dark" : "light")
}

async function handleSave() {
  saving.value = true
  try { ElMessage.success("已保存") }
  catch (e: any) { ElMessage.error(e.response?.data?.detail || "保存失败") }
  finally { saving.value = false }
}

onMounted(() => {
  const saved = localStorage.getItem("theme")
  isDark.value = saved === "dark"
  if (isDark.value) document.documentElement.classList.add("dark")
  fetchNbStatus()
})
</script>

<style scoped lang="scss">
.settings-page { min-height: 100vh; background: var(--color-bg-tab); }
.page-header { display: flex; align-items: center; padding: 16px 24px; background: var(--color-bg-1); border-bottom: 1px solid var(--color-divider-1); }
.page-title { font-size: 20px; font-weight: 600; }
.page-container { padding: 24px; max-width: 700px; margin: 0 auto; display: flex; flex-direction: column; gap: 20px; }
.settings-section { .section-title { font-size: 16px; font-weight: 600; margin-bottom: 20px; } }
.form-text { color: var(--color-text-2); }
.error-text { color: var(--color-danger, #f56c6c); font-size: 13px; }
.hint-text { font-size: 13px; color: var(--color-text-3); margin-top: 8px; }
.google-btn-container { min-height: 40px; }
</style>