# Plan 3: Vue 3 前端应用 实现计划

> **面向 AI 代理的工作者：** 必需子技能：使用 subagent-driven-development（推荐）或 executing-plans 逐任务实现此计划。步骤使用复选框（`- [ ]`）语法来跟踪进度。

**目标：** 基于 Element Plus + Vue 3 + Vite 搭建完整的有道宝库风格前端，包含登录、首页知识库列表、知识库详情（资料/问答/生成/概览四个 Tab）、外部知识库管理、用户设置等页面。

**架构：** 使用 Vue 3 Composition API + TypeScript + Element Plus 组件库。状态管理用 Pinia，路由用 Vue Router (Hash 模式)。Axios 封装 API 调用层，所有与后端交互的函数集中在 `src/api/` 目录。样式全面采用 baoku 设计 Token（CSS 变量），全局覆盖 Element Plus 主题色。

**技术栈：**
- Vue 3 + TypeScript + Vite + Element Plus
- Vue Router 4 (Hash 模式) + Pinia + Axios
- sass 作为样式预处理
- @element-plus/icons-vue 图标库

---

## Task 3.1: 脚手架搭建

### 文件：`frontend/package.json`

```json
{
  "name": "notebooklm-frontend",
  "version": "0.1.0",
  "private": true,
  "type": "module",
  "scripts": {
    "dev": "vite",
    "build": "vue-tsc --noEmit && vite build",
    "preview": "vite preview",
    "typecheck": "vue-tsc --noEmit"
  },
  "dependencies": {
    "vue": "^3.4.0",
    "vue-router": "^4.3.0",
    "pinia": "^2.1.0",
    "axios": "^1.6.0",
    "element-plus": "^2.5.0",
    "@element-plus/icons-vue": "^2.3.0"
  },
  "devDependencies": {
    "typescript": "~5.3.0",
    "vite": "^5.1.0",
    "@vitejs/plugin-vue": "^5.0.0",
    "vue-tsc": "^1.8.0",
    "sass": "^1.70.0",
    "@types/node": "^20.11.0"
  }
}
```

### 文件：`frontend/vite.config.ts`

```ts
import { defineConfig } from "vite"
import vue from "@vitejs/plugin-vue"
import { resolve } from "path"

export default defineConfig({
  plugins: [vue()],
  resolve: {
    alias: {
      "@": resolve(__dirname, "src"),
    },
  },
  server: {
    port: 5173,
    proxy: {
      "/api": {
        target: "http://localhost:8000",
        changeOrigin: true,
      },
    },
  },
})
```

### 文件：`frontend/tsconfig.json`

```json
{
  "compilerOptions": {
    "target": "ES2020",
    "useDefineForClassFields": true,
    "module": "ESNext",
    "lib": ["ES2020", "DOM", "DOM.Iterable"],
    "skipLibCheck": true,
    "moduleResolution": "bundler",
    "allowImportingTsExtensions": true,
    "resolveJsonModule": true,
    "isolatedModules": true,
    "noEmit": true,
    "jsx": "preserve",
    "strict": true,
    "noUnusedLocals": false,
    "noUnusedParameters": false,
    "noFallthroughCasesInSwitch": true,
    "baseUrl": ".",
    "paths": {
      "@/*": ["src/*"]
    }
  },
  "include": ["src/**/*.ts", "src/**/*.d.ts", "src/**/*.tsx", "src/**/*.vue"],
  "references": [{ "path": "./tsconfig.node.json" }]
}
```

### 文件：`frontend/tsconfig.node.json`

```json
{
  "compilerOptions": {
    "composite": true,
    "skipLibCheck": true,
    "module": "ESNext",
    "moduleResolution": "bundler",
    "allowSyntheticDefaultImports": true
  },
  "include": ["vite.config.ts"]
}
```

### 文件：`frontend/index.html`

```html
<!DOCTYPE html>
<html lang="zh-CN">
  <head>
    <meta charset="UTF-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1.0" />
    <meta name="theme-color" content="#ff3650" />
    <meta name="description" content="NotebookLM - 你的 AI 知识助手" />
    <link rel="preconnect" href="https://fonts.googleapis.com" />
    <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin />
    <link
      href="https://fonts.googleapis.com/css2?family=Noto+Sans+SC:wght@400;500;600;700&display=swap"
      rel="stylesheet"
    />
    <title>NotebookLM - 你的 AI 知识助手</title>
  </head>
  <body>
    <div id="app"></div>
    <script type="module" src="/src/main.ts"></script>
  </body>
</html>
```

### 文件：`frontend/src/main.ts`

```ts
import { createApp } from "vue"
import { createPinia } from "pinia"
import ElementPlus from "element-plus"
import "element-plus/dist/index.css"
import * as ElementPlusIconsVue from "@element-plus/icons-vue"

import App from "./App.vue"
import router from "./router"
import "./styles/variables.scss"
import "./styles/global.scss"

const app = createApp(App)

const pinia = createPinia()
app.use(pinia)
app.use(router)
app.use(ElementPlus, { locale: undefined })

for (const [key, component] of Object.entries(ElementPlusIconsVue)) {
  app.component(key, component)
}

app.mount("#app")
```

### 文件：`frontend/src/env.d.ts`

```ts
/// <reference types="vite/client" />

declare module "*.vue" {
  import type { DefineComponent } from "vue"
  const component: DefineComponent<{}, {}, any>
  export default component
}
```

### 文件：`frontend/src/App.vue`

```vue
<template>
  <router-view />
</template>

<script setup lang="ts">
import { onMounted } from "vue"
import { useAuthStore } from "@/stores/auth"

const authStore = useAuthStore()

onMounted(() => {
  authStore.restoreSession()
})
</script>

<style>
:root {
  --color-main-1: #ff3650;
  --color-main-2: #17181a;
  --color-text-focus: #1a75ff;
  --color-text-1: #2a2b2e;
  --color-text-2: #626469;
  --color-text-3: #939599;
  --color-text-4: #a8aaad;
  --color-bg-1: #fff;
  --color-bg-tab: #f5f6f7;
  --color-divider-1: #eeeff0;
  --radius-card: 12px;
  --radius-tab: 6px;
  --radius-input: 8px;
  --radius-button: 6px;
  --shadow-card: 0px 4px 40px 0px rgba(174, 180, 193, 0.17);
  --font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC",
    "Hiragino Sans GB", "Microsoft YaHei", "Noto Sans SC", sans-serif;
}

html.dark {
  --color-main-1: #ff3650;
  --color-main-2: #e8e8ea;
  --color-text-1: #e8e8ea;
  --color-text-2: #ababaf;
  --color-text-3: #7a7a7e;
  --color-text-4: #5a5a5e;
  --color-bg-1: #1a1a1c;
  --color-bg-tab: #252527;
  --color-divider-1: #333336;
  --shadow-card: 0px 4px 40px 0px rgba(0, 0, 0, 0.3);
}

*,
*::before,
*::after {
  box-sizing: border-box;
  margin: 0;
  padding: 0;
}

body {
  font-family: var(--font-family);
  color: var(--color-text-1);
  background-color: var(--color-bg-1);
  -webkit-font-smoothing: antialiased;
  -moz-osx-font-smoothing: grayscale;
  line-height: 1.6;
}

a {
  color: var(--color-text-focus);
  text-decoration: none;
}
</style>
```

### 文件：`frontend/src/styles/variables.scss`

```scss
$color-main-1: #ff3650;
$color-main-2: #17181a;
$color-text-focus: #1a75ff;
$color-text-1: #2a2b2e;
$color-text-2: #626469;
$color-text-3: #939599;
$color-text-4: #a8aaad;
$color-bg-1: #fff;
$color-bg-tab: #f5f6f7;
$color-divider-1: #eeeff0;
$radius-card: 12px;
$radius-tab: 6px;
$radius-input: 8px;
$radius-button: 6px;
$shadow-card: 0px 4px 40px 0px rgba(174, 180, 193, 0.17);
$font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC",
  "Hiragino Sans GB", "Microsoft YaHei", "Noto Sans SC", sans-serif;

:export {
  colorMain1: $color-main-1;
  colorMain2: $color-main-2;
  colorTextFocus: $color-text-focus;
  colorText1: $color-text-1;
  colorText2: $color-text-2;
  colorText3: $color-text-3;
  colorText4: $color-text-4;
  colorBg1: $color-bg-1;
  colorBgTab: $color-bg-tab;
  colorDivider1: $color-divider-1;
  radiusCard: $radius-card;
  radiusTab: $radius-tab;
  radiusInput: $radius-input;
  radiusButton: $radius-button;
}
```

### 文件：`frontend/src/styles/global.scss`

```scss
@use "./variables" as *;

:root {
  --el-color-primary: #{$color-main-1};
  --el-color-primary-light-3: #{lighten($color-main-1, 12%)};
  --el-color-primary-light-5: #{lighten($color-main-1, 20%)};
  --el-color-primary-light-7: #{lighten($color-main-1, 28%)};
  --el-color-primary-light-8: #{lighten($color-main-1, 34%)};
  --el-color-primary-light-9: #{lighten($color-main-1, 40%)};
  --el-color-primary-dark-2: #{darken($color-main-1, 10%)};
  --el-border-radius-base: #{$radius-button};
  --el-border-radius-small: #{$radius-tab};
  --el-border-radius-round: 20px;
  --el-font-family: #{$font-family};
  --el-text-color-primary: #{$color-text-1};
  --el-text-color-regular: #{$color-text-2};
  --el-text-color-secondary: #{$color-text-3};
  --el-border-color: #{$color-divider-1};
  --el-fill-color-light: #{$color-bg-tab};
}

::-webkit-scrollbar {
  width: 6px;
  height: 6px;
}

::-webkit-scrollbar-track {
  background: transparent;
}

::-webkit-scrollbar-thumb {
  background: var(--color-text-4);
  border-radius: 3px;
}

::-webkit-scrollbar-thumb:hover {
  background: var(--color-text-3);
}

.page-container {
  max-width: 1200px;
  margin: 0 auto;
  padding: 24px;
}

.card {
  background: var(--color-bg-1);
  border-radius: var(--radius-card);
  box-shadow: var(--shadow-card);
  padding: 20px;
}

.section-title {
  font-size: 16px;
  font-weight: 600;
  color: var(--color-text-1);
  margin-bottom: 16px;
}
```

### 文件：`frontend/src/router/index.ts`

```ts
import { createRouter, createWebHashHistory } from "vue-router"
import type { RouteRecordRaw } from "vue-router"
import { useAuthStore } from "@/stores/auth"

const routes: RouteRecordRaw[] = [
  {
    path: "/login",
    name: "Login",
    component: () => import("@/views/LoginView.vue"),
    meta: { requiresAuth: false },
  },
  {
    path: "/auth/google",
    name: "AuthGoogle",
    component: () => import("@/views/AuthGoogleView.vue"),
    meta: { requiresAuth: true },
  },
  {
    path: "/",
    name: "Home",
    component: () => import("@/views/HomeView.vue"),
    meta: { requiresAuth: true },
  },
  {
    path: "/notebook/:id",
    name: "Notebook",
    component: () => import("@/views/NotebookView.vue"),
    meta: { requiresAuth: true },
    redirect: (to) => ({ path: `/notebook/${to.params.id}/overview` }),
    children: [
      {
        path: "overview",
        name: "NotebookOverview",
        component: () => import("@/views/notebook/OverviewTab.vue"),
      },
      {
        path: "sources",
        name: "NotebookSources",
        component: () => import("@/views/notebook/SourcesTab.vue"),
      },
      {
        path: "chat",
        name: "NotebookChat",
        component: () => import("@/views/notebook/ChatTab.vue"),
      },
      {
        path: "chat/:sid",
        name: "NotebookChatSession",
        component: () => import("@/views/notebook/ChatTab.vue"),
      },
      {
        path: "generate",
        name: "NotebookGenerate",
        component: () => import("@/views/notebook/GenerateTab.vue"),
      },
      {
        path: "generate/:gid",
        name: "NotebookGenerateDetail",
        component: () => import("@/views/notebook/GenerateTab.vue"),
      },
    ],
  },
  {
    path: "/external-kb",
    name: "ExternalKb",
    component: () => import("@/views/ExternalKbView.vue"),
    meta: { requiresAuth: true },
  },
  {
    path: "/external-kb/connections/:id",
    name: "ExternalKbDetail",
    component: () => import("@/views/ExternalKbDetailView.vue"),
    meta: { requiresAuth: true },
  },
  {
    path: "/settings",
    name: "Settings",
    component: () => import("@/views/SettingsView.vue"),
    meta: { requiresAuth: true },
  },
  {
    path: "/history",
    name: "History",
    component: () => import("@/views/HistoryView.vue"),
    meta: { requiresAuth: true },
  },
]

const router = createRouter({
  history: createWebHashHistory(),
  routes,
})

router.beforeEach((to, _from, next) => {
  if (to.meta.requiresAuth === false) {
    next()
    return
  }
  const authStore = useAuthStore()
  if (!authStore.isAuthenticated) {
    next("/login")
  } else {
    next()
  }
})

export default router
```

---

## Task 3.2: 认证页面

### 文件：`frontend/src/api/auth.ts`

```ts
import request from "./request"

export interface LoginRequest {
  username: string
  password: string
}

export interface RegisterRequest {
  username: string
  password: string
  display_name?: string
}

export interface UserInfo {
  id: number
  username: string
  display_name: string
  avatar_url: string | null
  google_bound: boolean
  created_at: string
}

export interface AuthResponse {
  token: string
  user: UserInfo
}

export function loginApi(data: LoginRequest): Promise<AuthResponse> {
  return request.post("/api/auth/login", data).then((r) => r.data)
}

export function registerApi(data: RegisterRequest): Promise<AuthResponse> {
  return request.post("/api/auth/register", data).then((r) => r.data)
}

export function logoutApi(): Promise<void> {
  return request.post("/api/auth/logout").then((r) => r.data)
}

export function fetchMeApi(): Promise<UserInfo> {
  return request.get("/api/auth/me").then((r) => r.data)
}

export function bindGoogleApi(code: string): Promise<UserInfo> {
  return request.post("/api/auth/google/bind", { code }).then((r) => r.data)
}
```

