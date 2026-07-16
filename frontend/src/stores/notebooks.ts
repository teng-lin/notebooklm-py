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
