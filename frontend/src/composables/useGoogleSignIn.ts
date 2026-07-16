import { ref, onMounted } from "vue"

declare global {
  interface Window {
    google?: {
      accounts: {
        id: {
          initialize: (config: {
            client_id: string
            callback: (response: GoogleCredentialResponse) => void
            auto_select?: boolean
            cancel_on_tap_outside?: boolean
          }) => void
          renderButton: (
            parent: HTMLElement,
            options: {
              theme?: "outline" | "filled_blue" | "filled_black"
              size?: "large" | "medium" | "small"
              text?: "signin_with" | "signup_with" | "continue_with" | "signin"
              shape?: "rectangular" | "pill" | "circle" | "square"
              width?: number
              locale?: string
            },
          ) => void
          prompt: () => void
          disableAutoSelect: () => void
        }
      }
    }
  }
}

interface GoogleCredentialResponse {
  credential: string
  select_by: string
}

export function useGoogleSignIn(onSuccess: (credential: string) => void, onError?: (err: string) => void) {
  const ready = ref(false)
  const clientId = ref<string>("")
  const gisLoaded = ref(false)

  async function fetchClientId() {
    try {
      const res = await fetch("/api/auth/google/client-id")
      const data = await res.json()
      clientId.value = data.client_id || ""
    } catch {
      clientId.value = ""
    }
  }

  function waitForGis(maxRetries = 50): Promise<boolean> {
    return new Promise((resolve) => {
      let retries = 0
      const check = () => {
        if (window.google?.accounts?.id) {
          gisLoaded.value = true
          resolve(true)
        } else if (retries++ < maxRetries) {
          setTimeout(check, 100)
        } else {
          resolve(false)
        }
      }
      check()
    })
  }

  async function renderButton(el: HTMLElement) {
    if (!clientId.value) {
      onError?.("Google Client ID 未配置，请设置 GOOGLE_OAUTH_CLIENT_ID 环境变量")
      return
    }

    const loaded = await waitForGis()
    if (!loaded) {
      onError?.("Google Identity Services 脚本加载超时，请检查网络")
      return
    }

    if (!el) return

    el.innerHTML = ""

    window.google!.accounts.id.initialize({
      client_id: clientId.value,
      callback: (response: GoogleCredentialResponse) => {
        if (response.credential) {
          onSuccess(response.credential)
        } else {
          onError?.("Google 授权失败")
        }
      },
      auto_select: false,
      cancel_on_tap_outside: true,
    })
    window.google!.accounts.id.renderButton(el, {
      theme: "outline",
      size: "large",
      text: "signin_with",
      shape: "rectangular",
      width: 300,
      locale: "zh-CN",
    })
    ready.value = true
  }

  onMounted(() => {
    fetchClientId()
  })

  return { ready, clientId, gisLoaded, renderButton }
}