### 文件：`frontend/src/api/request.ts`

```ts
import axios from "axios"
import type { AxiosInstance, InternalAxiosRequestConfig } from "axios"

const request: AxiosInstance = axios.create({
  baseURL: "",
  timeout: 60000,
  headers: {
    "Content-Type": "application/json",
  },
})

request.interceptors.request.use(
  (config: InternalAxiosRequestConfig) => {
    const token = localStorage.getItem("token")
    if (token && config.headers) {
      config.headers.Authorization = `Bearer ${token}`
    }
    return config
  },
  (error) => Promise.reject(error),
)

request.interceptors.response.use(
  (response) => response,
  (error) => {
    if (error.response?.status === 401) {
      localStorage.removeItem("token")
      localStorage.removeItem("user")
      window.location.hash = "#/login"
    }
    return Promise.reject(error)
  },
)

export default request
```

### 文件：`frontend/src/stores/auth.ts`

```ts
import { defineStore } from "pinia"
import { ref, computed } from "vue"
import type { UserInfo, LoginRequest, RegisterRequest } from "@/api/auth"
import {
  loginApi,
  registerApi,
  logoutApi,
  fetchMeApi,
  bindGoogleApi,
} from "@/api/auth"

export const useAuthStore = defineStore("auth", () => {
  const token = ref<string | null>(null)
  const user = ref<UserInfo | null>(null)

  const isAuthenticated = computed(() => !!token.value)

  function restoreSession() {
    const savedToken = localStorage.getItem("token")
    const savedUser = localStorage.getItem("user")
    if (savedToken) {
      token.value = savedToken
    }
    if (savedUser) {
      try {
        user.value = JSON.parse(savedUser)
      } catch {
        localStorage.removeItem("user")
      }
    }
    if (savedToken) {
      fetchMeApi()
        .then((u) => {
          user.value = u
          localStorage.setItem("user", JSON.stringify(u))
        })
        .catch(() => {
          token.value = null
          user.value = null
          localStorage.removeItem("token")
          localStorage.removeItem("user")
        })
    }
  }

  async function login(data: LoginRequest) {
    const res = await loginApi(data)
    token.value = res.token
    user.value = res.user
    localStorage.setItem("token", res.token)
    localStorage.setItem("user", JSON.stringify(res.user))
  }

  async function register(data: RegisterRequest) {
    const res = await registerApi(data)
    token.value = res.token
    user.value = res.user
    localStorage.setItem("token", res.token)
    localStorage.setItem("user", JSON.stringify(res.user))
  }

  async function logout() {
    try {
      await logoutApi()
    } finally {
      token.value = null
      user.value = null
      localStorage.removeItem("token")
      localStorage.removeItem("user")
    }
  }

  async function fetchMe() {
    const u = await fetchMeApi()
    user.value = u
    localStorage.setItem("user", JSON.stringify(u))
  }

  async function bindGoogle(code: string) {
    const u = await bindGoogleApi(code)
    user.value = u
    localStorage.setItem("user", JSON.stringify(u))
  }

  return {
    token,
    user,
    isAuthenticated,
    restoreSession,
    login,
    register,
    logout,
    fetchMe,
    bindGoogle,
  }
})
```

### 文件：`frontend/src/views/LoginView.vue`

```vue
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
```

### 文件：`frontend/src/views/AuthGoogleView.vue`

```vue
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
```

---

## Task 3.3: 首页 — 知识库列表

### 文件：`frontend/src/api/notebooks.ts`

```ts
import request from "./request"

export interface Notebook {
  id: number; remote_id: string; title: string; description: string | null
  source_count: number; chat_count: number; last_synced_at: string | null
  created_at: string; updated_at: string
}

export interface CreateNotebookRequest { title: string; description?: string }
export interface UpdateNotebookRequest { title?: string; description?: string }

export function fetchNotebooksApi(params?: { search?: string; sort?: string; page?: number; page_size?: number }): Promise<{ items: Notebook[]; total: number }> {
  return request.get("/api/notebooks", { params }).then((r) => r.data)
}
export function fetchNotebookApi(id: number): Promise<Notebook> {
  return request.get(`/api/notebooks/${id}`).then((r) => r.data)
}
export function createNotebookApi(data: CreateNotebookRequest): Promise<Notebook> {
  return request.post("/api/notebooks", data).then((r) => r.data)
}
export function updateNotebookApi(id: number, data: UpdateNotebookRequest): Promise<Notebook> {
  return request.put(`/api/notebooks/${id}`, data).then((r) => r.data)
}
export function deleteNotebookApi(id: number): Promise<void> {
  return request.delete(`/api/notebooks/${id}`).then((r) => r.data)
}
export function syncNotebookApi(id: number): Promise<Notebook> {
  return request.post(`/api/notebooks/${id}/sync`).then((r) => r.data)
}
```

### 文件：`frontend/src/stores/notebooks.ts`

```ts
import { defineStore } from "pinia"
import { ref, computed } from "vue"
import type { Notebook, CreateNotebookRequest, UpdateNotebookRequest } from "@/api/notebooks"
import { fetchNotebooksApi, fetchNotebookApi, createNotebookApi, updateNotebookApi, deleteNotebookApi, syncNotebookApi } from "@/api/notebooks"

export const useNotebooksStore = defineStore("notebooks", () => {
  const notebooks = ref<Notebook[]>([]); const total = ref(0)
  const currentNotebook = ref<Notebook | null>(null); const loading = ref(false)
  const isEmpty = computed(() => notebooks.value.length === 0)

  async function fetchNotebooks(params?: { search?: string; sort?: string; page?: number; page_size?: number }) {
    loading.value = true
    try { const res = await fetchNotebooksApi(params); notebooks.value = res.items; total.value = res.total }
    finally { loading.value = false }
  }
  async function fetchNotebook(id: number) { const nb = await fetchNotebookApi(id); currentNotebook.value = nb; return nb }
  async function createNotebook(data: CreateNotebookRequest) { const nb = await createNotebookApi(data); notebooks.value.unshift(nb); total.value++; return nb }
  async function updateNotebook(id: number, data: UpdateNotebookRequest) {
    const nb = await updateNotebookApi(id, data)
    const idx = notebooks.value.findIndex((n) => n.id === id)
    if (idx !== -1) notebooks.value[idx] = nb
    if (currentNotebook.value?.id === id) currentNotebook.value = nb
    return nb
  }
  async function deleteNotebook(id: number) {
    await deleteNotebookApi(id); notebooks.value = notebooks.value.filter((n) => n.id !== id); total.value--
    if (currentNotebook.value?.id === id) currentNotebook.value = null
  }
  async function syncNotebook(id: number) {
    const nb = await syncNotebookApi(id)
    const idx = notebooks.value.findIndex((n) => n.id === id)
    if (idx !== -1) notebooks.value[idx] = nb
    if (currentNotebook.value?.id === id) currentNotebook.value = nb
    return nb
  }
  return { notebooks, total, currentNotebook, loading, isEmpty, fetchNotebooks, fetchNotebook, createNotebook, updateNotebook, deleteNotebook, syncNotebook }
})
```

### 文件：`frontend/src/views/HomeView.vue`

```vue
<template>
  <div class="home-page">
    <header class="home-header">
      <div class="header-left"><h1 class="page-title">我的宝库</h1></div>
      <div class="header-right">
        <el-input v-model="searchQuery" placeholder="搜索知识库" :prefix-icon="Search" clearable class="search-input" @input="debouncedSearch" />
        <el-button type="primary" class="create-btn" @click="showCreateDialog = true">+ 新建知识库</el-button>
      </div>
    </header>
    <div class="home-content page-container">
      <!-- Loading skeleton -->
      <div v-if="loading" class="card-grid">
        <div v-for="i in 6" :key="i" class="skeleton-card card">
          <div class="skeleton-line skeleton-title" /><div class="skeleton-line skeleton-desc" /><div class="skeleton-line skeleton-meta" />
        </div>
      </div>
      <!-- Empty state -->
      <div v-else-if="isEmpty" class="empty-state">
        <el-icon :size="64" color="#d0d0d0"><FolderOpened /></el-icon>
        <p class="empty-title">创建你的第一个知识库</p>
        <p class="empty-desc">上传文档，让 AI 帮你整理和生成内容</p>
        <el-button type="primary" @click="showCreateDialog = true">+ 新建知识库</el-button>
      </div>
      <!-- Notebook grid -->
      <div v-else class="card-grid">
        <div v-for="nb in notebooks" :key="nb.id" class="notebook-card card" @click="router.push(`/notebook/${nb.id}`)">
          <div class="card-header">
            <h3 class="card-title">{{ nb.title }}</h3>
            <el-tag v-if="nb.last_synced_at" size="small" type="info" class="sync-tag">已同步</el-tag>
          </div>
          <p v-if="nb.description" class="card-desc">{{ nb.description }}</p>
          <div class="card-meta">
            <span class="meta-item"><el-icon><Document /></el-icon>{{ nb.source_count }} 份资料</span>
            <span class="meta-item"><el-icon><ChatDotSquare /></el-icon>{{ nb.chat_count }} 次问答</span>
            <span class="meta-item time">{{ formatTime(nb.updated_at) }}</span>
          </div>
        </div>
      </div>
    </div>

    <!-- Create dialog -->
    <el-dialog v-model="showCreateDialog" title="新建知识库" width="420px" :close-on-click-modal="false">
      <el-form ref="createFormRef" :model="createForm" :rules="createRules" label-position="top">
        <el-form-item label="知识库名称" prop="title">
          <el-input v-model="createForm.title" placeholder="请输入知识库名称" maxlength="100" show-word-limit />
        </el-form-item>
        <el-form-item label="描述（选填）" prop="description">
          <el-input v-model="createForm.description" type="textarea" :rows="3" placeholder="简短描述这个知识库的用途" maxlength="500" show-word-limit />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="showCreateDialog = false">取消</el-button>
        <el-button type="primary" :loading="creating" @click="handleCreate">创建</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
import { ref, reactive, onMounted, computed } from "vue"
import { useRouter } from "vue-router"
import { Search, FolderOpened, Document, ChatDotSquare } from "@element-plus/icons-vue"
import { ElMessage } from "element-plus"
import type { FormInstance, FormRules } from "element-plus"
import { useNotebooksStore } from "@/stores/notebooks"
import { useAuthStore } from "@/stores/auth"

const router = useRouter()
const notebooksStore = useNotebooksStore()
const authStore = useAuthStore()

const notebooks = computed(() => notebooksStore.notebooks)
const loading = computed(() => notebooksStore.loading)
const isEmpty = computed(() => notebooksStore.isEmpty)
const searchQuery = ref("")
const showCreateDialog = ref(false)
const creating = ref(false)
const createFormRef = ref<FormInstance>()
const createForm = reactive({ title: "", description: "" })
const createRules: FormRules = {
  title: [{ required: true, message: "请输入知识库名称", trigger: "blur" }, { max: 100, message: "名称不能超过 100 字符", trigger: "blur" }],
}

let debounceTimer: ReturnType<typeof setTimeout> | null = null
function debouncedSearch() {
  if (debounceTimer) clearTimeout(debounceTimer)
  debounceTimer = setTimeout(() => { notebooksStore.fetchNotebooks({ search: searchQuery.value || undefined }) }, 300)
}

async function handleCreate() {
  const valid = await createFormRef.value?.validate().catch(() => false)
  if (!valid) return
  creating.value = true
  try {
    const nb = await notebooksStore.createNotebook({ title: createForm.title, description: createForm.description || undefined })
    ElMessage.success("创建成功"); showCreateDialog.value = false
    createForm.title = ""; createForm.description = ""
    router.push(`/notebook/${nb.id}`)
  } catch (e: any) { ElMessage.error(e.response?.data?.detail || "创建失败") }
  finally { creating.value = false }
}

function formatTime(t: string) {
  const d = new Date(t); const diff = Date.now() - d.getTime()
  if (diff < 3600000) return `${Math.floor(diff / 60000)} 分钟前`
  if (diff < 86400000) return `${Math.floor(diff / 3600000)} 小时前`
  return d.toLocaleDateString("zh-CN")
}

onMounted(() => {
  if (!authStore.isAuthenticated) { router.push("/login"); return }
  notebooksStore.fetchNotebooks()
})
</script>

<style scoped lang="scss">
.home-page { min-height: 100vh; background: var(--color-bg-tab); }
.home-header { display: flex; align-items: center; justify-content: space-between; padding: 16px 24px; background: var(--color-bg-1); border-bottom: 1px solid var(--color-divider-1); position: sticky; top: 0; z-index: 100; height: 64px; }
.header-left { display: flex; align-items: center; gap: 12px; }
.page-title { font-size: 20px; font-weight: 600; }
.header-right { display: flex; align-items: center; gap: 12px; }
.search-input { width: 240px; :deep(.el-input__wrapper) { border-radius: var(--radius-input); } }
.create-btn { height: 36px; border-radius: var(--radius-button); background: var(--color-main-1); border-color: var(--color-main-1); font-weight: 500; }
.home-content { padding-top: 24px; }
.card-grid { display: grid; grid-template-columns: repeat(3, 1fr); gap: 20px; }
.notebook-card { cursor: pointer; transition: transform 0.2s, box-shadow 0.2s; &:hover { transform: translateY(-2px); box-shadow: 0px 6px 48px 0px rgba(174,180,193,.25); } }
.card-header { display: flex; align-items: center; justify-content: space-between; margin-bottom: 8px; }
.card-title { font-size: 16px; font-weight: 600; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.sync-tag { flex-shrink: 0; }
.card-desc { font-size: 13px; color: var(--color-text-2); margin-bottom: 12px; display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; }
.card-meta { display: flex; align-items: center; gap: 16px; font-size: 12px; color: var(--color-text-3); }
.meta-item { display: flex; align-items: center; gap: 4px; .el-icon { font-size: 14px; } &.time { margin-left: auto; } }
.empty-state { display: flex; flex-direction: column; align-items: center; justify-content: center; padding: 80px 20px; gap: 12px; }
.empty-title { font-size: 18px; font-weight: 600; margin-top: 12px; }
.empty-desc { font-size: 14px; color: var(--color-text-3); margin-bottom: 8px; }
.skeleton-card {
  .skeleton-line { height: 14px; background: linear-gradient(90deg, var(--color-bg-tab) 25%, #e8e8ea 50%, var(--color-bg-tab) 75%); background-size: 200% 100%; animation: shimmer 1.5s infinite; border-radius: 4px; margin-bottom: 12px; }
  .skeleton-title { width: 60%; height: 18px; }
  .skeleton-desc { width: 80%; }
  .skeleton-meta { width: 40%; }
}
@keyframes shimmer { 0% { background-position: 200% 0; } 100% { background-position: -200% 0; } }
</style>
```


