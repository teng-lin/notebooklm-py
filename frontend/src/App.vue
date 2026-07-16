<template>
  <div class="app-layout">
    <AppHeader />
    <main class="app-main">
      <router-view />
    </main>
    <LoginDialog :visible="showLogin" @update:visible="showLogin = $event" />
  </div>
</template>

<script setup lang="ts">
import { ref, provide, onMounted } from "vue"
import { useAuthStore } from "@/stores/auth"
import { useTheme } from "@/composables/useTheme"
import AppHeader from "@/components/AppHeader.vue"
import LoginDialog from "@/components/LoginDialog.vue"

useTheme()

const authStore = useAuthStore()
const showLogin = ref(false)

provide("openLogin", () => { showLogin.value = true })

onMounted(() => {
  authStore.restoreSession()
})
</script>

<style lang="scss">
@use "@/styles/global.scss";
@use "@/styles/dark.scss";

*,
*::before,
*::after {
  box-sizing: border-box;
  margin: 0;
  padding: 0;
}

a {
  color: inherit;
  text-decoration: none;
}

button {
  font-family: inherit;
}

.app-layout {
  min-height: 100vh;
  display: flex;
  flex-direction: column;
  background: var(--baoku-bg);
}

.app-main {
  flex: 1;
  padding-top: var(--baoku-header-height);
}
</style>
