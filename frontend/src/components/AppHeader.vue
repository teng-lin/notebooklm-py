<template>
  <header class="web-header">
    <div class="header-inner">
      <div class="header-left">
        <router-link to="/" class="logo-area">
          <img src="https://baoku.youdao.com/home/assets/webp/ic_nav_back-BH4W20kS.webp" alt="" class="logo-icon" />
          <img src="https://baoku.youdao.com/home/assets/svg/ic_header-DVVwZcmJ.svg" alt="有道宝库" class="product-name" />
        </router-link>
      </div>
      <div class="header-right">
        <button class="btn-vip" @click="ElMessage.info('会员功能开发中')">
          <span class="vip-label">升级会员</span>
        </button>
        <div class="avatar-wrapper" v-click-outside="closeDropdown">
          <div class="avatar" @click="toggleDropdown">
            <el-avatar :size="32" :src="user?.avatar_url">
              {{ user?.username?.charAt(0)?.toUpperCase() || 'U' }}
            </el-avatar>
          </div>
          <div v-if="dropdownOpen" class="avatar-dropdown">
            <a href="#" class="dropdown-item" @click.prevent="ElMessage.info('开发中')">隐私政策</a>
            <a href="#" class="dropdown-item" @click.prevent="ElMessage.info('开发中')">服务条款</a>
            <a href="#" class="dropdown-item" @click.prevent="ElMessage.info('开发中')">有道宝库cli</a>
            <div class="dropdown-item" @click="ElMessage.info('开发中')">下载电脑版</div>
            <a href="#" class="dropdown-item" @click.prevent="ElMessage.info('开发中')">更新日志</a>
            <div class="dropdown-divider" />
            <div v-if="isAuthenticated" class="dropdown-item dropdown-item--login" @click="handleLogout">退出登录</div>
            <div v-else class="dropdown-item dropdown-item--login" @click="handleLogin">立即登录</div>
          </div>
        </div>
      </div>
    </div>
  </header>
</template>

<script setup lang="ts">
import { ref, inject, computed } from "vue"
import { useRouter } from "vue-router"
import { ElMessage } from "element-plus"
import { useAuthStore } from "@/stores/auth"

const router = useRouter()
const authStore = useAuthStore()
const user = computed(() => authStore.user)
const isAuthenticated = computed(() => authStore.isAuthenticated)
const dropdownOpen = ref(false)
const openLogin = inject<() => void>("openLogin")

function toggleDropdown() { dropdownOpen.value = !dropdownOpen.value }
function closeDropdown() { dropdownOpen.value = false }
function handleLogin() {
  dropdownOpen.value = false
  openLogin?.()
}
async function handleLogout() {
  dropdownOpen.value = false
  await authStore.logout()
  ElMessage.success("已退出")
  router.push("/")
}

const vClickOutside = {
  mounted(el: any, binding: any) {
    el._clickOutside = (e: Event) => { if (!el.contains(e.target as Node)) binding.value() }
    document.addEventListener("click", el._clickOutside)
  },
  unmounted(el: any, binding: any) {
    document.removeEventListener("click", el._clickOutside)
  }
}
</script>

<style scoped lang="scss">
.web-header {
  position: fixed;
  top: 0;
  left: 0;
  right: 0;
  height: var(--baoku-header-height);
  background: var(--baoku-surface);
  border-bottom: 1px solid var(--baoku-border);
  z-index: 1000;
}
.header-inner {
  max-width: 1440px;
  margin: 0 auto;
  height: 100%;
  padding: 0 24px;
  display: flex;
  align-items: center;
  justify-content: space-between;
}
.header-left { display: flex; align-items: center; }
.logo-area {
  display: flex;
  align-items: center;
  gap: 8px;
  cursor: pointer;
}
.logo-icon { width: 32px; height: 32px; }
.product-name { height: 20px; width: auto; }
.header-right { display: flex; align-items: center; gap: 16px; }
.btn-vip {
  height: 32px;
  padding: 0 16px;
  border: none;
  border-radius: 16px;
  background: linear-gradient(90deg, #ffe8aa, #ffd06a);
  color: #7a4a00;
  font-size: 13px;
  font-weight: 500;
  cursor: pointer;
}
.avatar-wrapper { position: relative; }
.avatar { cursor: pointer; }
.avatar-dropdown {
  position: absolute;
  top: 44px;
  right: 0;
  width: 200px;
  background: var(--baoku-surface);
  border-radius: var(--baoku-radius);
  box-shadow: var(--baoku-shadow-card);
  padding: 8px;
}
.dropdown-item {
  display: flex;
  align-items: center;
  height: 39px;
  padding: 0 12px;
  font-size: 14px;
  color: var(--baoku-text);
  border-radius: var(--baoku-radius-sm);
  cursor: pointer;
  &:hover { background: var(--baoku-surface-hover); }
}
.dropdown-divider {
  height: 1px;
  margin: 8px;
  background: var(--baoku-border);
}
.dropdown-item--login { color: var(--baoku-primary); }
</style>