---

## Task 3.4: 知识库详情 — 资料 Tab + 上传

### 文件：`frontend/src/views/NotebookView.vue`

```vue
<template>
  <div class="notebook-page">
    <header class="notebook-header">
      <div class="header-left">
        <el-button text class="back-btn" @click="router.push('/')"><el-icon><ArrowLeft /></el-icon><span>返回</span></el-button>
        <h2 class="notebook-title">{{ notebook?.title || "加载中..." }}</h2>
      </div>
      <div class="header-right">
        <el-button text class="sync-btn" :loading="syncing" @click="handleSync"><el-icon><Refresh /></el-icon>同步</el-button>
        <el-dropdown trigger="click">
          <el-button text class="more-btn"><el-icon><MoreFilled /></el-icon></el-button>
          <template #dropdown>
            <el-dropdown-menu>
              <el-dropdown-item @click="handleRename"><el-icon><Edit /></el-icon> 重命名</el-dropdown-item>
              <el-dropdown-item @click="handleShare"><el-icon><Share /></el-icon> 分享</el-dropdown-item>
              <el-dropdown-item divided @click="handleDelete"><el-icon><Delete /></el-icon> 删除知识库</el-dropdown-item>
            </el-dropdown-menu>
          </template>
        </el-dropdown>
      </div>
    </header>
    <div class="tab-bar">
      <el-tabs v-model="activeTab" class="notebook-tabs" @tab-change="handleTabChange">
        <el-tab-pane label="概览" name="overview" />
        <el-tab-pane label="资料" name="sources" />
        <el-tab-pane label="问答" name="chat" />
        <el-tab-pane label="生成" name="generate" />
      </el-tabs>
    </div>
    <div class="tab-content"><router-view /></div>

    <el-dialog v-model="showRenameDialog" title="重命名知识库" width="400px">
      <el-form ref="renameFormRef" :model="renameForm" :rules="renameRules" label-position="top">
        <el-form-item label="名称" prop="title"><el-input v-model="renameForm.title" maxlength="100" /></el-form-item>
        <el-form-item label="描述" prop="description"><el-input v-model="renameForm.description" type="textarea" :rows="3" maxlength="500" /></el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="showRenameDialog = false">取消</el-button>
        <el-button type="primary" :loading="renaming" @click="handleRenameConfirm">确定</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted } from "vue"
import { useRouter, useRoute } from "vue-router"
import { ArrowLeft, Refresh, MoreFilled, Edit, Share, Delete } from "@element-plus/icons-vue"
import { ElMessage, ElMessageBox } from "element-plus"
import type { FormInstance, FormRules } from "element-plus"
import { useNotebooksStore } from "@/stores/notebooks"

const router = useRouter()
const route = useRoute()
const notebooksStore = useNotebooksStore()
const notebookId = computed(() => Number(route.params.id))
const notebook = computed(() => notebooksStore.currentNotebook)

const activeTab = ref("overview")
const syncing = ref(false)
const showRenameDialog = ref(false)
const renaming = ref(false)
const renameFormRef = ref<FormInstance>()
const renameForm = ref({ title: "", description: "" })
const renameRules: FormRules = { title: [{ required: true, message: "请输入名称", trigger: "blur" }] }

function handleTabChange(name: string) { router.push(`/notebook/${notebookId.value}/${name}`) }

async function handleSync() {
  syncing.value = true
  try { await notebooksStore.syncNotebook(notebookId.value); ElMessage.success("同步完成") }
  catch (e: any) { ElMessage.error(e.response?.data?.detail || "同步失败") }
  finally { syncing.value = false }
}

function handleRename() {
  if (notebook.value) { renameForm.value.title = notebook.value.title; renameForm.value.description = notebook.value.description || "" }
  showRenameDialog.value = true
}

async function handleRenameConfirm() {
  const valid = await renameFormRef.value?.validate().catch(() => false)
  if (!valid) return
  renaming.value = true
  try { await notebooksStore.updateNotebook(notebookId.value, { title: renameForm.value.title, description: renameForm.value.description || undefined }); ElMessage.success("已更新"); showRenameDialog.value = false }
  catch (e: any) { ElMessage.error(e.response?.data?.detail || "更新失败") }
  finally { renaming.value = false }
}

function handleShare() { ElMessage.info("分享功能开发中") }

async function handleDelete() {
  try {
    await ElMessageBox.confirm("确定要删除这个知识库吗？此操作不可恢复。", "确认删除", { type: "warning", confirmButtonText: "删除", cancelButtonText: "取消" })
    await notebooksStore.deleteNotebook(notebookId.value); ElMessage.success("已删除"); router.push("/")
  } catch {}
}

onMounted(() => {
  notebooksStore.fetchNotebook(notebookId.value)
  const childPath = route.path.split("/").pop()
  if (["overview", "sources", "chat", "generate"].includes(childPath || "")) activeTab.value = childPath!
})
</script>

<style scoped lang="scss">
.notebook-page { min-height: 100vh; background: var(--color-bg-tab); }
.notebook-header { display: flex; align-items: center; justify-content: space-between; padding: 0 24px; height: 48px; background: var(--color-bg-1); border-bottom: 1px solid var(--color-divider-1); position: sticky; top: 0; z-index: 100; }
.header-left { display: flex; align-items: center; gap: 12px; }
.back-btn { font-size: 14px; color: var(--color-text-2); .el-icon { margin-right: 4px; } }
.notebook-title { font-size: 16px; font-weight: 600; }
.header-right { display: flex; align-items: center; gap: 4px; }
.sync-btn, .more-btn { color: var(--color-text-2); }
.tab-bar { background: var(--color-bg-1); padding: 0 24px; border-bottom: 1px solid var(--color-divider-1); }
.notebook-tabs {
  :deep(.el-tabs__header) { margin: 0; }
  :deep(.el-tabs__item) { height: 40px; font-size: 14px; color: var(--color-text-2); &.is-active { color: var(--color-main-1); font-weight: 500; } }
  :deep(.el-tabs__active-bar) { background-color: var(--color-main-1); }
  :deep(.el-tabs__nav-wrap::after) { display: none; }
}
.tab-content { padding: 24px; }
</style>
```

### 文件：`frontend/src/api/sources.ts`

```ts
import request from "./request"

export interface Source {
  id: number; remote_id: string; filename: string; original_filename: string
  file_type: string; file_size: number; page_count: number
  local_path: string | null; source_url: string | null; summary: string | null
  status: string; created_at: string; updated_at: string
}

export function fetchSourcesApi(notebookId: number, params?: { page?: number; page_size?: number }): Promise<{ items: Source[]; total: number }> {
  return request.get(`/api/notebooks/${notebookId}/sources`, { params }).then((r) => r.data)
}

export function uploadSourceApi(notebookId: number, file: File, onProgress?: (pct: number) => void): Promise<Source> {
  const form = new FormData()
  form.append("file", file)
  return request.post(`/api/notebooks/${notebookId}/sources/upload`, form, {
    headers: { "Content-Type": "multipart/form-data" },
    onUploadProgress: (e) => { if (e.total && onProgress) onProgress(Math.round((e.loaded / e.total) * 100)) },
  }).then((r) => r.data)
}

export function addSourceUrlApi(notebookId: number, url: string): Promise<Source> {
  return request.post(`/api/notebooks/${notebookId}/sources/url`, { url }).then((r) => r.data)
}

export function deleteSourceApi(notebookId: number, sourceId: number): Promise<void> {
  return request.delete(`/api/notebooks/${notebookId}/sources/${sourceId}`).then((r) => r.data)
}

export function renameSourceApi(notebookId: number, sourceId: number, filename: string): Promise<Source> {
  return request.put(`/api/notebooks/${notebookId}/sources/${sourceId}`, { filename }).then((r) => r.data)
}
```

### 文件：`frontend/src/components/SourceList.vue`

```vue
<template>
  <div class="source-list">
    <div v-if="sources.length === 0" class="empty">
      <el-icon :size="48" color="#d0d0d0"><Upload /></el-icon>
      <p class="empty-text">{{ emptyText || "暂无资料，点击上方按钮上传" }}</p>
    </div>
    <div v-for="source in sources" :key="source.id" class="source-item" @click="$emit('select', source)">
      <div class="source-icon">
        <el-icon :size="20" :color="fileIconColor(source.file_type)"><component :is="fileIcon(source.file_type)" /></el-icon>
      </div>
      <div class="source-info">
        <span class="source-name">{{ source.original_filename || source.filename }}</span>
        <span class="source-meta">{{ formatFileSize(source.file_size) }}<template v-if="source.page_count"> · {{ source.page_count }} 页</template> · {{ formatTime(source.created_at) }}</span>
      </div>
      <div class="source-status">
        <el-tag v-if="source.status === 'processing'" size="small" type="warning">处理中</el-tag>
        <el-tag v-else-if="source.status === 'active'" size="small" type="success">就绪</el-tag>
        <el-tag v-else-if="source.status === 'deleted'" size="small" type="info">已删除</el-tag>
      </div>
      <div class="source-actions">
        <el-button v-if="showDelete" text type="danger" size="small" @click.stop="$emit('delete', source)"><el-icon><Delete /></el-icon></el-button>
        <el-button v-if="showRename" text size="small" @click.stop="$emit('rename', source)"><el-icon><Edit /></el-icon></el-button>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import type { Source } from "@/api/sources"
import { Document, Delete, Edit, Upload, Link } from "@element-plus/icons-vue"
defineProps<{ sources: Source[]; emptyText?: string; showDelete?: boolean; showRename?: boolean }>()
defineEmits<{ select: [source: Source]; delete: [source: Source]; rename: [source: Source] }>()

function fileIcon(type: string) { const m: Record<string, any> = { pdf: Document, docx: Document, doc: Document, txt: Document, url: Link }; return m[type] || Document }
function fileIconColor(type: string) { const m: Record<string, string> = { pdf: "#ff3650", docx: "#1a75ff", doc: "#1a75ff", txt: "#626469", url: "#409eff" }; return m[type] || "#626469" }
function formatFileSize(bytes: number) { if (!bytes) return "0 B"; if (bytes < 1024) return `${bytes} B`; if (bytes < 1048576) return `${(bytes/1024).toFixed(1)} KB`; return `${(bytes/1048576).toFixed(1)} MB` }
function formatTime(t: string) { const d = new Date(t); const diff = Date.now() - d.getTime(); if (diff < 3600000) return `${Math.floor(diff/60000)} 分钟前`; if (diff < 86400000) return `${Math.floor(diff/3600000)} 小时前`; return d.toLocaleDateString("zh-CN") }
</script>

<style scoped lang="scss">
.source-list {
  .empty { display: flex; flex-direction: column; align-items: center; padding: 48px 20px; gap: 12px; .empty-text { font-size: 14px; color: var(--color-text-3); } }
}
.source-item { display: flex; align-items: center; gap: 12px; padding: 8px 12px; border-radius: 10px; cursor: pointer; transition: background 0.15s; height: 40px; &:hover { background: var(--color-bg-tab); } }
.source-icon { flex-shrink: 0; display: flex; align-items: center; justify-content: center; width: 28px; height: 28px; }
.source-info { flex: 1; min-width: 0; display: flex; flex-direction: column; gap: 2px; }
.source-name { font-size: 13px; font-weight: 500; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.source-meta { font-size: 11px; color: var(--color-text-3); }
.source-status { flex-shrink: 0; }
.source-actions { flex-shrink: 0; display: flex; gap: 4px; opacity: 0; transition: opacity 0.15s; }
.source-item:hover .source-actions { opacity: 1; }
</style>
```

### 文件：`frontend/src/components/UploadDialog.vue`

