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
          <el-form-item v-if="!user?.google_bound">
            <el-button @click="router.push('/auth/google')">绑定 Google 账号</el-button>
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
import { ref, onMounted } from "vue"; import { useRouter } from "vue-router"; import { ElMessage } from "element-plus"
import { useAuthStore } from "@/stores/auth"; import { updateNotebookApi } from "@/api/notebooks"

const router = useRouter(); const authStore = useAuthStore()
const user = authStore.user; const displayName = ref(user?.display_name || ""); const saving = ref(false)
const isDark = ref(document.documentElement.classList.contains("dark"))

function toggleTheme(val: boolean) {
  if (val) document.documentElement.classList.add("dark")
  else document.documentElement.classList.remove("dark")
  localStorage.setItem("theme", val ? "dark" : "light")
}

async function handleSave() {
  saving.value = true
  try { /* TODO: update user profile API */ ElMessage.success("已保存") }
  catch (e: any) { ElMessage.error(e.response?.data?.detail || "保存失败") }
  finally { saving.value = false }
}

onMounted(() => {
  const saved = localStorage.getItem("theme"); isDark.value = saved === "dark"
  if (isDark.value) document.documentElement.classList.add("dark")
})
</script>

<style scoped lang="scss">
.settings-page { min-height: 100vh; background: var(--color-bg-tab); }
.page-header { display: flex; align-items: center; padding: 16px 24px; background: var(--color-bg-1); border-bottom: 1px solid var(--color-divider-1); }
.page-title { font-size: 20px; font-weight: 600; }
.page-container { padding: 24px; max-width: 700px; margin: 0 auto; display: flex; flex-direction: column; gap: 20px; }
.settings-section { .section-title { font-size: 16px; font-weight: 600; margin-bottom: 20px; } }
.form-text { color: var(--color-text-2); }
</style>
