<template>
  <div class="login-page">
    <div class="login-card">
      <div class="login-header">
        <h1 class="brand">NotebookLM</h1>
        <p class="subtitle">你的 AI 知识助手</p>
      </div>
      <el-tabs v-model="activeTab" class="login-tabs" :stretch="true">
        <el-tab-pane label="登录" name="login">
          <el-form ref="loginFormRef" :model="loginForm" :rules="loginRules" label-position="top" @keyup.enter="handleLogin">
            <el-form-item label="用户名" prop="username">
              <el-input v-model="loginForm.username" placeholder="请输入用户名" :prefix-icon="User" />
            </el-form-item>
            <el-form-item label="密码" prop="password">
              <el-input v-model="loginForm.password" type="password" placeholder="请输入密码" show-password :prefix-icon="Lock" />
            </el-form-item>
            <el-form-item>
              <el-button type="primary" class="submit-btn" :loading="loading" @click="handleLogin">登录</el-button>
            </el-form-item>
          </el-form>
        </el-tab-pane>
        <el-tab-pane label="注册" name="register">
          <el-form ref="registerFormRef" :model="registerForm" :rules="registerRules" label-position="top" @keyup.enter="handleRegister">
            <el-form-item label="用户名" prop="username">
              <el-input v-model="registerForm.username" placeholder="请输入用户名" :prefix-icon="User" />
            </el-form-item>
            <el-form-item label="显示名称" prop="display_name">
              <el-input v-model="registerForm.display_name" placeholder="请输入显示名称（选填）" />
            </el-form-item>
            <el-form-item label="密码" prop="password">
              <el-input v-model="registerForm.password" type="password" placeholder="请输入密码" show-password :prefix-icon="Lock" />
            </el-form-item>
            <el-form-item label="确认密码" prop="confirmPassword">
              <el-input v-model="registerForm.confirmPassword" type="password" placeholder="请再次输入密码" show-password :prefix-icon="Lock" />
            </el-form-item>
            <el-form-item>
              <el-button type="primary" class="submit-btn" :loading="loading" @click="handleRegister">注册</el-button>
            </el-form-item>
          </el-form>
        </el-tab-pane>
      </el-tabs>
      <div class="login-footer">
        <el-button text @click="goToGoogleBind">绑定 Google 账号</el-button>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, reactive } from "vue"
import { useRouter } from "vue-router"
import { User, Lock } from "@element-plus/icons-vue"
import type { FormInstance, FormRules } from "element-plus"
import { ElMessage } from "element-plus"
import { useAuthStore } from "@/stores/auth"

const router = useRouter()
const authStore = useAuthStore()
const activeTab = ref("login")
const loading = ref(false)
const loginFormRef = ref<FormInstance>()
const registerFormRef = ref<FormInstance>()

const loginForm = reactive({ username: "", password: "" })
const loginRules: FormRules = {
  username: [{ required: true, message: "请输入用户名", trigger: "blur" }],
  password: [{ required: true, message: "请输入密码", trigger: "blur" }],
}

const registerForm = reactive({ username: "", display_name: "", password: "", confirmPassword: "" })
const registerRules: FormRules = {
  username: [{ required: true, message: "请输入用户名", trigger: "blur" }],
  password: [{ required: true, message: "请输入密码", trigger: "blur" }, { min: 6, message: "密码至少 6 位", trigger: "blur" }],
  confirmPassword: [{ required: true, message: "请确认密码", trigger: "blur" }, {
    validator: (_r: any, v: string, cb: any) => v === registerForm.password ? cb() : cb(new Error("两次输入的密码不一致")),
    trigger: "blur",
  }],
}

async function handleLogin() {
  const valid = await loginFormRef.value?.validate().catch(() => false)
  if (!valid) return
  loading.value = true
  try { await authStore.login(loginForm); ElMessage.success("登录成功"); router.push("/") }
  catch (e: any) { ElMessage.error(e.response?.data?.detail || "登录失败") }
  finally { loading.value = false }
}

async function handleRegister() {
  const valid = await registerFormRef.value?.validate().catch(() => false)
  if (!valid) return
  loading.value = true
  try {
    await authStore.register({ username: registerForm.username, password: registerForm.password, display_name: registerForm.display_name || undefined })
    ElMessage.success("注册成功"); router.push("/")
  } catch (e: any) { ElMessage.error(e.response?.data?.detail || "注册失败") }
  finally { loading.value = false }
}

function goToGoogleBind() { router.push("/auth/google") }
</script>

<style scoped lang="scss">
.login-page {
  min-height: 100vh; display: flex; align-items: center; justify-content: center;
  background: var(--color-bg-tab); padding: 24px;
}
.login-card {
  width: 100%; max-width: 420px; background: var(--color-bg-1);
  border-radius: var(--radius-card); box-shadow: var(--shadow-card); padding: 40px 32px 24px;
}
.login-header { text-align: center; margin-bottom: 32px; }
.brand { font-size: 28px; font-weight: 700; color: var(--color-main-1); margin-bottom: 8px; }
.subtitle { font-size: 14px; color: var(--color-text-3); }
.login-tabs {
  :deep(.el-tabs__header) { margin-bottom: 24px; }
  :deep(.el-tabs__item) {
    font-size: 15px; font-weight: 500; color: var(--color-text-3);
    &.is-active { color: var(--color-main-1); }
  }
  :deep(.el-tabs__active-bar) { background-color: var(--color-main-1); }
}
.submit-btn {
  width: 100%; height: 44px; font-size: 16px; border-radius: var(--radius-button);
  background: var(--color-main-1); border-color: var(--color-main-1);
}
.login-footer { text-align: center; margin-top: 16px; }
</style>