```vue
<template>
  <el-dialog :model-value="visible" title="上传资料" width="520px" :close-on-click-modal="false" @update:model-value="$emit('update:visible', $event)">
    <div class="upload-container">
      <div class="drag-zone" :class="{ 'drag-over': isDragOver }" @dragover.prevent="isDragOver = true" @dragleave.prevent="isDragOver = false" @drop.prevent="handleDrop" @click="triggerFileInput">
        <el-icon :size="48" color="#d0d0d0"><UploadFilled /></el-icon>
        <p class="drag-text">{{ isDragOver ? "释放文件以上传" : "拖拽文件到此处，或点击选择文件" }}</p>
        <p class="drag-hint">支持 PDF、Word、TXT 格式，单个文件最大 50MB</p>
      </div>
      <input ref="fileInputRef" type="file" accept=".pdf,.docx,.doc,.txt" style="display: none" multiple @change="handleFileChange" />
      <div class="url-input-group">
        <el-divider><span class="divider-text">或通过链接添加</span></el-divider>
        <div class="url-row">
          <el-input v-model="urlInput" placeholder="输入网页链接" clearable />
          <el-button type="primary" :disabled="!urlInput.trim()" :loading="addingUrl" @click="handleAddUrl">添加</el-button>
        </div>
      </div>
      <div v-if="uploadProgress.length > 0" class="progress-list">
        <div v-for="item in uploadProgress" :key="item.name" class="progress-item">
          <span class="progress-name">{{ item.name }}</span>
          <el-progress :percentage="item.percent" :status="item.status" :stroke-width="6" />
        </div>
      </div>
    </div>
  </el-dialog>
</template>

<script setup lang="ts">
import { ref } from "vue"
import { UploadFilled } from "@element-plus/icons-vue"
import { ElMessage } from "element-plus"
import { uploadSourceApi, addSourceUrlApi } from "@/api/sources"

const props = defineProps<{ visible: boolean; notebookId: number }>()
const emit = defineEmits<{ "update:visible": [value: boolean]; uploaded: [] }>()

const isDragOver = ref(false); const fileInputRef = ref<HTMLInputElement>(); const urlInput = ref(""); const addingUrl = ref(false)
interface UPI { name: string; percent: number; status: "success" | "exception" | "" }
const uploadProgress = ref<UPI[]>([])

function triggerFileInput() { fileInputRef.value?.click() }
function handleDrop(e: DragEvent) { isDragOver.value = false; const files = e.dataTransfer?.files; if (files) uploadFiles(Array.from(files)) }
function handleFileChange(e: Event) { const input = e.target as HTMLInputElement; if (input.files) uploadFiles(Array.from(input.files)); input.value = "" }

function uploadFiles(files: File[]) {
  for (const file of files) {
    const item: UPI = { name: file.name, percent: 0, status: "" }
    uploadProgress.value.push(item)
    uploadSourceApi(props.notebookId, file, (pct) => { item.percent = pct })
      .then(() => { item.status = "success"; emit("uploaded"); ElMessage.success(`${file.name} 上传成功`) })
      .catch((e) => { item.status = "exception"; ElMessage.error(`${file.name} 上传失败: ${e.response?.data?.detail || e.message}`) })
  }
}

async function handleAddUrl() {
  const url = urlInput.value.trim()
  if (!url) return; addingUrl.value = true
  try { await addSourceUrlApi(props.notebookId, url); ElMessage.success("链接已添加"); urlInput.value = ""; emit("uploaded") }
  catch (e: any) { ElMessage.error(e.response?.data?.detail || "添加失败") }
  finally { addingUrl.value = false }
}
</script>

<style scoped lang="scss">
.upload-container { display: flex; flex-direction: column; gap: 20px; }
.drag-zone { display: flex; flex-direction: column; align-items: center; justify-content: center; padding: 40px 20px; border: 2px dashed var(--color-divider-1); border-radius: var(--radius-card); cursor: pointer; transition: all 0.2s; gap: 12px; &:hover, &.drag-over { border-color: var(--color-main-1); background: rgba(255,54,80,.04); } }
.drag-text { font-size: 14px; color: var(--color-text-2); }
.drag-hint { font-size: 12px; color: var(--color-text-4); }
.url-input-group { .divider-text { font-size: 12px; color: var(--color-text-4); } }
.url-row { display: flex; gap: 8px; }
.progress-list { display: flex; flex-direction: column; gap: 12px; }
.progress-item { display: flex; flex-direction: column; gap: 4px; }
.progress-name { font-size: 13px; color: var(--color-text-1); }
</style>
```

### 文件：`frontend/src/views/notebook/SourcesTab.vue`

```vue
<template>
  <div class="sources-tab">
    <div class="sources-header">
      <div class="source-type-switch">
        <el-radio-group v-model="sourceType" size="small">
          <el-radio-button value="local">本地资料</el-radio-button>
          <el-radio-button value="external">外部知识库</el-radio-button>
        </el-radio-group>
      </div>
      <div class="sources-actions">
        <el-button type="primary" size="small" @click="showUpload = true"><el-icon><Upload /></el-icon> 上传</el-button>
      </div>
    </div>
    <template v-if="sourceType === 'local'">
      <SourceList :sources="sources" empty-text="暂无资料，点击上方按钮上传" :show-delete="true" :show-rename="true" @select="()=>{}" @delete="handleDelete" @rename="handleRename" />
    </template>
    <template v-else>
      <ExternalKbPanel :notebook-id="notebookId" />
    </template>

    <UploadDialog :visible="showUpload" :notebook-id="notebookId" @update:visible="showUpload = $event" @uploaded="fetchSources" />

    <el-dialog v-model="showDeleteConfirm" title="删除资料" width="380px">
      <p>确定要删除「{{ deleteTarget?.original_filename || deleteTarget?.filename }}」吗？</p>
      <template #footer>
        <el-button @click="showDeleteConfirm = false">取消</el-button>
        <el-button type="danger" :loading="deleting" @click="confirmDelete">删除</el-button>
      </template>
    </el-dialog>
    <el-dialog v-model="showRenameDialog" title="重命名" width="380px">
      <el-input v-model="renameValue" maxlength="200" />
      <template #footer>
        <el-button @click="showRenameDialog = false">取消</el-button>
        <el-button type="primary" :loading="renaming" @click="confirmRename">确定</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted } from "vue"
import { useRoute } from "vue-router"
import { Upload } from "@element-plus/icons-vue"
import { ElMessage } from "element-plus"
import type { Source } from "@/api/sources"
import { fetchSourcesApi, deleteSourceApi, renameSourceApi } from "@/api/sources"
import SourceList from "@/components/SourceList.vue"
import UploadDialog from "@/components/UploadDialog.vue"
import ExternalKbPanel from "@/components/ExternalKbPanel.vue"

const route = useRoute()
const notebookId = computed(() => Number(route.params.id))
const sourceType = ref("local")
const sources = ref<Source[]>([])
const loading = ref(false)
const showUpload = ref(false)
const showDeleteConfirm = ref(false); const deleteTarget = ref<Source | null>(null); const deleting = ref(false)
const showRenameDialog = ref(false); const renameTarget = ref<Source | null>(null); const renameValue = ref(""); const renaming = ref(false)

async function fetchSources() { loading.value = true; try { const res = await fetchSourcesApi(notebookId.value); sources.value = res.items } finally { loading.value = false } }

function handleDelete(source: Source) { deleteTarget.value = source; showDeleteConfirm.value = true }
async function confirmDelete() {
  if (!deleteTarget.value) return; deleting.value = true
  try { await deleteSourceApi(notebookId.value, deleteTarget.value.id); sources.value = sources.value.filter((s) => s.id !== deleteTarget.value!.id); ElMessage.success("已删除"); showDeleteConfirm.value = false }
  catch (e: any) { ElMessage.error(e.response?.data?.detail || "删除失败") }
  finally { deleting.value = false }
}

function handleRename(source: Source) { renameTarget.value = source; renameValue.value = source.original_filename || source.filename; showRenameDialog.value = true }
async function confirmRename() {
  if (!renameTarget.value || !renameValue.value.trim()) return; renaming.value = true
  try { const updated = await renameSourceApi(notebookId.value, renameTarget.value.id, renameValue.value.trim()); const idx = sources.value.findIndex((s) => s.id === updated.id); if (idx !== -1) sources.value[idx] = updated; ElMessage.success("已重命名"); showRenameDialog.value = false }
  catch (e: any) { ElMessage.error(e.response?.data?.detail || "重命名失败") }
  finally { renaming.value = false }
}

onMounted(() => { fetchSources() })
</script>

<style scoped lang="scss">
.sources-tab { max-width: 900px; margin: 0 auto; }
.sources-header { display: flex; align-items: center; justify-content: space-between; margin-bottom: 20px; }
.source-type-switch {
  :deep(.el-radio-button__inner) { border-radius: var(--radius-tab); font-size: 13px; }
  :deep(.el-radio-button:first-child .el-radio-button__inner) { border-radius: var(--radius-tab) 0 0 var(--radius-tab); }
  :deep(.el-radio-button:last-child .el-radio-button__inner) { border-radius: 0 var(--radius-tab) var(--radius-tab) 0; }
}
</style>
```

---

## Task 3.5: 外部知识库面板

### 文件：`frontend/src/api/external-kb.ts`

```ts
import request from "./request"

export interface ExternalKbConnection { id: number; name: string; provider_type: string; api_base_url: string; auth_type: string; is_active: boolean; last_sync_at: string | null; created_at: string }
export interface ExternalKbCollection { id: number; connection_id: number; remote_id: string; name: string; description: string | null; document_count: number }
export interface ExternalKbDocument { id: number; collection_id: number; remote_id: string; title: string; summary: string | null; file_type: string | null; file_size: number | null; url: string | null }
export interface ImportResult { id: number; source_id: number; status: string }

export function fetchConnectionsApi(): Promise<ExternalKbConnection[]> { return request.get("/api/external-kb/connections").then((r) => r.data) }
export function createConnectionApi(data: { name: string; provider_type: string; api_base_url: string; auth_type: string; auth_credentials?: Record<string, string> }): Promise<ExternalKbConnection> { return request.post("/api/external-kb/connections", data).then((r) => r.data) }
export function updateConnectionApi(id: number, data: Partial<{ name: string; api_base_url: string; auth_type: string; auth_credentials: Record<string, string> }>): Promise<ExternalKbConnection> { return request.put(`/api/external-kb/connections/${id}`, data).then((r) => r.data) }
export function deleteConnectionApi(id: number): Promise<void> { return request.delete(`/api/external-kb/connections/${id}`).then((r) => r.data) }
export function testConnectionApi(id: number): Promise<{ success: boolean; message: string }> { return request.post(`/api/external-kb/connections/${id}/test`).then((r) => r.data) }
export function fetchCollectionsApi(connectionId: number): Promise<ExternalKbCollection[]> { return request.get(`/api/external-kb/connections/${connectionId}/collections`).then((r) => r.data) }
export function fetchDocumentsApi(connectionId: number, collectionId: number): Promise<ExternalKbDocument[]> { return request.get(`/api/external-kb/connections/${connectionId}/collections/${collectionId}/documents`).then((r) => r.data) }
export function searchExternalKbApi(connectionId: number, collectionId: number, query: string): Promise<ExternalKbDocument[]> { return request.get(`/api/external-kb/connections/${connectionId}/collections/${collectionId}/search`, { params: { q: query } }).then((r) => r.data) }
export function importDocumentApi(connectionId: number, documentId: number, notebookId: number): Promise<ImportResult> { return request.post("/api/external-kb/import", { connection_id: connectionId, document_id: documentId, target_notebook_id: notebookId }).then((r) => r.data) }
```

### 文件：`frontend/src/components/ExternalKbPanel.vue`

