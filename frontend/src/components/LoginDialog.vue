<template>
  <el-dialog :model-value="visible" title="" width="720px" :close-on-click-modal="true" :show-close="true" @update:model-value="$emit('update:visible', $event)">
    <div class="login-dialog-body">
      <div class="login-left">
        <div class="login-brand">
          <img src="https://baoku.youdao.com/home/assets/webp/ic_nav_back-BH4W20kS.webp" alt="" class="brand-icon" />
        </div>
        <h2 class="login-title">全域知识，一触即达</h2>
        <p class="login-desc">全领域资料深度理解，<br>让海量碎片资料瞬间转化为结构知识。</p>
        <div class="login-carousel">
          <div class="carousel-placeholder">
            <div class="upload-icon">
              <el-icon :size="24"><Document /></el-icon>
            </div>
            <p class="upload-text">点击或拖拽上传文件</p>
            <p class="upload-hint">支持pdf / doc / docx / jpg<br>jpeg / png / markdown / txt / txt</p>
          </div>
        </div>
      </div>
      <div class="login-right">
        <el-tabs v-model="activeTab" stretch>
          <el-tab-pane label="账号登录" name="account">
            <el-form ref="formRef" :model="form" :rules="rules" label-position="top">
              <el-form-item label="用户名" prop="username">
                <el-input v-model="form.username" placeholder="请输入用户名" />
              </el-form-item>
              <el-form-item label="密码" prop="password">
                <el-input v-model="form.password" type="password" show-password placeholder="请输入密码" />
              </el-form-item>
              <el-form-item>
                <el-button type="primary" class="login-submit" :loading="loading" @click="handleLogin">登 录</el-button>
              </el-form-item>
            </el-form>
          </el-tab-pane>
          <el-tab-pane label="Google 登录" name="google">
            <div class="google-login-tab">
              <div ref="googleBtnRef" class="google-btn-container"></div>
              <p class="google-hint">登录后将自动绑定 Google 账号</p>
            </div>
          </el-tab-pane>
        </el-tabs>
        <div class="login-footer">
          <span>还没有账号？</span>
          <el-button link type="primary" @click="handleRegister">立即注册</el-button>
        </div>
      </div>
    </div>
  </el-dialog>
</template>

<script setup lang="ts">
import { ref, watch, computed } from "vue"
import { useRoute } from "vue-router"
import { ElMessage, type FormInstance, type FormRules } from "element-plus"
import { Document } from "@element-plus/icons-vue"
import { useAuthStore } from "@/stores/auth"
import { useGoogleSignIn } from "@/composables/useGoogleSignIn"

const props = defineProps<{ visible: boolean }>()
const emit = defineEmits<{ "update:visible": [value: boolean] }>()

const authStore = useAuthStore()
const route = useRoute()
const activeTab = ref("account")
const loading = ref(false)
const formRef = ref<FormInstance>()
const form = ref({ username: "", password: "" })
const rules: FormRules = {
  username: [{ required: true, message: "请输入用户名", trigger: "blur" }],
  password: [{ required: true, message: "请输入密码", trigger: "blur" }],
}

const googleBtnRef = ref<HTMLElement>()
const googleReady = ref(false)

async function handleCredential(credential: string) {
  try {
    await authStore.bindGoogle(credential)
    ElMessage.success("Google 登录成功")
    emit("update:visible", false)
  } catch (e: any) {
    ElMessage.error(e.response?.data?.detail || "登录失败")
  }
}

const { renderButton, clientId } = useGoogleSignIn(handleCredential, (err) => {
  ElMessage.error(err)
})

watch([clientId, googleBtnRef, () => props.visible], async () => {
  if (clientId.value && googleBtnRef.value && props.visible && !googleReady.value) {
    await renderButton(googleBtnRef.value)
    googleReady.value = true
  }
})

async function handleLogin() {
  const valid = await formRef.value?.validate().catch(() => false)
  if (!valid) return
  loading.value = true
  try {
    await authStore.login({ username: form.value.username, password: form.value.password })
    ElMessage.success("登录成功")
    emit("update:visible", false)
  } catch (e: any) {
    ElMessage.error(e.response?.data?.detail || "登录失败")
  } finally {
    loading.value = false
  }
}

async function handleRegister() {
  const valid = await formRef.value?.validate().catch(() => false)
  if (!valid) return
  loading.value = true
  try {
    await authStore.register({ username: form.value.username, password: form.value.password })
    ElMessage.success("注册并登录成功")
    emit("update:visible", false)
  } catch (e: any) {
    ElMessage.error(e.response?.data?.detail || "注册失败")
  } finally {
    loading.value = false
  }
}
</script>

<style scoped lang="scss">
.login-dialog-body {
  display: flex;
  min-height: 440px;
}
.login-left {
  width: 360px;
  padding: 40px 32px;
  background: linear-gradient(135deg, #f8f9ff 0%, #f0f4ff 100%);
  border-radius: var(--baoku-radius) 0 0 var(--baoku-radius);
  display: flex;
  flex-direction: column;
  align-items: center;
  text-align: center;
}
.login-brand { margin-bottom: 24px; }
.brand-icon { width: 48px; height: 48px; }
.login-title { font-size: 22px; font-weight: 600; margin-bottom: 12px; }
.login-desc { font-size: 14px; color: var(--baoku-text-2); line-height: 1.6; margin-bottom: 32px; }
.login-carousel {
  flex: 1;
  width: 100%;
  display: flex;
  align-items: center;
  justify-content: center;
}
.carousel-placeholder {
  width: 240px;
  height: 220px;
  border: 1px dashed var(--baoku-border-2);
  border-radius: var(--baoku-radius);
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 12px;
  background: var(--baoku-surface);
}
.upload-icon { color: var(--baoku-primary); }
.upload-text { font-size: 14px; color: var(--baoku-text); }
.upload-hint { font-size: 12px; color: var(--baoku-text-3); line-height: 1.5; }
.login-right {
  flex: 1;
  padding: 32px 40px;
}
.login-submit {
  width: 100%;
  height: 44px;
  background: var(--baoku-text);
  border: none;
  font-size: 16px;
  border-radius: var(--baoku-radius-sm);
}
.login-footer {
  margin-top: 16px;
  text-align: center;
  font-size: 13px;
  color: var(--baoku-text-3);
}
.google-login-tab {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 16px;
  padding: 40px 0;
}
.google-btn-container { min-height: 40px; }
.google-hint { font-size: 12px; color: var(--baoku-text-3); }
</style>
