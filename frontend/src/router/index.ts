import { createRouter, createWebHashHistory } from "vue-router"
import type { RouteRecordRaw } from "vue-router"
import { useAuthStore } from "@/stores/auth"

const routes: RouteRecordRaw[] = [
  {
    path: "/",
    name: "Home",
    component: () => import("@/views/HomeView.vue"),
    meta: { requiresAuth: false },
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
  if (to.meta.requiresAuth !== true) {
    next()
    return
  }
  const authStore = useAuthStore()
  if (!authStore.isAuthenticated) {
    next("/")
  } else {
    next()
  }
})

export default router