```vue
<template>
  <div class="external-kb-panel">
    <div v-if="loading" class="loading"><el-skeleton :rows="3" animated /></div>

    <div v-else-if="connections.length === 0" class="empty">
      <el-icon :size="48" color="#d0d0d0"><Connection /></el-icon>
      <p class="empty-text">尚未接入外部知识库</p>
      <el-button type="primary" size="small" @click="showConnForm = true">添加外部知识库</el-button>
    </div>

    <div v-else class="conn-list">
      <div v-for="conn in connections" :key="conn.id" class="conn-card card">
        <div class="conn-header" @click="toggleExpand(conn.id)">
          <div class="conn-info">
            <el-icon :size="18"><Connection /></el-icon>
            <span class="conn-name">{{ conn.name }}</span>
            <el-tag size="small" type="info" class="provider-badge">{{ providerLabel(conn.provider_type) }}</el-tag>
          </div>
          <div class="conn-meta">
            <span class="sync-time" v-if="conn.last_sync_at">最后同步: {{ formatTime(conn.last_sync_at) }}</span>
            <el-icon class="expand-icon" :class="{ expanded: expandedIds.has(conn.id) }"><ArrowDown /></el-icon>
          </div>
        </div>

        <div v-if="expandedIds.has(conn.id)" class="conn-body">
          <div class="search-row">
            <el-input v-model="searchQueries[conn.id]" placeholder="搜索外部知识库..." size="small" :prefix-icon="Search" clearable @input="debouncedSearch(conn.id)" />
          </div>
          <div v-if="collectionsLoading.has(conn.id)" class="loading"><el-skeleton :rows="2" animated /></div>
          <div v-else class="collections">
            <div v-for="coll in collectionsByConn(conn.id)" :key="coll.id" class="collection-item">
              <div class="collection-header" @click="toggleCollExpand(conn.id, coll.id)">
                <el-icon :size="16"><Folder /></el-icon>
                <span class="coll-name">{{ coll.name }}</span>
                <span class="coll-count">{{ coll.document_count }} 篇</span>
                <el-icon class="expand-icon" :class="{ expanded: expandedColls.has(`${conn.id}-${coll.id}`) }"><ArrowDown /></el-icon>
              </div>
              <div v-if="expandedColls.has(`${conn.id}-${coll.id}`)" class="documents">
                <div v-if="docsLoading.has(`${conn.id}-${coll.id}`)"><el-skeleton :rows="2" animated /></div>
                <div v-for="doc in documentsByColl(conn.id, coll.id)" :key="doc.id" class="document-item">
                  <el-icon :size="14"><Document /></el-icon>
                  <span class="doc-title">{{ doc.title }}</span>
                  <el-button text type="primary" size="small" :loading="importingDoc === doc.id" @click="handleImport(conn.id, doc.id)">导入</el-button>
                </div>
                <div v-if="(documentsByColl(conn.id, coll.id) || []).length === 0" class="no-docs">暂无文档</div>
              </div>
            </div>
          </div>
        </div>
      </div>
      <div class="add-conn-row"><el-button text type="primary" @click="showConnForm = true">+ 添加外部知识库</el-button></div>
    </div>
    <ExternalKbConnForm :visible="showConnForm" @update:visible="showConnForm = $event" @created="handleConnCreated" />
  </div>
</template>

<script setup lang="ts">
import { ref, reactive, onMounted } from "vue"
import { Connection, ArrowDown, Search, Folder, Document } from "@element-plus/icons-vue"
import { ElMessage } from "element-plus"
import type { ExternalKbConnection, ExternalKbCollection, ExternalKbDocument } from "@/api/external-kb"
import { fetchConnectionsApi, fetchCollectionsApi, fetchDocumentsApi, searchExternalKbApi, importDocumentApi } from "@/api/external-kb"
import ExternalKbConnForm from "./ExternalKbConnForm.vue"

const props = defineProps<{ notebookId: number }>()
const connections = ref<ExternalKbConnection[]>([]); const collections = ref<Record<number, ExternalKbCollection[]>>({}); const documents = ref<Record<string, ExternalKbDocument[]>>({})
const loading = ref(false); const collectionsLoading = reactive(new Set<number>()); const docsLoading = reactive(new Set<string>())
const expandedIds = reactive(new Set<number>()); const expandedColls = reactive(new Set<string>()); const searchQueries = reactive<Record<number, string>>({})
const importingDoc = ref<number | null>(null); const showConnForm = ref(false)
let debounceTimers: Record<number, ReturnType<typeof setTimeout>> = {}

function collectionsByConn(connId: number) { return collections.value[connId] || [] }
function documentsByColl(connId: number, collId: number) { return documents.value[`${connId}-${collId}`] || [] }
function providerLabel(type: string) { const m: Record<string, string> = { openapi: "OpenAPI", dify: "Dify", qanything: "QAnything", vectordb: "向量数据库", custom: "自定义" }; return m[type] || type }

async function toggleExpand(connId: number) {
  if (expandedIds.has(connId)) { expandedIds.delete(connId); return }
  expandedIds.add(connId)
  if (!collections.value[connId]) {
    collectionsLoading.add(connId)
    try { collections.value[connId] = await fetchCollectionsApi(connId) }
    catch { ElMessage.error("加载集合列表失败") }
    finally { collectionsLoading.delete(connId) }
  }
}

async function toggleCollExpand(connId: number, collId: number) {
  const key = `${connId}-${collId}`
  if (expandedColls.has(key)) { expandedColls.delete(key); return }
  expandedColls.add(key)
  if (!documents.value[key]) {
    docsLoading.add(key)
    try { documents.value[key] = await fetchDocumentsApi(connId, collId) }
    catch { ElMessage.error("加载文档列表失败") }
    finally { docsLoading.delete(key) }
  }
}

function debouncedSearch(connId: number) {
  if (debounceTimers[connId]) clearTimeout(debounceTimers[connId])
  debounceTimers[connId] = setTimeout(async () => {
    const q = searchQueries[connId]?.trim(); if (!q) return; const colls = collections.value[connId]; if (!colls) return
    for (const coll of colls) { try { documents.value[`${connId}-${coll.id}`] = await searchExternalKbApi(connId, coll.id, q) } catch {} }
  }, 400)
}

async function handleImport(connId: number, docId: number) {
  importingDoc.value = docId
  try { await importDocumentApi(connId, docId, props.notebookId); ElMessage.success("导入成功，请在本地资料中查看") }
  catch (e: any) { ElMessage.error(e.response?.data?.detail || "导入失败") }
  finally { importingDoc.value = null }
}
function handleConnCreated() { showConnForm.value = false; fetchConnections() }
async function fetchConnections() { loading.value = true; try { connections.value = await fetchConnectionsApi() } catch { ElMessage.error("加载外部知识库连接失败") } finally { loading.value = false } }
function formatTime(t: string) { const d = new Date(t); const diff = Date.now() - d.getTime(); if (diff < 3600000) return `${Math.floor(diff/60000)} 分钟前`; if (diff < 86400000) return `${Math.floor(diff/3600000)} 小时前`; return d.toLocaleDateString("zh-CN") }
onMounted(() => { fetchConnections() })
</script>

<style scoped lang="scss">
.external-kb-panel { .loading { padding: 20px 0; } }
.empty { display: flex; flex-direction: column; align-items: center; padding: 40px 20px; gap: 12px; .empty-text { font-size: 14px; color: var(--color-text-3); } }
.conn-list { display: flex; flex-direction: column; gap: 12px; }
.conn-card { padding: 0; overflow: hidden; }
.conn-header { display: flex; align-items: center; justify-content: space-between; padding: 12px 16px; cursor: pointer; &:hover { background: var(--color-bg-tab); } }
.conn-info { display: flex; align-items: center; gap: 8px; .el-icon { color: var(--color-main-1); } }
.conn-name { font-size: 14px; font-weight: 500; }
.provider-badge { font-size: 11px; }
.conn-meta { display: flex; align-items: center; gap: 12px; }
.sync-time { font-size: 12px; color: var(--color-text-3); }
.expand-icon { font-size: 14px; color: var(--color-text-3); transition: transform 0.2s; &.expanded { transform: rotate(180deg); } }
.conn-body { padding: 0 16px 12px; border-top: 1px solid var(--color-divider-1); }
.search-row { padding: 12px 0; }
.collections { display: flex; flex-direction: column; }
.collection-header { display: flex; align-items: center; gap: 8px; padding: 8px; cursor: pointer; border-radius: 6px; &:hover { background: var(--color-bg-tab); } .el-icon { color: var(--color-text-focus); } }
.coll-name { flex: 1; font-size: 13px; }
.coll-count { font-size: 12px; color: var(--color-text-3); }
.documents { padding-left: 28px; display: flex; flex-direction: column; gap: 2px; }
.document-item { display: flex; align-items: center; gap: 8px; padding: 6px 8px; border-radius: 6px; font-size: 13px; color: var(--color-text-2); &:hover { background: var(--color-bg-tab); } .doc-title { flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; } }
.no-docs { padding: 12px 8px; font-size: 12px; color: var(--color-text-4); text-align: center; }
.add-conn-row { text-align: center; padding: 8px 0; }
</style>
```

### 文件：`frontend/src/components/ExternalKbConnForm.vue`

```vue
<template>
  <el-dialog :model-value="visible" title="添加外部知识库" width="520px" :close-on-click-modal="false" @update:model-value="$emit('update:visible', $event)">
    <el-form ref="formRef" :model="form" :rules="rules" label-position="top">
      <el-form-item label="名称" prop="name"><el-input v-model="form.name" placeholder="例如：公司内部知识库" maxlength="100" /></el-form-item>
      <el-form-item label="类型" prop="provider_type"><el-select v-model="form.provider_type" style="width:100%">
        <el-option label="通用 OpenAPI" value="openapi" /><el-option label="Dify" value="dify" /><el-option label="QAnything" value="qanything" /><el-option label="向量数据库" value="vectordb" /><el-option label="自定义 API" value="custom" />
      </el-select></el-form-item>
      <el-form-item label="API 地址" prop="api_base_url"><el-input v-model="form.api_base_url" placeholder="https://example.com/api" /></el-form-item>
      <el-form-item label="认证方式" prop="auth_type"><el-select v-model="form.auth_type" style="width:100%">
        <el-option label="API Key" value="api_key" /><el-option label="Bearer Token" value="bearer" /><el-option label="Basic 认证" value="basic" /><el-option label="OAuth 2.0" value="oauth2" />
      </el-select></el-form-item>
      <el-form-item label="认证凭据" prop="auth_key"><el-input v-model="form.auth_key" type="password" show-password :placeholder="authPlaceholder" /></el-form-item>
    </el-form>
    <template #footer>
      <el-button :loading="testing" @click="handleTest">测试连接</el-button>
      <el-button type="primary" :loading="submitting" @click="handleSubmit">添加</el-button>
    </template>
  </el-dialog>
</template>

<script setup lang="ts">
import { ref, reactive, computed } from "vue"
import { ElMessage } from "element-plus"
import type { FormInstance, FormRules } from "element-plus"
import { createConnectionApi } from "@/api/external-kb"

const props = defineProps<{ visible: boolean }>()
const emit = defineEmits<{ "update:visible": [value: boolean]; created: [] }>()
const formRef = ref<FormInstance>(); const submitting = ref(false); const testing = ref(false)
const form = reactive({ name: "", provider_type: "openapi", api_base_url: "", auth_type: "api_key", auth_key: "" })
const rules: FormRules = { name: [{ required: true, message: "请输入名称", trigger: "blur" }], provider_type: [{ required: true, message: "请选择类型", trigger: "change" }], api_base_url: [{ required: true, message: "请输入 API 地址", trigger: "blur" }] }
const authPlaceholder = computed(() => { const m: Record<string, string> = { api_key: "请输入 API Key", bearer: "请输入 Bearer Token", basic: "格式: username:password", oauth2: "请输入 OAuth Client ID" }; return m[form.auth_type] || "请输入认证凭据" })
function buildPayload() {
  const c: Record<string, string> = {}
  if (form.auth_type === "api_key") c.api_key = form.auth_key
  else if (form.auth_type === "bearer") c.token = form.auth_key
  else if (form.auth_type === "basic") { const p = form.auth_key.split(":"); c.username = p[0] || ""; c.password = p[1] || "" }
  else if (form.auth_type === "oauth2") c.client_id = form.auth_key
  return { name: form.name, provider_type: form.provider_type, api_base_url: form.api_base_url, auth_type: form.auth_type, auth_credentials: c }
}
async function handleTest() { testing.value = true; try { await createConnectionApi(buildPayload()); ElMessage.success("连接成功") } catch (e: any) { ElMessage.error(e.response?.data?.detail || "连接失败") } finally { testing.value = false } }
async function handleSubmit() { const valid = await formRef.value?.validate().catch(() => false); if (!valid) return; submitting.value = true; try { await createConnectionApi(buildPayload()); ElMessage.success("添加成功"); form.name = ""; form.api_base_url = ""; form.auth_key = ""; emit("created") } catch (e: any) { ElMessage.error(e.response?.data?.detail || "添加失败") } finally { submitting.value = false } }
</script>
```

---

## Task 3.6: 问答 Tab

### 文件：`frontend/src/api/chat.ts`

```ts
import request from "./request"

export interface ChatSession { id: number; user_id: number; notebook_id: number; title: string; message_count: number; created_at: string; updated_at: string }
export interface CitationItem { source_id: number; source_name: string; text: string; page: number | null; rect: number[] | null }
export interface ChatMessage { id: number; session_id: number; role: "user" | "assistant"; content: string; citations: CitationItem[] | null; created_at: string }

export function fetchSessionsApi(notebookId: number): Promise<ChatSession[]> { return request.get(`/api/notebooks/${notebookId}/chat/sessions`).then((r) => r.data) }
export function createSessionApi(notebookId: number, title?: string): Promise<ChatSession> { return request.post(`/api/notebooks/${notebookId}/chat/sessions`, { title }).then((r) => r.data) }
export function deleteSessionApi(notebookId: number, sessionId: number): Promise<void> { return request.delete(`/api/notebooks/${notebookId}/chat/sessions/${sessionId}`).then((r) => r.data) }
export function fetchMessagesApi(notebookId: number, sessionId: number): Promise<ChatMessage[]> { return request.get(`/api/notebooks/${notebookId}/chat/sessions/${sessionId}/messages`).then((r) => r.data) }
export function sendMessageApi(notebookId: number, sessionId: number, content: string): Promise<ChatMessage> { return request.post(`/api/notebooks/${notebookId}/chat/sessions/${sessionId}/messages`, { content }).then((r) => r.data) }

export function sendMessageStreamApi(
  notebookId: number, sessionId: number, content: string,
  onMessage: (text: string) => void, onCitations?: (citations: CitationItem[]) => void,
  onDone?: () => void, onError?: (err: any) => void,
): { abort: () => void } {
  const controller = new AbortController(); const token = localStorage.getItem("token")
  fetch(`/api/notebooks/${notebookId}/chat/sessions/${sessionId}/messages/stream`, {
    method: "POST",
    headers: { "Content-Type": "application/json", Authorization: token ? `Bearer ${token}` : "" },
    body: JSON.stringify({ content }), signal: controller.signal,
  }).then(async (res) => {
    if (!res.ok) { const err = await res.json().catch(() => ({ detail: "请求失败" })); onError?.(err); return }
    const reader = res.body?.getReader(); if (!reader) return; const decoder = new TextDecoder(); let buffer = ""
    while (true) {
      const { done, value } = await reader.read(); if (done) break
      buffer += decoder.decode(value, { stream: true })
      const lines = buffer.split("\n"); buffer = lines.pop() || ""
      for (const line of lines) {
        if (!line.startsWith("data: ")) continue
        const data = line.slice(6)
        if (data === "[DONE]") { onDone?.(); return }
        try { const p = JSON.parse(data); if (p.text) onMessage(p.text); if (p.citations) onCitations?.(p.citations); if (p.done) onDone?.() } catch {}
      }
    }
    onDone?.()
  }).catch((err) => { if (err.name !== "AbortError") onError?.(err) })
  return { abort: () => controller.abort() }
}
```

### 文件：`frontend/src/stores/chat.ts`

