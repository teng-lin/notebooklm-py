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
