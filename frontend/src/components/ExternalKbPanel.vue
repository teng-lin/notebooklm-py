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

const props = defineProps<{ notebookId: string }>()
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