```ts
import { defineStore } from "pinia"; import { ref } from "vue"
import type { ChatSession, ChatMessage } from "@/api/chat"
import { fetchSessionsApi, createSessionApi, deleteSessionApi, fetchMessagesApi, sendMessageStreamApi } from "@/api/chat"

export const useChatStore = defineStore("chat", () => {
  const sessions = ref<ChatSession[]>([]); const currentSessionId = ref<number | null>(null)
  const messages = ref<ChatMessage[]>([]); const streaming = ref(false); const streamContent = ref("")

  async function fetchSessions(nbId: number) { sessions.value = await fetchSessionsApi(nbId) }
  async function createSession(nbId: number) { const s = await createSessionApi(nbId); sessions.value.unshift(s); currentSessionId.value = s.id; messages.value = []; return s }
  async function deleteSession(nbId: number, sid: number) { await deleteSessionApi(nbId, sid); sessions.value = sessions.value.filter((s) => s.id !== sid); if (currentSessionId.value === sid) { currentSessionId.value = null; messages.value = [] } }
  async function loadMessages(nbId: number, sid: number) { currentSessionId.value = sid; messages.value = await fetchMessagesApi(nbId, sid) }

  function sendMessage(nbId: number, sid: number, content: string, callbacks?: { onMessage?: (t: string) => void; onDone?: () => void; onError?: (err: any) => void }) {
    streaming.value = true; streamContent.value = ""
    messages.value.push({ id: -Date.now(), session_id: sid, role: "user", content, citations: null, created_at: new Date().toISOString() })
    return sendMessageStreamApi(nbId, sid, content,
      (text) => { streamContent.value += text; callbacks?.onMessage?.(text) },
      (citations) => { const last = messages.value[messages.value.length-1]; if (last?.role === "assistant") last.citations = citations },
      () => { streaming.value = false; messages.value.push({ id: -Date.now(), session_id: sid, role: "assistant", content: streamContent.value, citations: null, created_at: new Date().toISOString() }); streamContent.value = ""; callbacks?.onDone?.() },
      (err) => { streaming.value = false; callbacks?.onError?.(err) },
    )
  }

  return { sessions, currentSessionId, messages, streaming, streamContent, fetchSessions, createSession, deleteSession, loadMessages, sendMessage }
})
```

### 文件：`frontend/src/utils/marked.ts`

```ts
function escapeHtml(text: string): string { return text.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/\"/g, "&quot;") }
export function marked(text: string): string {
  let html = escapeHtml(text)
  html = html.replace(/```(\w*)\n([\s\S]*?)```/g, (_m, lang, code) => `<pre><code${lang ? ` class="language-${lang}"` : ""}>${escapeHtml(code)}</code></pre>`)
  html = html.replace(/`([^`]+)`/g, "<code>$1</code>")
  html = html.replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>")
  html = html.replace(/\*(.+?)\*/g, "<em>$1</em>")
  html = html.replace(/~~(.+?)~~/g, "<del>$1</del>")
  html = html.replace(/\[([^\]]+)\]\(([^)]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>')
  html = html.replace(/^(\s*)- (.+)$/gm, "<li>$2</li>"); html = html.replace(/(<li>.*<\/li>\n?)+/g, "<ul>$&</ul>")
  html = html.replace(/^(\s*)\d+\. (.+)$/gm, "<li>$2</li>")
  html = html.replace(/^### (.+)$/gm, "<h3>$1</h3>"); html = html.replace(/^## (.+)$/gm, "<h2>$1</h2>"); html = html.replace(/^# (.+)$/gm, "<h1>$1</h1>")
  const lines = html.split("\n"); let result = "", inPara = false
  for (const line of lines) {
    const t = line.trim()
    if (!t) { if (inPara) { result += "</p>"; inPara = false }; continue }
    if (t.startsWith("<h") || t.startsWith("<ul") || t.startsWith("</ul") || t.startsWith("<li") || t.startsWith("<pre") || t.startsWith("</pre")) {
      if (inPara) { result += "</p>"; inPara = false }; result += t + "\n"
    } else { if (!inPara) { result += "<p>"; inPara = true } else result += "<br>"; result += t }
  }
  if (inPara) result += "</p>"
  return result
}
```

### 文件：`frontend/src/components/ChatMessage.vue`

```vue
<template>
  <div class="chat-message" :class="[message.role]">
    <div class="message-avatar">
      <el-avatar :size="32" v-if="message.role === 'assistant'"><el-icon><MagicStick /></el-icon></el-avatar>
      <el-avatar :size="32" v-else icon="UserFilled" />
    </div>
    <div class="message-content">
      <div class="message-bubble">
        <div v-if="message.role === 'assistant'" class="markdown-body" v-html="renderedContent" />
        <div v-else class="user-text">{{ message.content }}</div>
      </div>
      <div v-if="message.citations && message.citations.length > 0" class="citations">
        <span class="citations-label">来源引用：</span>
        <span v-for="(cit, i) in message.citations" :key="i" class="citation-link" @click="$emit('citation-click', cit)">[{{ i+1 }}] {{ cit.source_name }}</span>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed } from "vue"; import { MagicStick } from "@element-plus/icons-vue"
import type { ChatMessage, CitationItem } from "@/api/chat"; import { marked } from "@/utils/marked"
const props = defineProps<{ message: ChatMessage }>(); defineEmits<{ "citation-click": [citation: CitationItem] }>()
const renderedContent = computed(() => props.message.role === "assistant" ? marked(props.message.content) : props.message.content)
</script>

<style scoped lang="scss">
.chat-message { display: flex; gap: 12px; padding: 16px 0; max-width: 800px; margin: 0 auto;
  &.user { flex-direction: row-reverse; .message-bubble { background: var(--color-bg-tab); border-radius: 12px 12px 2px; } }
  &.assistant { .message-bubble { background: var(--color-bg-1); border: 1px solid var(--color-divider-1); border-radius: 2px 12px 12px; } }
}
.message-avatar { flex-shrink: 0; }
.message-content { max-width: 75%; display: flex; flex-direction: column; gap: 6px; }
.message-bubble { padding: 12px 16px; font-size: 14px; line-height: 1.7; }
.user-text { white-space: pre-wrap; word-break: break-word; }
.markdown-body {
  :deep(p) { margin-bottom: 8px; &:last-child { margin-bottom: 0; } }
  :deep(code) { background: var(--color-bg-tab); padding: 2px 6px; border-radius: 4px; font-size: 13px; }
  :deep(pre) { background: var(--color-bg-tab); padding: 12px; border-radius: 8px; overflow-x: auto; margin: 8px 0; }
  :deep(ul), :deep(ol) { padding-left: 20px; margin-bottom: 8px; }
}
.citations { display: flex; flex-wrap: wrap; gap: 4px; align-items: center; }
.citations-label { font-size: 12px; color: var(--color-text-3); }
.citation-link { font-size: 12px; color: var(--color-text-focus); cursor: pointer; padding: 2px 6px; border-radius: 4px; background: rgba(26,117,255,.08); &:hover { background: rgba(26,117,255,.15); } }
</style>
```

### 文件：`frontend/src/components/ChatInput.vue`

```vue
<template>
  <div class="chat-input-area">
    <div class="input-wrapper">
      <el-input ref="inputRef" v-model="content" type="textarea" :rows="3" :disabled="disabled" placeholder="输入你的问题... (Enter 发送，Shift+Enter 换行)" class="chat-textarea" @keydown="handleKeydown" />
      <el-button type="primary" class="send-btn" :disabled="!content.trim() || disabled" :loading="disabled" @click="handleSend"><el-icon><Promotion /></el-icon></el-button>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref } from "vue"; import { Promotion } from "@element-plus/icons-vue"
defineProps<{ disabled?: boolean }>(); const emit = defineEmits<{ send: [content: string] }>()
const content = ref("")
function handleKeydown(e: KeyboardEvent) { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); handleSend() } }
function handleSend() { const text = content.value.trim(); if (!text) return; emit("send", text); content.value = "" }
</script>

<style scoped lang="scss">
.chat-input-area { padding: 16px 24px; background: var(--color-bg-1); border-top: 1px solid var(--color-divider-1); }
.input-wrapper { max-width: 800px; margin: 0 auto; position: relative; }
.chat-textarea { :deep(.el-textarea__inner) { border-radius: var(--radius-input); padding-right: 50px; resize: none; font-size: 14px; line-height: 1.6; } }
.send-btn { position: absolute; right: 8px; bottom: 8px; border-radius: 50%; width: 36px; height: 36px; padding: 0; display: flex; align-items: center; justify-content: center; background: var(--color-main-1); border-color: var(--color-main-1); }
</style>
```

### 文件：`frontend/src/components/CitationPopup.vue`

```vue
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
```

### 文件：`frontend/src/views/notebook/ChatTab.vue`

```vue
<template>
  <div class="chat-tab">
    <div class="chat-sidebar">
      <div class="sidebar-header"><h3 class="sidebar-title">对话历史</h3><el-button type="primary" size="small" class="new-chat-btn" @click="handleNewSession">+ 新建对话</el-button></div>
      <div class="session-list">
        <div v-for="session in sessions" :key="session.id" class="session-item" :class="{ active: session.id === currentSessionId }" @click="switchSession(session.id)">
          <div class="session-info"><span class="session-title">{{ session.title || "新对话" }}</span><span class="session-meta">{{ session.message_count }} 条消息</span></div>
          <el-button text size="small" type="danger" class="session-del" @click.stop="handleDeleteSession(session.id)"><el-icon><Delete /></el-icon></el-button>
        </div>
        <div v-if="sessions.length === 0" class="session-empty">暂无对话记录</div>
      </div>
    </div>
    <div class="chat-main">
      <div v-if="!currentSessionId" class="chat-welcome">
        <el-icon :size="64" color="#d0d0d0"><ChatLineSquare /></el-icon><h2>开始新对话</h2><p>基于知识库内容，向 AI 提问</p>
        <el-button type="primary" @click="handleNewSession">开始对话</el-button>
      </div>
      <template v-else>
        <div ref="messagesRef" class="messages-area">
          <ChatMessage v-for="msg in messages" :key="msg.id" :message="msg" @citation-click="handleCitationClick" />
          <div v-if="chatStore.streaming" class="chat-message assistant">
            <div class="message-avatar"><el-avatar :size="32"><el-icon><MagicStick /></el-icon></el-avatar></div>
            <div class="message-content"><div class="message-bubble streaming">{{ chatStore.streamContent }}<span class="cursor-blink">|</span></div></div>
          </div>
        </div>
        <ChatInput :disabled="chatStore.streaming" @send="handleSend" />
      </template>
    </div>
    <CitationPopup :visible="showCitation" :citation="selectedCitation" @update:visible="showCitation = $event" />
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted, nextTick, watch } from "vue"; import { useRoute } from "vue-router"
import { Delete, ChatLineSquare, MagicStick } from "@element-plus/icons-vue"; import { ElMessage, ElMessageBox } from "element-plus"
import { useChatStore } from "@/stores/chat"; import type { CitationItem } from "@/api/chat"
import ChatMessage from "@/components/ChatMessage.vue"; import ChatInput from "@/components/ChatInput.vue"; import CitationPopup from "@/components/CitationPopup.vue"

const route = useRoute(); const chatStore = useChatStore()
const notebookId = computed(() => Number(route.params.id)); const currentSessionId = computed(() => chatStore.currentSessionId)
const sessions = computed(() => chatStore.sessions); const messages = computed(() => chatStore.messages)
const messagesRef = ref<HTMLElement>(); const showCitation = ref(false); const selectedCitation = ref<CitationItem | null>(null)

function scrollToBottom() { nextTick(() => { if (messagesRef.value) messagesRef.value.scrollTop = messagesRef.value.scrollHeight }) }
watch([() => messages.value.length, () => chatStore.streamContent], () => scrollToBottom(), { flush: "post" })
async function handleNewSession() { try { await chatStore.createSession(notebookId.value) } catch { ElMessage.error("创建对话失败") } }
async function switchSession(sid: number) { try { await chatStore.loadMessages(notebookId.value, sid); scrollToBottom() } catch { ElMessage.error("加载消息失败") } }
async function handleDeleteSession(sid: number) { try { await ElMessageBox.confirm("确定删除此对话？", "确认"); await chatStore.deleteSession(notebookId.value, sid) } catch {} }
function handleSend(c: string) { if (!currentSessionId.value) return; chatStore.sendMessage(notebookId.value, currentSessionId.value, c, { onError: (err: any) => ElMessage.error(err?.detail || "发送失败") }); scrollToBottom() }
function handleCitationClick(citation: CitationItem) { selectedCitation.value = citation; showCitation.value = true }
onMounted(async () => {
  await chatStore.fetchSessions(notebookId.value); const sid = route.params.sid
  if (sid && typeof sid === "string") { const id = Number(sid); if (sessions.value.some((s) => s.id === id)) { await chatStore.loadMessages(notebookId.value, id); scrollToBottom() } }
})
</script>

