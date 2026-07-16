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
