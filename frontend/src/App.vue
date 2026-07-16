<template>
  <router-view />
</template>

<script setup lang="ts">
import { onMounted } from "vue"
import { useAuthStore } from "@/stores/auth"
import { useTheme } from "@/composables/useTheme"

useTheme()

const authStore = useAuthStore()

onMounted(() => {
  authStore.restoreSession()
})
</script>

<style lang="scss">
@use "@/styles/dark.scss";

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
  --color_main_1: #ff3650;
  --color_main_2: #17181a;
  --color_text_focus: #1a75ff;
  --color_text_1: #2a2b2e;
  --color_text_2: #626469;
  --color_text_3: #939599;
  --color_text_4: #a8aaad;
  --color_bg_1: #fff;
  --color_bg_2: #fafafa;
  --color_bg_3: #f0f1f2;
  --color_bg_tab: #f5f6f7;
  --color_bg_hover: #f0f1f2;
  --color_divider_1: #eeeff0;
  --color_divider_2: #e0e1e2;
  --color_border_1: #e0e1e2;
  --color_card_bg: #fff;
  --color_card_shadow: 0px 4px 40px 0px rgba(174, 180, 193, 0.17);
  --color_input_bg: #fff;
  --color_input_border: #dcdfe6;
  --border-radius-card: 12px;
  --border-radius-tag: 6px;
  --border-radius-input: 8px;
  --border-radius-button: 6px;
  --border-radius-msg-user: 12px 12px 2px;
  --border-radius-msg-ai: 2px 12px 12px;
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