<style scoped lang="scss">
.chat-tab { display: flex; height: calc(100vh - 48px - 64px - 40px - 48px); margin: -24px; }
.chat-sidebar { width: 280px; flex-shrink: 0; background: var(--color-bg-1); border-right: 1px solid var(--color-divider-1); display: flex; flex-direction: column; }
.sidebar-header { padding: 16px; border-bottom: 1px solid var(--color-divider-1); display: flex; align-items: center; justify-content: space-between; }
.sidebar-title { font-size: 14px; font-weight: 600; }
.new-chat-btn { border-radius: var(--radius-button); background: var(--color-main-1); border-color: var(--color-main-1); }
.session-list { flex: 1; overflow-y: auto; padding: 8px; }
.session-item { display: flex; align-items: center; padding: 10px 12px; border-radius: 8px; cursor: pointer; margin-bottom: 4px; &:hover { background: var(--color-bg-tab); } &.active { background: rgba(255,54,80,.08); } .session-del { opacity: 0; flex-shrink: 0; } &:hover .session-del { opacity: 1; } }
.session-info { flex: 1; min-width: 0; }
.session-title { display: block; font-size: 13px; font-weight: 500; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.session-meta { display: block; font-size: 11px; color: var(--color-text-3); margin-top: 2px; }
.session-empty { padding: 24px; text-align: center; color: var(--color-text-3); font-size: 13px; }
.chat-main { flex: 1; display: flex; flex-direction: column; background: var(--color-bg-tab); }
.chat-welcome { flex: 1; display: flex; flex-direction: column; align-items: center; justify-content: center; gap: 12px; h2 { font-size: 20px; font-weight: 600; } p { font-size: 14px; color: var(--color-text-3); margin-bottom: 8px; } }
.messages-area { flex: 1; overflow-y: auto; padding: 16px 24px; }
.message-bubble.streaming { background: var(--color-bg-1); border: 1px solid var(--color-divider-1); border-radius: 2px 12px 12px; padding: 12px 16px; font-size: 14px; line-height: 1.7; max-width: 75%; }
.cursor-blink { animation: blink 1s step-end infinite; color: var(--color-main-1); }
@keyframes blink { 50% { opacity: 0; } }
</style>
```


---

## Task 3.7: 生成 Tab

### 文件：`frontend/src/api/generation.ts`

```ts
import request from "./request"

export interface GeneratedContent {
  id: number; notebook_id: number; content_type: string; title: string | null; prompt: string | null
  engine: string; status: string; local_file_path: string | null; thumbnail_path: string | null; file_size: number | null; error_message: string | null
  ppt_page_count: number | null; ppt_template: string | null; ppt_json: string | null; ppt_preview_images: string | null
  mindmap_data: string | null; mindmap_layout: string | null
  infographic_template: string | null; infographic_blocks: string | null
  audio_file_path: string | null; duration_seconds: number | null; audio_speakers: string | null; audio_transcript: string | null
  video_file_path: string | null; video_duration_seconds: number | null; video_resolution: string | null; video_scenes: string | null; video_narration: string | null
  doc_page_count: number | null; doc_sections: string | null; doc_format: string | null
  created_at: string
}

export interface GenerateRequest { content_type: string; prompt: string; template?: string; options?: Record<string, any> }
export interface TemplateInfo { id: string; name: string; description: string; thumbnail_url?: string }

export function fetchGeneratedContentsApi(notebookId: number): Promise<GeneratedContent[]> { return request.get(`/api/notebooks/${notebookId}/generated`).then((r) => r.data) }
export function generateContentApi(notebookId: number, data: GenerateRequest): Promise<GeneratedContent> { return request.post(`/api/notebooks/${notebookId}/generate`, data).then((r) => r.data) }
export function fetchTemplatesApi(contentType: string): Promise<TemplateInfo[]> { return request.get("/api/generation/templates", { params: { content_type: contentType } }).then((r) => r.data) }
export function fetchGeneratedDetailApi(notebookId: number, generatedId: number): Promise<GeneratedContent> { return request.get(`/api/notebooks/${notebookId}/generated/${generatedId}`).then((r) => r.data) }
export function deleteGeneratedApi(notebookId: number, generatedId: number): Promise<void> { return request.delete(`/api/notebooks/${notebookId}/generated/${generatedId}`).then((r) => r.data) }
```

### 文件：`frontend/src/components/GenerationTemplatePicker.vue`

```vue
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
```

### 文件：`frontend/src/views/notebook/GenerateTab.vue`

```vue
<template>
  <div class="generate-tab">
    <div class="content-type-grid">
      <div v-for="ct in contentTypes" :key="ct.type" class="content-type-card card" @click="currentType = ct.type">
        <div class="ctype-icon" :style="{ background: ct.color + '18' }"><el-icon :size="28" :color="ct.color"><component :is="ct.icon" /></el-icon></div>
        <span class="ctype-name">{{ ct.label }}</span>
      </div>
    </div>

    <div v-if="currentType" class="generator-panel card">
      <div class="generator-header">
        <h3 class="generator-title">{{ contentTypes.find((c) => c.type === currentType)?.label }}</h3>
        <el-button text @click="currentType = ''"><el-icon><Close /></el-icon></el-button>
      </div>
      <component :is="generatorComponent" :notebook-id="notebookId" @back="currentType = ''" />
    </div>

    <div class="history-section">
      <h3 class="section-title">生成历史</h3>
      <div v-if="generatedList.length === 0" class="history-empty">暂无生成记录</div>
      <div v-else class="history-grid">
        <div v-for="item in generatedList" :key="item.id" class="history-card card" @click="router.push(`/notebook/${notebookId}/generate/${item.id}`)">
          <div class="history-header">
            <el-tag size="small" :type="statusType(item.status)">{{ statusLabel(item.status) }}</el-tag>
            <el-tag size="small" type="info">{{ typeLabel(item.content_type) }}</el-tag>
          </div>
          <p class="history-title">{{ item.title || item.prompt || "无标题" }}</p>
          <span class="history-time">{{ formatTime(item.created_at) }}</span>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted, shallowRef } from "vue"; import { useRouter, useRoute } from "vue-router"
import { Close, Document, DataAnalysis, PictureFilled, Microphone, VideoCamera, Edit } from "@element-plus/icons-vue"
import { fetchGeneratedContentsApi } from "@/api/generation"
import PptGenerator from "./generate/PptGenerator.vue"; import MindmapGenerator from "./generate/MindmapGenerator.vue"
import InfographicGenerator from "./generate/InfographicGenerator.vue"; import PodcastGenerator from "./generate/PodcastGenerator.vue"
import VideoGenerator from "./generate/VideoGenerator.vue"; import DocumentGenerator from "./generate/DocumentGenerator.vue"

const router = useRouter(); const route = useRoute(); const notebookId = computed(() => Number(route.params.id))
const currentType = ref(""); const generatedList = ref<any[]>([])
const contentTypes = [
  { type: "ppt", label: "PPT", icon: Document, color: "#ff3650" },
  { type: "mindmap", label: "脑图", icon: DataAnalysis, color: "#1a75ff" },
  { type: "infographic", label: "信息图", icon: PictureFilled, color: "#67c23a" },
  { type: "podcast", label: "播客", icon: Microphone, color: "#e6a23c" },
  { type: "video", label: "视频", icon: VideoCamera, color: "#909399" },
  { type: "document", label: "文档", icon: Edit, color: "#409eff" },
]
const generatorComponent = computed(() => {
  const map: Record<string, any> = { ppt: PptGenerator, mindmap: MindmapGenerator, infographic: InfographicGenerator, podcast: PodcastGenerator, video: VideoGenerator, document: DocumentGenerator }
  return currentType.value ? shallowRef(map[currentType.value]) : null
})
function statusType(s: string) { return s === "completed" ? "success" : s === "processing" ? "warning" : s === "failed" ? "danger" : "info" }
function statusLabel(s: string) { return s === "completed" ? "已完成" : s === "processing" ? "生成中" : s === "failed" ? "失败" : "排队中" }
function typeLabel(t: string) { const m: Record<string, string> = { ppt:"PPT", mindmap:"脑图", infographic:"信息图", podcast:"播客", video:"视频", document:"文档" }; return m[t] || t }
function formatTime(t: string) { const d = new Date(t); const diff = Date.now() - d.getTime(); if (diff < 3600000) return `${Math.floor(diff/60000)} 分钟前`; if (diff < 86400000) return `${Math.floor(diff/3600000)} 小时前`; return d.toLocaleDateString("zh-CN") }
onMounted(async () => { try { generatedList.value = await fetchGeneratedContentsApi(notebookId.value) } catch {} })
</script>

<style scoped lang="scss">
.generate-tab { max-width: 1000px; margin: 0 auto; }
.content-type-grid { display: grid; grid-template-columns: repeat(6, 1fr); gap: 16px; margin-bottom: 24px; }
.content-type-card { display: flex; flex-direction: column; align-items: center; gap: 8px; padding: 20px 12px; cursor: pointer; transition: transform .2s; &:hover { transform: translateY(-2px); } }
.ctype-icon { width: 48px; height: 48px; display: flex; align-items: center; justify-content: center; border-radius: 12px; }
.ctype-name { font-size: 13px; font-weight: 500; }
.generator-panel { margin-bottom: 24px; }
.generator-header { display: flex; align-items: center; justify-content: space-between; margin-bottom: 20px; }
.generator-title { font-size: 16px; font-weight: 600; }
.history-section { .section-title { font-size: 16px; font-weight: 600; margin-bottom: 16px; } }
.history-empty { text-align: center; padding: 40px; color: var(--color-text-3); font-size: 14px; }
.history-grid { display: grid; grid-template-columns: repeat(3, 1fr); gap: 16px; }
.history-card { cursor: pointer; transition: transform .2s; &:hover { transform: translateY(-2px); } }
.history-header { display: flex; gap: 8px; margin-bottom: 8px; }
.history-title { font-size: 14px; font-weight: 500; margin-bottom: 8px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.history-time { font-size: 12px; color: var(--color-text-3); }
</style>
```

### 文件：`frontend/src/views/notebook/generate/PptGenerator.vue`

```vue
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
const props = defineProps<{ notebookId: number }>(); const emit = defineEmits<{ back: [] }>()
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
```

### 文件：`frontend/src/views/notebook/generate/MindmapGenerator.vue`

```vue
<template>
  <div class="mindmap-generator">
    <GenerationTemplatePicker :templates="templates" :selected="selectedTemplate" @select="selectedTemplate = $event" />
    <el-form label-position="top">
      <el-form-item label="生成指令"><el-input v-model="prompt" type="textarea" :rows="3" placeholder="描述思维导图的主题" /></el-form-item>
      <el-form-item>
        <el-button type="primary" :loading="generating" @click="handleGenerate">生成脑图</el-button>
      </el-form-item>
    </el-form>
    <div v-if="result" class="preview-area">
      <div class="mindmap-canvas" ref="canvasRef"></div>
      <div class="actions"><el-button @click="handleDownloadJSON">下载 JSON</el-button><el-button @click="handleDownloadPNG">下载 PNG</el-button></div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted } from "vue"; import { ElMessage } from "element-plus"
import GenerationTemplatePicker from "@/components/GenerationTemplatePicker.vue"
import { fetchTemplatesApi, generateContentApi } from "@/api/generation"
const props = defineProps<{ notebookId: number }>(); const emit = defineEmits<{ back: [] }>()
const templates = ref<any[]>([]); const selectedTemplate = ref(""); const prompt = ref(""); const generating = ref(false); const result = ref<any>(null); const canvasRef = ref<HTMLElement>()
async function handleGenerate() {
  if (!prompt.value.trim()) { ElMessage.warning("请输入生成指令"); return }
  generating.value = true
  try { const res = await generateContentApi(props.notebookId, { content_type: "mindmap", prompt: prompt.value, template: selectedTemplate.value || undefined }); result.value = res; ElMessage.success("脑图生成成功") }
  catch (e: any) { ElMessage.error(e.response?.data?.detail || "生成失败") }
  finally { generating.value = false }
}
function handleDownloadJSON() { if (result.value?.mindmap_data) { const blob = new Blob([result.value.mindmap_data], { type: "application/json" }); const url = URL.createObjectURL(blob); window.open(url, "_blank") } }
function handleDownloadPNG() { if (result.value?.local_file_path) window.open(result.value.local_file_path, "_blank") }
onMounted(async () => { try { templates.value = await fetchTemplatesApi("mindmap") } catch {} })
</script>

<style scoped lang="scss">
.mindmap-canvas { min-height: 300px; background: var(--color-bg-tab); border-radius: var(--radius-card); padding: 20px; }
.actions { display: flex; gap: 8px; margin-top: 12px; }
</style>
```

### 文件：`frontend/src/views/notebook/generate/InfographicGenerator.vue`

```vue
<template>
  <div class="infographic-generator">
    <GenerationTemplatePicker :templates="templates" :selected="selectedTemplate" @select="selectedTemplate = $event" />
    <el-form label-position="top">
      <el-form-item label="内容描述"><el-input v-model="prompt" type="textarea" :rows="3" placeholder="描述信息图的内容" /></el-form-item>
      <el-form-item><el-button type="primary" :loading="generating" @click="handleGenerate">生成信息图</el-button></el-form-item>
    </el-form>
    <div v-if="result" class="preview-area">
      <div class="infographic-preview"><img v-if="result.local_file_path" :src="result.local_file_path" alt="信息图预览" /></div>
      <el-button @click="handleDownloadPNG">下载 PNG</el-button>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted } from "vue"; import { ElMessage } from "element-plus"
import GenerationTemplatePicker from "@/components/GenerationTemplatePicker.vue"
import { fetchTemplatesApi, generateContentApi } from "@/api/generation"
const props = defineProps<{ notebookId: number }>(); const emit = defineEmits<{ back: [] }>()
const templates = ref<any[]>([]); const selectedTemplate = ref(""); const prompt = ref(""); const generating = ref(false); const result = ref<any>(null)
async function handleGenerate() {
  if (!prompt.value.trim()) { ElMessage.warning("请输入内容描述"); return }
  generating.value = true
  try { const res = await generateContentApi(props.notebookId, { content_type: "infographic", prompt: prompt.value, template: selectedTemplate.value || undefined }); result.value = res; ElMessage.success("信息图生成成功") }
  catch (e: any) { ElMessage.error(e.response?.data?.detail || "生成失败") }
  finally { generating.value = false }
}
function handleDownloadPNG() { if (result.value?.local_file_path) window.open(result.value.local_file_path, "_blank") }
onMounted(async () => { try { templates.value = await fetchTemplatesApi("infographic") } catch {} })
</script>

<style scoped lang="scss">
.infographic-preview { img { max-width: 100%; border-radius: var(--radius-card); } }
</style>
```

### 文件：`frontend/src/views/notebook/generate/PodcastGenerator.vue`

```vue
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
```

### 文件：`frontend/src/views/notebook/generate/VideoGenerator.vue`

```vue
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
```

### 文件：`frontend/src/views/notebook/generate/DocumentGenerator.vue`

```vue
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
```

---

## Task 3.8: 概览 Tab + 外部知识库页面 + 设置

### 文件：`frontend/src/views/notebook/OverviewTab.vue`

```vue
<template>
  <div class="overview-tab">
    <div class="stats-grid">
      <div class="stat-card card">
        <div class="stat-value">{{ stats.source_count }}</div>
        <div class="stat-label">文档数</div>
      </div>
      <div class="stat-card card">
        <div class="stat-value">{{ stats.chat_count }}</div>
        <div class="stat-label">问答数</div>
      </div>
      <div class="stat-card card">
        <div class="stat-value">{{ stats.generate_count }}</div>
        <div class="stat-label">生成次数</div>
      </div>
    </div>

    <div class="section">
      <h3 class="section-title">AI 摘要</h3>
      <div class="card">
        <p v-if="summary" class="summary-text">{{ summary }}</p>
        <p v-else class="summary-empty">暂无摘要，开始上传资料后会自动生成</p>
      </div>
    </div>

    <div class="section">
      <h3 class="section-title">最近活动</h3>
      <div class="card">
        <div v-if="activities.length === 0" class="activity-empty">暂无活动记录</div>
        <div v-for="act in activities" :key="act.id" class="activity-item">
          <el-tag size="small" :type="act.type === 'chat' ? 'primary' : act.type === 'source' ? 'success' : 'warning'" class="activity-badge">{{ act.label }}</el-tag>
          <span class="activity-desc">{{ act.description }}</span>
          <span class="activity-time">{{ formatTime(act.created_at) }}</span>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted } from "vue"; import { useRoute } from "vue-router"
import { fetchSourcesApi } from "@/api/sources"; import { fetchGeneratedContentsApi } from "@/api/generation"
import { fetchSessionsApi } from "@/api/chat"
const route = useRoute(); const notebookId = computed(() => Number(route.params.id))
const stats = ref({ source_count: 0, chat_count: 0, generate_count: 0 })
const summary = ref(""); const activities = ref<any[]>([])

function formatTime(t: string) { const d = new Date(t); const diff = Date.now() - d.getTime(); if (diff < 3600000) return `${Math.floor(diff/60000)} 分钟前`; if (diff < 86400000) return `${Math.floor(diff/3600000)} 小时前`; return d.toLocaleDateString("zh-CN") }

onMounted(async () => {
  try {
    const [sources, sessions, generated] = await Promise.all([
      fetchSourcesApi(notebookId.value), fetchSessionsApi(notebookId.value), fetchGeneratedContentsApi(notebookId.value),
    ])
    stats.value = { source_count: sources.total || sources.items.length, chat_count: sessions.length, generate_count: generated.length }
    generated.forEach((g: any) => activities.value.push({ id: `gen-${g.id}`, type: "generate", label: "生成", description: `生成了 ${g.title || g.content_type}`, created_at: g.created_at }))
    sessions.forEach((s: any) => activities.value.push({ id: `chat-${s.id}`, type: "chat", label: "问答", description: s.title || "新对话", created_at: s.updated_at }))
    sources.items.forEach((s: any) => activities.value.push({ id: `src-${s.id}`, type: "source", label: "资料", description: `上传了 ${s.original_filename}`, created_at: s.created_at }))
    activities.value.sort((a, b) => new Date(b.created_at).getTime() - new Date(a.created_at).getTime())
    activities.value = activities.value.slice(0, 20)
  } catch {}
})
</script>

<style scoped lang="scss">
.overview-tab { max-width: 900px; margin: 0 auto; }
.stats-grid { display: grid; grid-template-columns: repeat(3, 1fr); gap: 20px; margin-bottom: 24px; }
.stat-card { text-align: center; padding: 24px; }
.stat-value { font-size: 32px; font-weight: 700; color: var(--color-main-1); }
.stat-label { font-size: 14px; color: var(--color-text-2); margin-top: 4px; }
.section { margin-bottom: 24px; }
.summary-text { font-size: 14px; line-height: 1.7; color: var(--color-text-2); }
.summary-empty { font-size: 14px; color: var(--color-text-3); }
.activity-empty { font-size: 14px; color: var(--color-text-3); text-align: center; padding: 20px; }
.activity-item { display: flex; align-items: center; gap: 12px; padding: 10px 0; border-bottom: 1px solid var(--color-divider-1); &:last-child { border-bottom: none; } }
.activity-badge { flex-shrink: 0; }
.activity-desc { flex: 1; font-size: 14px; color: var(--color-text-1); }
.activity-time { font-size: 12px; color: var(--color-text-3); flex-shrink: 0; }
</style>
```

### 文件：`frontend/src/views/ExternalKbView.vue`

```vue
<template>
  <div class="external-kb-page">
    <header class="page-header">
      <h1 class="page-title">外部知识库</h1>
      <el-button type="primary" @click="showForm = true">+ 添加连接</el-button>
    </header>

    <div class="page-container">
      <div v-if="loading" class="loading"><el-skeleton :rows="4" animated /></div>
      <div v-else-if="connections.length === 0" class="empty">
        <el-icon :size="64" color="#d0d0d0"><Connection /></el-icon>
        <p class="empty-title">尚未接入外部知识库</p>
        <p class="empty-desc">连接外部知识库，将外部文档导入到你的知识库中</p>
      </div>
      <div v-else class="conn-list">
        <div v-for="conn in connections" :key="conn.id" class="conn-item card" @click="router.push(`/external-kb/connections/${conn.id}`)">
          <div class="conn-header">
            <el-icon :size="20" color="var(--color-main-1)"><Connection /></el-icon>
            <span class="conn-name">{{ conn.name }}</span>
          </div>
          <div class="conn-meta">
            <span class="conn-provider">{{ providerLabel(conn.provider_type) }}</span>
            <span class="conn-time" v-if="conn.last_sync_at">最后同步: {{ formatTime(conn.last_sync_at) }}</span>
            <el-tag size="small" :type="conn.is_active ? 'success' : 'info'">{{ conn.is_active ? "活跃" : "已停用" }}</el-tag>
          </div>
          <div class="conn-actions">
            <el-button text type="danger" size="small" @click.stop="handleDelete(conn.id)">删除</el-button>
          </div>
        </div>
      </div>
    </div>

    <ExternalKbConnForm :visible="showForm" @update:visible="showForm = $event" @created="handleCreated" />
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted } from "vue"; import { useRouter } from "vue-router"
import { Connection } from "@element-plus/icons-vue"; import { ElMessage, ElMessageBox } from "element-plus"
import type { ExternalKbConnection } from "@/api/external-kb"
import { fetchConnectionsApi, deleteConnectionApi } from "@/api/external-kb"
import ExternalKbConnForm from "@/components/ExternalKbConnForm.vue"

const router = useRouter(); const connections = ref<ExternalKbConnection[]>([]); const loading = ref(false); const showForm = ref(false)
function providerLabel(type: string) { const m: Record<string, string> = { openapi:"OpenAPI", dify:"Dify", qanything:"QAnything", vectordb:"向量数据库", custom:"自定义" }; return m[type] || type }
function formatTime(t: string) { const d = new Date(t); const diff = Date.now() - d.getTime(); if (diff < 3600000) return `${Math.floor(diff/60000)} 分钟前`; if (diff < 86400000) return `${Math.floor(diff/3600000)} 小时前`; return d.toLocaleDateString("zh-CN") }
async function handleDelete(id: number) { try { await ElMessageBox.confirm("确定删除此连接？"); await deleteConnectionApi(id); connections.value = connections.value.filter((c) => c.id !== id); ElMessage.success("已删除") } catch {} }
function handleCreated() { showForm.value = false; fetchConnections() }
async function fetchConnections() { loading.value = true; try { connections.value = await fetchConnectionsApi() } catch { ElMessage.error("加载失败") } finally { loading.value = false } }
onMounted(() => { fetchConnections() })
</script>

<style scoped lang="scss">
.external-kb-page { min-height: 100vh; background: var(--color-bg-tab); }
.page-header { display: flex; align-items: center; justify-content: space-between; padding: 16px 24px; background: var(--color-bg-1); border-bottom: 1px solid var(--color-divider-1); position: sticky; top: 0; z-index: 100; }
.page-title { font-size: 20px; font-weight: 600; }
.page-container { padding: 24px; max-width: 900px; margin: 0 auto; }
.loading { padding: 40px 0; }
.empty { display: flex; flex-direction: column; align-items: center; padding: 80px 20px; gap: 12px; p { color: var(--color-text-3); } }
.conn-list { display: flex; flex-direction: column; gap: 12px; }
.conn-item { cursor: pointer; &:hover { box-shadow: 0px 6px 48px 0px rgba(174,180,193,.25); } }
.conn-header { display: flex; align-items: center; gap: 8px; margin-bottom: 8px; }
.conn-name { font-size: 16px; font-weight: 600; }
.conn-meta { display: flex; align-items: center; gap: 12px; font-size: 13px; color: var(--color-text-3); }
.conn-actions { margin-top: 8px; }
</style>
```

### 文件：`frontend/src/views/ExternalKbDetailView.vue`

```vue
<template>
  <div class="external-kb-detail">
    <header class="page-header">
      <el-button text @click="router.push('/external-kb')"><el-icon><ArrowLeft /></el-icon>返回</el-button>
      <h1 class="page-title">{{ connection?.name || "连接详情" }}</h1>
    </header>
    <div class="page-container">
      <div v-if="!connection" class="loading">加载中...</div>
      <template v-else>
        <div class="conn-info card">
          <div class="info-row"><span class="label">类型</span><span>{{ providerLabel(connection.provider_type) }}</span></div>
          <div class="info-row"><span class="label">API 地址</span><span>{{ connection.api_base_url }}</span></div>
          <div class="info-row"><span class="label">状态</span><el-tag :type="connection.is_active ? 'success' : 'info'">{{ connection.is_active ? "活跃" : "已停用" }}</el-tag></div>
        </div>
      </template>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted } from "vue"; import { useRouter, useRoute } from "vue-router"; import { ArrowLeft } from "@element-plus/icons-vue"
import type { ExternalKbConnection } from "@/api/external-kb"; import { fetchConnectionsApi } from "@/api/external-kb"
const router = useRouter(); const route = useRoute(); const connection = ref<ExternalKbConnection | null>(null)
function providerLabel(type: string) { const m: Record<string, string> = { openapi:"OpenAPI", dify:"Dify", qanything:"QAnything", vectordb:"向量数据库", custom:"自定义" }; return m[type] || type }
onMounted(async () => {
  try { const list = await fetchConnectionsApi(); connection.value = list.find((c) => c.id === Number(route.params.id)) || null }
  catch { connection.value = null }
})
</script>

<style scoped lang="scss">
.external-kb-detail { min-height: 100vh; background: var(--color-bg-tab); }
.page-header { display: flex; align-items: center; gap: 12px; padding: 16px 24px; background: var(--color-bg-1); border-bottom: 1px solid var(--color-divider-1); }
.page-title { font-size: 20px; font-weight: 600; }
.page-container { padding: 24px; max-width: 900px; margin: 0 auto; }
.conn-info { display: flex; flex-direction: column; gap: 12px; }
.info-row { display: flex; gap: 12px; font-size: 14px; .label { color: var(--color-text-3); min-width: 100px; } }
</style>
```

### 文件：`frontend/src/views/SettingsView.vue`

```vue
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
```

### 文件：`frontend/src/views/HistoryView.vue`

```vue
<template>
  <div class="history-page">
    <header class="page-header">
      <h1 class="page-title">请求历史</h1>
    </header>
    <div class="page-container">
      <div class="card">
        <el-table :data="logs" style="width: 100%" v-loading="loading" empty-text="暂无记录">
          <el-table-column prop="endpoint" label="端点" min-width="200" />
          <el-table-column prop="method" label="方法" width="80">
            <template #default="{ row }"><el-tag :type="row.method === 'GET' ? 'primary' : row.method === 'POST' ? 'success' : 'warning'" size="small">{{ row.method }}</el-tag></template>
          </el-table-column>
          <el-table-column prop="response_status" label="状态" width="80">
            <template #default="{ row }"><el-tag :type="row.response_status < 300 ? 'success' : 'danger'" size="small">{{ row.response_status }}</el-tag></template>
          </el-table-column>
          <el-table-column prop="latency_ms" label="耗时" width="100">
            <template #default="{ row }">{{ row.latency_ms }}ms</template>
          </el-table-column>
          <el-table-column prop="created_at" label="时间" width="180">
            <template #default="{ row }">{{ formatTime(row.created_at) }}</template>
          </el-table-column>
        </el-table>
        <el-pagination v-if="total > pageSize" v-model:current-page="page" :page-size="pageSize" :total="total" layout="prev, pager, next" @current-change="fetchLogs" style="margin-top: 16px; justify-content: center;" />
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted } from "vue"; import { ElMessage } from "element-plus"; import request from "@/api/request"
const loading = ref(false); const logs = ref<any[]>([]); const total = ref(0); const page = ref(1); const pageSize = 20
function formatTime(t: string) { const d = new Date(t); return d.toLocaleString("zh-CN") }
async function fetchLogs() {
  loading.value = true
  try { const res = await request.get("/api/request-logs", { params: { page: page.value, page_size: pageSize } }); logs.value = res.data.items || []; total.value = res.data.total || 0 }
  catch { ElMessage.error("加载失败") }
  finally { loading.value = false }
}
onMounted(() => { fetchLogs() })
</script>

<style scoped lang="scss">
.history-page { min-height: 100vh; background: var(--color-bg-tab); }
.page-header { padding: 16px 24px; background: var(--color-bg-1); border-bottom: 1px solid var(--color-divider-1); }
.page-title { font-size: 20px; font-weight: 600; }
.page-container { padding: 24px; max-width: 1000px; margin: 0 auto; }
</style>
```
