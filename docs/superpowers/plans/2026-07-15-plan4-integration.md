# Plan 4: 集成 + 打磨 — 实现计划

> **面向 AI 代理的工作者：** 必需子技能：使用 subagent-driven-development（推荐）或 executing-plans 逐任务实现此计划。步骤使用复选框（`- [ ]`）语法来跟踪进度。

**目标：** 将 Plans 1-3 构建的后端 API 和 Vue 3 前端连接起来，添加暗色主题、响应式布局、集成测试和 Docker 部署支持。

**架构：** 前端 Vite 开发服务器通过代理将 `/api` 请求转发到 FastAPI 后端（端口 8000）。生产环境则由 FastAPI 直接挂载 `frontend/dist` 作为静态资源并提供回退路由以支持 SPA。CORS 中间件仅在开发模式下开放 `localhost:5173`。暗色主题通过 CSS 类切换驱动，偏好持久化到 localStorage。响应式布局基于断点组合媒体查询实现。Docker 采用多阶段构建，先用 Node 编译前端，再用 Python 启动生产服务器。

**技术栈：** Python (FastAPI + uvicorn + SQLAlchemy)，Node (Vue 3 + Vite + TypeScript + Element Plus)，Docker，PostgreSQL

---

## Task 4.1: CORS + Vite 代理 + 静态文件托管

### 4.1.1 — 添加 CORS 中间件到 FastAPI

**文件：** `src/notebooklm/server/server.py`

创建主服务器入口文件（baoku clone 专用，独立于已有的 notebooklm-server）：

```python
"""Baoku clone REST server — multi-user, JWT-authenticated, CORS-enabled.

This server runs separately from the existing ``notebooklm-server`` (which is a
single-user /v1 NotebookLM proxy).  The baoku server provides its own auth,
notebook, source, chat, generation, and external-KB APIs under ``/api/*``.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from starlette.responses import FileResponse

from .database import create_db_and_tables
from .routes import auth, chat, external_kb, generation, notebooks, sources

SERVER_NAME = "baoku-server"

_frontend_dist = os.path.join(os.path.dirname(__file__), "..", "..", "..", "frontend", "dist")
_dev_mode = os.environ.get("BAOKU_DEV", "0") == "1"


def create_app() -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        create_db_and_tables()
        yield

    app = FastAPI(
        title=SERVER_NAME,
        lifespan=lifespan,
        docs_url="/api/docs",
        redoc_url="/api/redoc",
        openapi_url="/api/openapi.json",
    )

    if _dev_mode:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=["http://localhost:5173"],
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )
    else:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=[],
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )

    app.include_router(auth.router, prefix="/api/auth")
    app.include_router(notebooks.router, prefix="/api/notebooks")
    app.include_router(sources.router, prefix="/api/sources")
    app.include_router(chat.router, prefix="/api/chat")
    app.include_router(generation.router, prefix="/api/generation")
    app.include_router(external_kb.router, prefix="/api/external-kb")

    if not _dev_mode and os.path.isdir(_frontend_dist):
        app.mount("/assets", StaticFiles(directory=os.path.join(_frontend_dist, "assets")), name="assets")

        @app.get("/{full_path:path}")
        async def serve_spa(full_path: str) -> FileResponse:
            index = os.path.join(_frontend_dist, "index.html")
            if os.path.isfile(index):
                return FileResponse(index, media_type="text/html")
            return FileResponse(index, media_type="text/html")

    @app.get("/api/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    return app
```

### 4.1.2 — 创建 `__main__.py` 入口

**文件：** `src/notebooklm/server/__init__.py`

确保 `__init__.py` 存在且为空（或已有内容，只需确认）：

```python
```

**文件：** `src/notebooklm/server/__main__.py`

更新以支持 baoku clone 启动方式：

```python
import argparse
import os
import sys

import uvicorn


def main() -> None:
    parser = argparse.ArgumentParser(prog="baoku-server")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--reload", action="store_true")
    parser.add_argument("--dev", action="store_true", help="Enable CORS for localhost:5173")
    args = parser.parse_args()

    if args.dev:
        os.environ["BAOKU_DEV"] = "1"

    uvicorn.run(
        "notebooklm.server.server:create_app",
        factory=True,
        host=args.host,
        port=args.port,
        reload=args.reload,
    )


if __name__ == "__main__":
    main()
```

### 4.1.3 — 配置 Vite 代理

**文件：** `frontend/vite.config.ts`

```typescript
import { defineConfig } from "vite";
import vue from "@vitejs/plugin-vue";
import { resolve } from "path";

export default defineConfig({
  plugins: [vue()],
  resolve: {
    alias: {
      "@": resolve(__dirname, "src"),
    },
  },
  server: {
    port: 5173,
    proxy: {
      "/api": {
        target: "http://localhost:8000",
        changeOrigin: true,
      },
    },
  },
});
```

### 4.1.4 — 创建开发环境变量

**文件：** `frontend/.env.development`

```
VITE_API_BASE=http://localhost:8000
```

**文件：** `frontend/.env.production`

```
VITE_API_BASE=
```

### 4.1.5 — 添加 `uv run build-frontend` 脚本

**文件：** `pyproject.toml`

在 `[project.scripts]` 末尾追加：

```toml
build-frontend = "scripts.build_frontend:main"
```

**文件：** `scripts/build_frontend.py`

```python
"""Build the Vue 3 frontend for production deployment."""

import subprocess
import sys
from pathlib import Path


def main() -> None:
    frontend_dir = Path(__file__).resolve().parent.parent / "frontend"
    if not frontend_dir.is_dir():
        print("frontend/ directory not found — skipping frontend build.")
        sys.exit(0)

    print("=== Installing frontend dependencies ===")
    subprocess.run(["npm", "install"], cwd=str(frontend_dir), check=True)

    print("=== Building frontend ===")
    subprocess.run(["npm", "run", "build"], cwd=str(frontend_dir), check=True)

    dist = frontend_dir / "dist"
    if dist.is_dir():
        print(f"Frontend built successfully: {dist}")
    else:
        print("Frontend build failed: dist/ not found.")
        sys.exit(1)


if __name__ == "__main__":
    main()
```

### ▶ 功能验证 4.1

```bash
# 启动后端
BAOKU_DEV=1 uv run uvicorn notebooklm.server.server:create_app --factory --reload --port 8000 &

# 验证 CORS 头存在
curl -s -o /dev/null -w "%{http_code}" -H "Origin: http://localhost:5173" http://localhost:8000/api/health
# 预期: 200

# 验证 CORS 头
curl -s -D - -H "Origin: http://localhost:5173" http://localhost:8000/api/health 2>&1 | grep -i "access-control-allow-origin"
# 预期: access-control-allow-origin: http://localhost:5173

# 验证未授权请求被拒绝
curl -s -o /dev/null -w "%{http_code}" http://localhost:8000/api/notebooks
# 预期: 401

# 停止后端
kill %1 2>/dev/null || true
```

---

## Task 4.2: 暗色主题

### 4.2.1 — 创建主题组合式函数

**文件：** `frontend/src/composables/useTheme.ts`

```typescript
import { ref, watch } from "vue";

type Theme = "light" | "dark";

const STORAGE_KEY = "baoku-theme";

function getSystemPreference(): Theme {
  if (window.matchMedia("(prefers-color-scheme: dark)").matches) {
    return "dark";
  }
  return "light";
}

function loadTheme(): Theme {
  const stored = localStorage.getItem(STORAGE_KEY);
  if (stored === "light" || stored === "dark") {
    return stored;
  }
  return getSystemPreference();
}

function applyTheme(theme: Theme) {
  if (theme === "dark") {
    document.documentElement.classList.add("dark");
  } else {
    document.documentElement.classList.remove("dark");
  }
}

const currentTheme = ref<Theme>(loadTheme());

applyTheme(currentTheme.value);

watch(currentTheme, (val) => {
  applyTheme(val);
  localStorage.setItem(STORAGE_KEY, val);
});

export function useTheme() {
  function toggle() {
    currentTheme.value = currentTheme.value === "light" ? "dark" : "light";
  }

  function setTheme(theme: Theme) {
    currentTheme.value = theme;
  }

  return {
    theme: currentTheme,
    toggle,
    setTheme,
  };
}
```

### 4.2.2 — 创建暗色主题样式

**文件：** `frontend/src/styles/dark.scss`

```scss
html.dark {
  --color_bg_1: #0b0c0d;
  --color_bg_2: #121314;
  --color_bg_3: #1a1b1e;
  --color_bg_tab: #1e1f22;
  --color_bg_hover: #232426;
  --color_text_1: #e1e3e6;
  --color_text_2: #abacaf;
  --color_text_3: #7a7b7e;
  --color_text_4: #555659;
  --color_divider_1: #232426;
  --color_divider_2: #2c2d30;
  --color_border_1: #2c2d30;
  --color_card_bg: #121314;
  --color_card_shadow: 0px 4px 40px 0px rgba(0, 0, 0, 0.3);
  --color_input_bg: #1a1b1e;
  --color_input_border: #2c2d30;
  --color_main_1: #ff3650;
  --color_main_2: #e1e3e6;
  --color_text_focus: #5c9aff;
  --el-color-primary: #409eff;
  --el-bg-color: #121314;
  --el-bg-color-overlay: #1a1b1e;
  --el-text-color-primary: #e1e3e6;
  --el-text-color-regular: #abacaf;
  --el-border-color: #2c2d30;
  --el-fill-color: #1e1f22;
  --el-fill-color-light: #232426;
  --el-fill-color-lighter: #2c2d30;

  background-color: var(--color_bg_1);
  color: var(--color_text_1);

  .el-card {
    background-color: var(--color_card_bg);
    border-color: var(--color_border_1);
  }

  .el-dialog {
    background-color: var(--color_bg_2);
  }

  .el-input__wrapper {
    background-color: var(--color_input_bg);
    border-color: var(--color_input_border);
    box-shadow: none;
  }

  .el-input__inner {
    color: var(--color_text_1);
  }

  .el-tabs__item {
    color: var(--color_text_2);
    &:hover {
      color: var(--color_text_1);
    }
    &.is-active {
      color: var(--color_text_focus);
    }
  }

  .el-menu {
    background-color: var(--color_bg_2);
    border-color: var(--color_divider_1);
  }

  .el-menu-item {
    color: var(--color_text_2);
    &:hover {
      background-color: var(--color_bg_hover);
    }
    &.is-active {
      color: var(--color_text_focus);
      background-color: var(--color_bg_hover);
    }
  }

  .el-table {
    --el-table-bg-color: var(--color_bg_2);
    --el-table-tr-bg-color: var(--color_bg_2);
    --el-table-header-bg-color: var(--color_bg_3);
    --el-table-border-color: var(--color_divider_2);
    color: var(--color_text_1);
  }

  .el-pagination {
    --el-pagination-bg-color: var(--color_bg_2);
    button:disabled {
      background-color: var(--color_bg_2);
    }
  }

  .el-empty__description p {
    color: var(--color_text_3);
  }

  .baoku-chat-message-user {
    background-color: var(--color_main_1);
    color: #fff;
  }

  .baoku-chat-message-ai {
    background-color: var(--color_bg_3);
    color: var(--color_text_1);
  }

  .baoku-source-card {
    background-color: var(--color_card_bg);
    border-color: var(--color_border_1);
    &:hover {
      border-color: var(--color_text_focus);
    }
  }

  .baoku-nav-sidebar {
    background-color: var(--color_bg_2);
    border-color: var(--color_divider_1);
  }

  .baoku-generation-card {
    background-color: var(--color_card_bg);
    border-color: var(--color_border_1);
    &:hover {
      border-color: var(--color_text_focus);
    }
  }

  .baoku-session-item {
    &:hover {
      background-color: var(--color_bg_hover);
    }
    &.is-active {
      background-color: var(--color_bg_hover);
      border-color: var(--color_text_focus);
    }
  }

  ::-webkit-scrollbar-thumb {
    background-color: var(--color_divider_2);
  }

  ::-webkit-scrollbar-track {
    background-color: var(--color_bg_1);
  }
}
```

### 4.2.3 — 在 App.vue 中加载主题

**文件：** `frontend/src/App.vue`

```vue
<template>
  <router-view />
</template>

<script setup lang="ts">
import { useTheme } from "@/composables/useTheme";

useTheme();
</script>

<style lang="scss">
@use "@/styles/variables.scss";
@use "@/styles/global.scss";
@use "@/styles/dark.scss";
</style>
```

### 4.2.4 — 创建全局变量文件

**文件：** `frontend/src/styles/variables.scss`

```scss
:root {
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
  --el-color-primary: #409eff;
  --el-bg-color: #fff;
  --el-bg-color-overlay: #fff;
  --el-text-color-primary: #2a2b2e;
  --el-text-color-regular: #626469;
  --el-border-color: #dcdfe6;
  --el-fill-color: #f5f6f7;
  --el-fill-color-light: #f0f1f2;
  --el-fill-color-lighter: #eef0f1;
  --font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC",
    "Hiragino Sans GB", "Microsoft YaHei", sans-serif;
  --border-radius-card: 12px;
  --border-radius-tag: 6px;
  --border-radius-input: 8px;
  --border-radius-button: 6px;
  --border-radius-msg-user: 12px 12px 2px;
  --border-radius-msg-ai: 2px 12px 12px;
}
```

### 4.2.5 — 更新 SettingsView 添加主题切换

**文件：** `frontend/src/views/SettingsView.vue`

```vue
<template>
  <div class="settings-view">
    <div class="settings-container">
      <h1 class="settings-title">设置</h1>

      <el-card shadow="never" class="settings-card">
        <template #header>
          <span>外观</span>
        </template>
        <div class="setting-row">
          <div class="setting-info">
            <div class="setting-label">主题模式</div>
            <div class="setting-desc">切换亮色/暗色主题</div>
          </div>
          <div class="setting-control">
            <el-switch
              :model-value="theme === 'dark'"
              @change="toggle"
              active-text="暗色"
              inactive-text="亮色"
            />
          </div>
        </div>
      </el-card>

      <el-card shadow="never" class="settings-card">
        <template #header>
          <span>账户</span>
        </template>
        <div class="setting-row">
          <div class="setting-info">
            <div class="setting-label">当前用户</div>
            <div class="setting-desc">{{ user?.username || "未登录" }}</div>
          </div>
          <div class="setting-control">
            <el-button type="danger" plain @click="handleLogout">退出登录</el-button>
          </div>
        </div>
      </el-card>
    </div>
  </div>
</template>

<script setup lang="ts">
import { useTheme } from "@/composables/useTheme";
import { useAuthStore } from "@/stores/auth";
import { useRouter } from "vue-router";

const { theme, toggle } = useTheme();
const auth = useAuthStore();
const router = useRouter();
const user = auth.user;

function handleLogout() {
  auth.logout();
  router.push("/login");
}
</script>

<style scoped lang="scss">
.settings-view {
  max-width: 720px;
  margin: 0 auto;
  padding: 32px 16px;
}

.settings-title {
  font-size: 24px;
  font-weight: 600;
  margin-bottom: 24px;
  color: var(--color_text_1);
}

.settings-card {
  margin-bottom: 16px;
  border-radius: var(--border-radius-card);
}

.setting-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 8px 0;
}

.setting-label {
  font-size: 15px;
  font-weight: 500;
  color: var(--color_text_1);
}

.setting-desc {
  font-size: 13px;
  color: var(--color_text_3);
  margin-top: 2px;
}
</style>
```

### ▶ 功能验证 4.2

```bash
# 验证暗色主题 SCSS 编译正确
cd frontend && npx sass src/styles/dark.scss /dev/null --no-source-map 2>&1 || echo "sass check done"

# 验证 useTheme composable 语法
cd frontend && npx vue-tsc --noEmit src/composables/useTheme.ts 2>&1 || true
# 预期: 无类型错误
```

---

## Task 4.3: 响应式布局

### 4.3.1 — 创建断点组合式函数

**文件：** `frontend/src/composables/useBreakpoint.ts`

```typescript
import { ref, computed, onMounted, onUnmounted } from "vue";

type Breakpoint = "sm" | "md" | "lg" | "xl";

const BREAKPOINTS: Record<Breakpoint, number> = {
  sm: 640,
  md: 768,
  lg: 1024,
  xl: 1280,
};

function resolveBreakpoint(width: number): Breakpoint {
  if (width < BREAKPOINTS.sm) return "sm";
  if (width < BREAKPOINTS.md) return "md";
  if (width < BREAKPOINTS.lg) return "lg";
  return "xl";
}

const width = ref(1024);

let mqls: MediaQueryList[] = [];
let handlers: (() => void)[] = [];

export function useBreakpoint() {
  const breakpoint = computed(() => resolveBreakpoint(width.value));

  const isMobile = computed(() => breakpoint.value === "sm");
  const isTablet = computed(() => breakpoint.value === "md");
  const isDesktop = computed(() => breakpoint.value === "lg" || breakpoint.value === "xl");

  function onResize() {
    width.value = window.innerWidth;
  }

  onMounted(() => {
    width.value = window.innerWidth;
    window.addEventListener("resize", onResize);

    for (const [, bp] of Object.entries(BREAKPOINTS)) {
      const mql = window.matchMedia(`(min-width: ${bp}px)`);
      mqls.push(mql);
      const handler = () => onResize();
      mql.addEventListener("change", handler);
      handlers.push(handler);
    }
  });

  onUnmounted(() => {
    window.removeEventListener("resize", onResize);
    for (let i = 0; i < mqls.length; i++) {
      mqls[i].removeEventListener("change", handlers[i]);
    }
    mqls = [];
    handlers = [];
  });

  return {
    breakpoint,
    width,
    isMobile,
    isTablet,
    isDesktop,
  };
}
```

### 4.3.2 — 更新全局样式添加响应式 mixins

**文件：** `frontend/src/styles/global.scss`

```scss
@use "variables";

* {
  margin: 0;
  padding: 0;
  box-sizing: border-box;
}

html {
  font-family: var(--font-family);
  -webkit-font-smoothing: antialiased;
  -moz-osx-font-smoothing: grayscale;
}

body {
  background-color: var(--color_bg_1);
  color: var(--color_text_1);
  min-height: 100vh;
}

a {
  color: var(--color_text_focus);
  text-decoration: none;
}

/* === Responsive mixins — use via @include in component <style> === */

@mixin respond-to($bp) {
  @if $bp == sm {
    @media (max-width: 639px) { @content; }
  } @else if $bp == md {
    @media (min-width: 640px) and (max-width: 1023px) { @content; }
  } @else if $bp == lg {
    @media (min-width: 1024px) { @content; }
  }
}

@mixin mobile-only {
  @media (max-width: 639px) { @content; }
}

@mixin tablet-up {
  @media (min-width: 640px) { @content; }
}

@mixin desktop-up {
  @media (min-width: 1024px) { @content; }
}

/* === Grid responsive helpers === */

.baoku-grid-3 {
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: 16px;
  @media (max-width: 1023px) {
    grid-template-columns: repeat(2, 1fr);
  }
  @media (max-width: 639px) {
    grid-template-columns: 1fr;
  }
}

.baoku-grid-2 {
  display: grid;
  grid-template-columns: repeat(2, 1fr);
  gap: 16px;
  @media (max-width: 639px) {
    grid-template-columns: 1fr;
  }
}

.baoku-flex-row {
  display: flex;
  flex-direction: row;
  @media (max-width: 639px) {
    flex-direction: column;
  }
}

/* === Full-screen dialog on mobile === */
@media (max-width: 639px) {
  .el-dialog {
    width: 100% !important;
    max-width: 100% !important;
    margin: 0 !important;
    height: 100%;
    max-height: 100%;
    border-radius: 0 !important;
    .el-dialog__header {
      padding: 16px;
    }
    .el-dialog__body {
      padding: 16px;
    }
    .el-dialog__footer {
      padding: 16px;
    }
  }
}

/* === Bottom navigation on mobile === */
.baoku-mobile-bottom-nav {
  display: none;
  @media (max-width: 639px) {
    display: flex;
    position: fixed;
    bottom: 0;
    left: 0;
    right: 0;
    z-index: 1000;
    background: var(--color_bg_2);
    border-top: 1px solid var(--color_divider_1);
    padding: 8px 0;
    padding-bottom: max(8px, env(safe-area-inset-bottom));
    justify-content: space-around;
  }
}

/* === Slide-over drawer on mobile === */
.baoku-drawer-overlay {
  @media (max-width: 639px) {
    position: fixed;
    inset: 0;
    background: rgba(0, 0, 0, 0.5);
    z-index: 900;
  }
}

.baoku-drawer {
  @media (max-width: 639px) {
    position: fixed;
    top: 0;
    left: 0;
    bottom: 0;
    width: 280px;
    max-width: 80vw;
    z-index: 1000;
    background: var(--color_bg_2);
    box-shadow: 2px 0 12px rgba(0, 0, 0, 0.15);
    overflow-y: auto;
    transform: translateX(-100%);
    transition: transform 0.2s ease;
    &.is-open {
      transform: translateX(0);
    }
  }
}

/* === Horizontal scroll for content type cards === */
.baoku-horizontal-scroll {
  @media (max-width: 639px) {
    display: flex;
    overflow-x: auto;
    gap: 12px;
    padding: 8px 0;
    scroll-snap-type: x mandatory;
    -webkit-overflow-scrolling: touch;
    > * {
      flex-shrink: 0;
      width: 160px;
      scroll-snap-align: start;
    }
    &::-webkit-scrollbar {
      display: none;
    }
  }
}

/* === Empty state centering === */
.baoku-empty-state {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  min-height: 300px;
  color: var(--color_text_3);
  .baoku-empty-icon {
    font-size: 64px;
    margin-bottom: 16px;
    opacity: 0.6;
  }
  .baoku-empty-text {
    font-size: 15px;
  }
}
```

### 4.3.3 — 更新 HomeView 响应式卡片网格

**文件：** `frontend/src/views/HomeView.vue`

```vue
<template>
  <div class="home-view">
    <header class="home-header">
      <h1 class="home-title">知识库</h1>
      <div class="home-actions">
        <el-input
          v-model="searchQuery"
          placeholder="搜索知识库..."
          prefix-icon="Search"
          clearable
          class="search-input"
        />
        <el-button type="primary" @click="showCreateDialog = true">
          <el-icon><Plus /></el-icon>
          新建知识库
        </el-button>
      </div>
    </header>

    <div v-if="loading" class="baoku-empty-state">
      <el-icon class="baoku-empty-icon" :size="64"><Loading /></el-icon>
      <span class="baoku-empty-text">加载中...</span>
    </div>

    <div v-else-if="filteredNotebooks.length === 0" class="baoku-empty-state">
      <el-icon class="baoku-empty-icon" :size="64"><Folder /></el-icon>
      <span class="baoku-empty-text">还没有知识库，点击右上角创建一个</span>
    </div>

    <div v-else class="baoku-grid-3">
      <div
        v-for="nb in filteredNotebooks"
        :key="nb.id"
        class="notebook-card"
        @click="router.push(`/notebook/${nb.id}`)"
      >
        <div class="notebook-card-header">
          <h3 class="notebook-card-title">{{ nb.title }}</h3>
          <el-tag v-if="nb.source_count > 0" size="small" type="info">
            {{ nb.source_count }} 份资料
          </el-tag>
        </div>
        <p v-if="nb.description" class="notebook-card-desc">{{ nb.description }}</p>
        <div class="notebook-card-meta">
          <span>{{ nb.chat_count || 0 }} 次问答</span>
          <span>更新于 {{ formatTime(nb.updated_at) }}</span>
        </div>
      </div>
    </div>

    <el-dialog v-model="showCreateDialog" title="新建知识库" width="480px">
      <el-form ref="formRef" :model="createForm" :rules="rules" label-position="top">
        <el-form-item label="知识库名称" prop="title">
          <el-input v-model="createForm.title" placeholder="输入知识库名称" maxlength="100" />
        </el-form-item>
        <el-form-item label="描述（可选）" prop="description">
          <el-input
            v-model="createForm.description"
            type="textarea"
            :rows="3"
            placeholder="简短描述这个知识库的用途"
            maxlength="500"
          />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="showCreateDialog = false">取消</el-button>
        <el-button type="primary" :loading="creating" @click="handleCreate">创建</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted } from "vue";
import { useRouter } from "vue-router";
import { Plus, Search, Folder, Loading } from "@element-plus/icons-vue";
import { ElMessage } from "element-plus";
import { api } from "@/api";

interface Notebook {
  id: number;
  title: string;
  description: string | null;
  source_count: number;
  chat_count: number;
  updated_at: string;
  created_at: string;
}

const router = useRouter();
const notebooks = ref<Notebook[]>([]);
const loading = ref(true);
const searchQuery = ref("");
const showCreateDialog = ref(false);
const creating = ref(false);
const createForm = ref({ title: "", description: "" });
const rules = {
  title: [{ required: true, message: "请输入知识库名称", trigger: "blur" }],
};

const filteredNotebooks = computed(() => {
  if (!searchQuery.value) return notebooks.value;
  const q = searchQuery.value.toLowerCase();
  return notebooks.value.filter(
    (nb) =>
      nb.title.toLowerCase().includes(q) ||
      (nb.description && nb.description.toLowerCase().includes(q))
  );
});

function formatTime(ts: string) {
  const d = new Date(ts);
  const now = new Date();
  const diff = now.getTime() - d.getTime();
  if (diff < 3600000) return `${Math.floor(diff / 60000)} 分钟前`;
  if (diff < 86400000) return `${Math.floor(diff / 3600000)} 小时前`;
  return d.toLocaleDateString("zh-CN");
}

async function loadNotebooks() {
  try {
    const res = await api.getNotebooks();
    notebooks.value = res.data;
  } catch {
    ElMessage.error("加载知识库失败");
  } finally {
    loading.value = false;
  }
}

async function handleCreate() {
  creating.value = true;
  try {
    const res = await api.createNotebook(createForm.value.title, createForm.value.description);
    notebooks.value.unshift(res.data);
    showCreateDialog.value = false;
    createForm.value = { title: "", description: "" };
    ElMessage.success("创建成功");
  } catch {
    ElMessage.error("创建失败");
  } finally {
    creating.value = false;
  }
}

onMounted(loadNotebooks);
</script>

<style scoped lang="scss">
.home-view {
  max-width: 1200px;
  margin: 0 auto;
  padding: 32px 16px 80px;
}

.home-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 24px;
  flex-wrap: wrap;
  gap: 12px;
}

.home-title {
  font-size: 24px;
  font-weight: 600;
  color: var(--color_text_1);
}

.home-actions {
  display: flex;
  align-items: center;
  gap: 12px;
}

.search-input {
  width: 240px;
  @media (max-width: 639px) {
    width: 100%;
  }
}

.notebook-card {
  background: var(--color_card_bg);
  border: 1px solid var(--color_border_1);
  border-radius: var(--border-radius-card);
  padding: 20px;
  cursor: pointer;
  transition: all 0.2s ease;
  box-shadow: var(--color_card_shadow);
  &:hover {
    transform: translateY(-2px);
    box-shadow: 0 6px 20px rgba(0, 0, 0, 0.1);
  }
}

.notebook-card-header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 8px;
  margin-bottom: 8px;
}

.notebook-card-title {
  font-size: 16px;
  font-weight: 600;
  color: var(--color_text_1);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.notebook-card-desc {
  font-size: 13px;
  color: var(--color_text_3);
  margin-bottom: 12px;
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
}

.notebook-card-meta {
  display: flex;
  justify-content: space-between;
  font-size: 12px;
  color: var(--color_text_4);
}
</style>
```

### 4.3.4 — 更新 NotebookView 响应式布局

**文件：** `frontend/src/views/NotebookView.vue`

```vue
<template>
  <div class="notebook-view">
    <header class="notebook-header">
      <el-button text @click="router.push('/')">
        <el-icon><ArrowLeft /></el-icon>
        返回
      </el-button>
      <h2 class="notebook-title">{{ notebook?.title || "加载中..." }}</h2>
    </header>

    <el-tabs
      v-model="activeTab"
      class="notebook-tabs"
      @tab-click="handleTabClick"
    >
      <el-tab-pane label="概览" name="overview" />
      <el-tab-pane label="资料" name="sources" />
      <el-tab-pane label="问答" name="chat" />
      <el-tab-pane label="生成" name="generate" />
    </el-tabs>

    <div class="notebook-content">
      <OverviewTab v-if="activeTab === 'overview'" :notebook-id="id" />
      <SourcesTab v-if="activeTab === 'sources'" :notebook-id="id" />
      <ChatTab v-if="activeTab === 'chat'" :notebook-id="id" />
      <GenerateTab v-if="activeTab === 'generate'" :notebook-id="id" />
    </div>

    <nav class="baoku-mobile-bottom-nav">
      <el-button
        v-for="tab in tabs"
        :key="tab.name"
        :type="activeTab === tab.name ? 'primary' : 'text'"
        @click="activeTab = tab.name"
      >
        <el-icon><component :is="tab.icon" /></el-icon>
        <span>{{ tab.label }}</span>
      </el-button>
    </nav>
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted } from "vue";
import { useRoute, useRouter } from "vue-router";
import { ArrowLeft, InfoFilled, Document, ChatDotSquare, MagicStick } from "@element-plus/icons-vue";
import { ElMessage } from "element-plus";
import { api } from "@/api";
import OverviewTab from "@/views/notebook/OverviewTab.vue";
import SourcesTab from "@/views/notebook/SourcesTab.vue";
import ChatTab from "@/views/notebook/ChatTab.vue";
import GenerateTab from "@/views/notebook/GenerateTab.vue";

const route = useRoute();
const router = useRouter();
const id = Number(route.params.id);
const activeTab = ref("overview");
const notebook = ref<{ title: string } | null>(null);

const tabs = [
  { name: "overview", label: "概览", icon: InfoFilled },
  { name: "sources", label: "资料", icon: Document },
  { name: "chat", label: "问答", icon: ChatDotSquare },
  { name: "generate", label: "生成", icon: MagicStick },
];

function handleTabClick() {
  const tabName = activeTab.value;
  router.replace(`/notebook/${id}?tab=${tabName}`);
}

async function loadNotebook() {
  try {
    const res = await api.getNotebook(id);
    notebook.value = res.data;
  } catch {
    ElMessage.error("加载知识库失败");
  }
}

onMounted(() => {
  const tab = route.query.tab as string | undefined;
  if (tab && ["overview", "sources", "chat", "generate"].includes(tab)) {
    activeTab.value = tab;
  }
  loadNotebook();
});
</script>

<style scoped lang="scss">
.notebook-view {
  max-width: 1200px;
  margin: 0 auto;
  padding: 16px;
  padding-bottom: 80px;
}

.notebook-header {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-bottom: 16px;
}

.notebook-title {
  font-size: 20px;
  font-weight: 600;
  color: var(--color_text_1);
}

.notebook-tabs {
  :deep(.el-tabs__header) {
    margin-bottom: 16px;
  }
  @media (max-width: 639px) {
    display: none;
  }
}

.notebook-content {
  min-height: 400px;
}
</style>
```

### 4.3.5 — 更新 ChatTab 移动端侧边栏

**文件：** `frontend/src/views/notebook/ChatTab.vue`

```vue
<template>
  <div class="chat-layout">
    <!-- Desktop sidebar -->
    <aside class="chat-sidebar">
      <div class="chat-sidebar-header">
        <h3>对话历史</h3>
        <el-button size="small" type="primary" @click="startNewChat">
          <el-icon><Plus /></el-icon>
          新对话
        </el-button>
      </div>
      <div v-if="sessions.length === 0" class="sidebar-empty">暂无对话</div>
      <div
        v-for="s in sessions"
        :key="s.id"
        class="baoku-session-item"
        :class="{ 'is-active': s.id === activeSessionId }"
        @click="selectSession(s.id)"
      >
        <div class="session-title">{{ s.title || "新对话" }}</div>
        <div class="session-meta">{{ s.message_count }} 条消息</div>
      </div>
    </aside>

    <!-- Mobile drawer trigger -->
    <el-button
      class="mobile-drawer-trigger"
      size="small"
      @click="drawerOpen = true"
    >
      <el-icon><ChatDotSquare /></el-icon>
      会话列表
    </el-button>

    <!-- Mobile drawer overlay -->
    <div
      v-if="drawerOpen"
      class="baoku-drawer-overlay"
      @click="drawerOpen = false"
    />

    <!-- Mobile drawer -->
    <aside class="baoku-drawer" :class="{ 'is-open': drawerOpen }">
      <div class="chat-sidebar-header">
        <h3>对话历史</h3>
        <el-button text @click="drawerOpen = false">
          <el-icon><Close /></el-icon>
        </el-button>
      </div>
      <el-button size="small" type="primary" class="new-chat-btn" @click="startNewChat">
        <el-icon><Plus /></el-icon>
        新对话
      </el-button>
      <div
        v-for="s in sessions"
        :key="s.id"
        class="baoku-session-item"
        :class="{ 'is-active': s.id === activeSessionId }"
        @click="selectSession(s.id); drawerOpen = false"
      >
        <div class="session-title">{{ s.title || "新对话" }}</div>
        <div class="session-meta">{{ s.message_count }} 条消息</div>
      </div>
    </aside>

    <!-- Main chat area -->
    <main class="chat-main">
      <div v-if="!activeSessionId" class="baoku-empty-state">
        <el-icon class="baoku-empty-icon" :size="64"><ChatDotSquare /></el-icon>
        <span class="baoku-empty-text">选择一个对话或创建一个新对话</span>
      </div>
      <div v-else class="chat-messages" ref="messagesRef">
        <div
          v-for="msg in messages"
          :key="msg.id"
          class="chat-message"
          :class="msg.role === 'user' ? 'baoku-chat-message-user' : 'baoku-chat-message-ai'"
        >
          <div class="message-content">{{ msg.content }}</div>
          <div v-if="msg.citations && msg.citations.length > 0" class="message-citations">
            <span
              v-for="(c, i) in msg.citations"
              :key="i"
              class="citation-tag"
              @click="scrollToSource(c.source_id)"
            >
              {{ i + 1 }}
            </span>
          </div>
        </div>
        <div v-if="sending" class="chat-message baoku-chat-message-ai">
          <div class="message-content">
            <el-icon class="is-loading"><Loading /></el-icon>
            思考中...
          </div>
        </div>
      </div>

      <div v-if="activeSessionId" class="chat-input-area">
        <el-input
          v-model="inputText"
          type="textarea"
          :rows="2"
          placeholder="输入你的问题..."
          @keydown.enter.ctrl="sendMessage"
        />
        <el-button type="primary" :loading="sending" @click="sendMessage">
          发送
        </el-button>
      </div>
    </main>
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted, watch } from "vue";
import { Plus, ChatDotSquare, Close, Loading } from "@element-plus/icons-vue";
import { ElMessage } from "element-plus";
import { api } from "@/api";

const props = defineProps<{ notebookId: number }>();

const sessions = ref<any[]>([]);
const activeSessionId = ref<number | null>(null);
const messages = ref<any[]>([]);
const inputText = ref("");
const sending = ref(false);
const drawerOpen = ref(false);
const messagesRef = ref<HTMLElement | null>(null);

async function loadSessions() {
  try {
    const res = await api.getChatSessions(props.notebookId);
    sessions.value = res.data;
  } catch {
    // ignore
  }
}

async function selectSession(id: number) {
  activeSessionId.value = id;
  try {
    const res = await api.getChatMessages(id);
    messages.value = res.data;
  } catch {
    ElMessage.error("加载消息失败");
  }
}

async function sendMessage() {
  if (!inputText.value.trim() || !activeSessionId.value) return;
  const text = inputText.value;
  inputText.value = "";
  sending.value = true;

  messages.value.push({ id: Date.now(), role: "user", content: text });

  try {
    const res = await api.sendChatMessage(activeSessionId.value, text);
    messages.value.push(res.data);
  } catch {
    ElMessage.error("发送失败");
  } finally {
    sending.value = false;
    setTimeout(() => {
      if (messagesRef.value) {
        messagesRef.value.scrollTop = messagesRef.value.scrollHeight;
      }
    }, 100);
  }
}

async function startNewChat() {
  try {
    const res = await api.createChatSession(props.notebookId, "新对话");
    sessions.value.unshift(res.data);
    activeSessionId.value = res.data.id;
    messages.value = [];
  } catch {
    ElMessage.error("创建对话失败");
  }
}

function scrollToSource(sourceId: string) {
  // placeholder — would emit or navigate
}

onMounted(loadSessions);
</script>

<style scoped lang="scss">
.chat-layout {
  display: flex;
  gap: 16px;
  height: calc(100vh - 200px);
  @media (max-width: 639px) {
    flex-direction: column;
    height: calc(100vh - 280px);
  }
}

.chat-sidebar {
  width: 260px;
  flex-shrink: 0;
  border-right: 1px solid var(--color_divider_1);
  padding-right: 16px;
  overflow-y: auto;
  @media (max-width: 639px) {
    display: none;
  }
}

.chat-sidebar-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 12px;
  h3 {
    font-size: 15px;
    font-weight: 600;
    color: var(--color_text_1);
  }
}

.sidebar-empty {
  color: var(--color_text_4);
  font-size: 13px;
  text-align: center;
  padding: 32px 0;
}

.baoku-session-item {
  padding: 10px 12px;
  border-radius: 8px;
  cursor: pointer;
  margin-bottom: 4px;
  border-left: 3px solid transparent;
  &.is-active {
    background: var(--color_bg_hover);
    border-color: var(--color_text_focus);
  }
  &:hover {
    background: var(--color_bg_hover);
  }
}

.session-title {
  font-size: 14px;
  font-weight: 500;
  color: var(--color_text_1);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.session-meta {
  font-size: 12px;
  color: var(--color_text_4);
  margin-top: 2px;
}

.chat-main {
  flex: 1;
  display: flex;
  flex-direction: column;
  min-width: 0;
}

.chat-messages {
  flex: 1;
  overflow-y: auto;
  padding: 16px 0;
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.chat-message {
  max-width: 75%;
  padding: 12px 16px;
  line-height: 1.6;
  font-size: 14px;
}

.baoku-chat-message-user {
  align-self: flex-end;
  border-radius: var(--border-radius-msg-user);
  background-color: var(--color_main_1);
  color: #fff;
}

.baoku-chat-message-ai {
  align-self: flex-start;
  border-radius: var(--border-radius-msg-ai);
  background-color: var(--color_bg_3);
  color: var(--color_text_1);
}

.message-content {
  white-space: pre-wrap;
  word-break: break-word;
}

.message-citations {
  margin-top: 8px;
  display: flex;
  gap: 4px;
  flex-wrap: wrap;
}

.citation-tag {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 22px;
  height: 22px;
  border-radius: 50%;
  background: rgba(255, 255, 255, 0.2);
  font-size: 11px;
  cursor: pointer;
  &:hover {
    background: rgba(255, 255, 255, 0.35);
  }
}

.chat-input-area {
  display: flex;
  gap: 8px;
  padding-top: 12px;
  border-top: 1px solid var(--color_divider_1);
}

.mobile-drawer-trigger {
  display: none;
  @media (max-width: 639px) {
    display: inline-flex;
    margin-bottom: 8px;
  }
}

.new-chat-btn {
  width: 100%;
  margin-bottom: 8px;
}
</style>
```

### 4.3.6 — 更新 GenerateTab 内容类型卡片水平滚动

**文件：** `frontend/src/views/notebook/GenerateTab.vue`

```vue
<template>
  <div class="generate-tab">
    <h3 class="generate-title">选择要生成的内容类型</h3>

    <div class="baoku-horizontal-scroll content-type-grid">
      <div
        v-for="type in contentTypes"
        :key="type.id"
        class="content-type-card"
        :class="{ selected: selectedType === type.id }"
        @click="selectedType = type.id"
      >
        <el-icon :size="32"><component :is="type.icon" /></el-icon>
        <div class="type-name">{{ type.name }}</div>
        <div class="type-desc">{{ type.desc }}</div>
      </div>
    </div>

    <div v-if="selectedType" class="generation-config">
      <el-form label-position="top">
        <el-form-item label="生成指令">
          <el-input
            v-model="prompt"
            type="textarea"
            :rows="4"
            placeholder="描述你想要生成的内容..."
          />
        </el-form-item>
        <el-form-item label="模板">
          <el-select v-model="selectedTemplate" placeholder="选择模板">
            <el-option
              v-for="t in templates"
              :key="t.id"
              :label="t.name"
              :value="t.id"
            />
          </el-select>
        </el-form-item>
        <el-button type="primary" :loading="generating" @click="handleGenerate">
          <el-icon><MagicStick /></el-icon>
          开始生成
        </el-button>
      </el-form>
    </div>

    <div v-if="generatedItems.length > 0" class="generation-history">
      <h4>生成历史</h4>
      <div v-for="item in generatedItems" :key="item.id" class="generation-item">
        <div class="generation-item-info">
          <span class="item-type">{{ getTypeName(item.content_type) }}</span>
          <span class="item-title">{{ item.title || "未命名" }}</span>
        </div>
        <el-tag :type="item.status === 'completed' ? 'success' : 'warning'" size="small">
          {{ item.status === "completed" ? "已完成" : "处理中" }}
        </el-tag>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, computed } from "vue";
import {
  MagicStick,
  Document,
  Share,
  Picture,
  VideoCamera,
  Headset,
  TrendCharts,
} from "@element-plus/icons-vue";
import { ElMessage } from "element-plus";
import { api } from "@/api";

const props = defineProps<{ notebookId: number }>();

const contentTypes = [
  { id: "document", name: "文档", desc: "笔记、摘要、FAQ", icon: Document },
  { id: "podcast", name: "播客", desc: "AI 双人对话音频", icon: Headset },
  { id: "ppt", name: "PPT", desc: "结构化幻灯片", icon: Share },
  { id: "mindmap", name: "脑图", desc: "思维导图", icon: TrendCharts },
  { id: "infographic", name: "信息图", desc: "图文信息图", icon: Picture },
  { id: "video", name: "视频", desc: "短视频生成", icon: VideoCamera },
];

const selectedType = ref<string | null>(null);
const prompt = ref("");
const selectedTemplate = ref("");
const generating = ref(false);
const generatedItems = ref<any[]>([]);
const templates = computed(() => {
  const map: Record<string, { id: string; name: string }[]> = {
    document: [
      { id: "summary", name: "摘要" },
      { id: "notes", name: "详细笔记" },
      { id: "faq", name: "FAQ" },
      { id: "study-guide", name: "学习指南" },
    ],
    podcast: [{ id: "default", name: "双人对话" }],
    ppt: [
      { id: "business", name: "商务" },
      { id: "academic", name: "学术" },
      { id: "creative", name: "创意" },
    ],
    mindmap: [
      { id: "tree", name: "树形" },
      { id: "radial", name: "辐射" },
    ],
    infographic: [
      { id: "timeline", name: "时间线" },
      { id: "comparison", name: "对比" },
    ],
    video: [
      { id: "explainer", name: "解说" },
      { id: "tutorial", name: "教程" },
    ],
  };
  return map[selectedType.value || ""] || [];
});

function getTypeName(type: string) {
  return contentTypes.find((c) => c.id === type)?.name || type;
}

async function handleGenerate() {
  if (!selectedType.value || !prompt.value.trim()) {
    ElMessage.warning("请选择内容类型并输入指令");
    return;
  }
  generating.value = true;
  try {
    const res = await api.generateContent(props.notebookId, {
      content_type: selectedType.value,
      prompt: prompt.value,
      template: selectedTemplate.value || undefined,
    });
    generatedItems.value.unshift(res.data);
    ElMessage.success("生成已开始");
  } catch {
    ElMessage.error("生成失败");
  } finally {
    generating.value = false;
  }
}
</script>

<style scoped lang="scss">
.generate-tab {
  max-width: 800px;
}

.generate-title {
  font-size: 18px;
  font-weight: 600;
  margin-bottom: 16px;
  color: var(--color_text_1);
}

.content-type-grid {
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: 12px;
  margin-bottom: 24px;
}

.content-type-card {
  display: flex;
  flex-direction: column;
  align-items: center;
  padding: 20px 12px;
  border-radius: var(--border-radius-card);
  border: 1px solid var(--color_border_1);
  cursor: pointer;
  transition: all 0.2s;
  color: var(--color_text_2);
  &:hover {
    border-color: var(--color_text_focus);
    color: var(--color_text_focus);
  }
  &.selected {
    border-color: var(--color_text_focus);
    background: rgba(26, 117, 255, 0.06);
    color: var(--color_text_focus);
  }
}

.type-name {
  font-size: 14px;
  font-weight: 500;
  margin-top: 8px;
}

.type-desc {
  font-size: 12px;
  margin-top: 4px;
  color: var(--color_text_4);
}

.generation-config {
  margin-bottom: 24px;
}

.generation-history {
  h4 {
    font-size: 15px;
    font-weight: 600;
    margin-bottom: 12px;
    color: var(--color_text_1);
  }
}

.generation-item {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 10px 12px;
  border-radius: 8px;
  border: 1px solid var(--color_border_1);
  margin-bottom: 8px;
}

.generation-item-info {
  display: flex;
  gap: 8px;
  align-items: center;
}

.item-type {
  font-size: 12px;
  padding: 2px 6px;
  border-radius: 4px;
  background: var(--color_bg_tab);
  color: var(--color_text_3);
}

.item-title {
  font-size: 14px;
  color: var(--color_text_1);
}
</style>
```

### ▶ 功能验证 4.3

```bash
# 验证全局 SCSS 编译
cd frontend && npx sass src/styles/global.scss /dev/null --no-source-map 2>&1

# 验证断点 composable 类型
cd frontend && npx vue-tsc --noEmit src/composables/useBreakpoint.ts 2>&1 || true
```

---

## Task 4.4: API 集成测试

### 4.4.1 — 创建集成测试文件

**文件：** `tests/server/test_api_integration.py`

```python
"""API integration tests for the baoku clone server.

Tests run against the FastAPI TestClient with an in-memory SQLite database.
"""

from __future__ import annotations

import json
import os
from typing import Any, Iterator

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("sqlalchemy")

from fastapi.testclient import TestClient

from notebooklm.server.database import (
    _engine,
    create_db_and_tables,
    get_session,
)
from notebooklm.server.server import create_app


TEST_DB = ":memory:"

_orig_db_url: str | None = None


@pytest.fixture(autouse=True)
def _test_db(monkeypatch: pytest.MonkeyPatch) -> None:
    global _orig_db_url
    import notebooklm.server.database as db_mod

    if _orig_db_url is None:
        _orig_db_url = os.environ.get("BAOKU_DATABASE_URL", "")
    monkeypatch.setenv("BAOKU_DATABASE_URL", TEST_DB)
    db_mod._engine = db_mod._create_engine(TEST_DB)
    create_db_and_tables()
    yield
    # Drop all tables for next test
    from sqlalchemy import text

    with db_mod._engine.connect() as conn:
        for table in reversed(db_mod.Base.metadata.sorted_tables):
            conn.execute(text(f"DROP TABLE IF EXISTS {table.name}"))
        conn.commit()


@pytest.fixture
def client() -> Iterator[TestClient]:
    app = create_app()
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


@pytest.fixture
def token(client: TestClient) -> str:
    resp = client.post(
        "/api/auth/register",
        json={"username": "testuser", "password": "testpass123", "display_name": "Test"},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    return data["access_token"]


@pytest.fixture
def authed(client: TestClient, token: str) -> TestClient:
    client.headers.update({"Authorization": f"Bearer {token}"})
    return client


class TestAuth:
    def test_register_and_login(self, client: TestClient) -> None:
        resp = client.post(
            "/api/auth/register",
            json={"username": "newuser", "password": "SecurePass1", "display_name": "New"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "access_token" in data
        assert data["token_type"] == "bearer"

        resp2 = client.post(
            "/api/auth/login",
            json={"username": "newuser", "password": "SecurePass1"},
        )
        assert resp2.status_code == 200
        data2 = resp2.json()
        assert "access_token" in data2

    def test_login_wrong_password(self, client: TestClient) -> None:
        client.post(
            "/api/auth/register",
            json={"username": "u1", "password": "pass1"},
        )
        resp = client.post(
            "/api/auth/login",
            json={"username": "u1", "password": "wrong"},
        )
        assert resp.status_code == 401

    def test_me_endpoint(self, client: TestClient, token: str) -> None:
        resp = client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["username"] == "testuser"
        assert data["display_name"] == "Test"


class TestNotebooks:
    def test_list_empty(self, authed: TestClient) -> None:
        resp = authed.get("/api/notebooks")
        assert resp.status_code == 200
        assert resp.json() == []

    def test_create_and_list(self, authed: TestClient) -> None:
        resp = authed.post("/api/notebooks", json={"title": "My Notebook"})
        assert resp.status_code == 200
        nb = resp.json()
        assert nb["title"] == "My Notebook"
        assert nb["id"] > 0

        resp2 = authed.get("/api/notebooks")
        assert len(resp2.json()) == 1

    def test_get_notebook(self, authed: TestClient) -> None:
        created = authed.post("/api/notebooks", json={"title": "Test NB"}).json()
        resp = authed.get(f"/api/notebooks/{created['id']}")
        assert resp.status_code == 200
        assert resp.json()["title"] == "Test NB"

    def test_delete_notebook(self, authed: TestClient) -> None:
        created = authed.post("/api/notebooks", json={"title": "Delete Me"}).json()
        resp = authed.delete(f"/api/notebooks/{created['id']}")
        assert resp.status_code == 200
        resp2 = authed.get("/api/notebooks")
        assert resp2.json() == []


class TestSources:
    def test_upload_source(self, authed: TestClient) -> None:
        nb = authed.post("/api/notebooks", json={"title": "NB"}).json()
        resp = authed.post(
            "/api/sources/upload",
            data={"notebook_id": str(nb["id"])},
            files={"file": ("test.txt", b"Hello world", "text/plain")},
        )
        assert resp.status_code == 200
        src = resp.json()
        assert src["filename"] == "test.txt"
        assert src["notebook_id"] == nb["id"]

    def test_list_sources(self, authed: TestClient) -> None:
        nb = authed.post("/api/notebooks", json={"title": "NB"}).json()
        authed.post(
            "/api/sources/upload",
            data={"notebook_id": str(nb["id"])},
            files={"file": ("a.txt", b"aaa", "text/plain")},
        )
        resp = authed.get(f"/api/sources?notebook_id={nb['id']}")
        assert resp.status_code == 200
        assert len(resp.json()) == 1


class TestChat:
    def test_chat_session_crud(self, authed: TestClient) -> None:
        nb = authed.post("/api/notebooks", json={"title": "NB"}).json()

        resp = authed.post(f"/api/chat/sessions", json={"notebook_id": nb["id"], "title": "Chat 1"})
        assert resp.status_code == 200
        sess = resp.json()
        assert sess["title"] == "Chat 1"

        resp2 = authed.get(f"/api/chat/sessions?notebook_id={nb['id']}")
        assert len(resp2.json()) == 1

    def test_send_message(self, authed: TestClient) -> None:
        nb = authed.post("/api/notebooks", json={"title": "NB"}).json()
        sess = authed.post(
            f"/api/chat/sessions", json={"notebook_id": nb["id"], "title": "Chat"}
        ).json()

        resp = authed.post(
            f"/api/chat/sessions/{sess['id']}/messages",
            json={"content": "Hello"},
        )
        assert resp.status_code == 200
        msg = resp.json()
        assert msg["role"] == "user"
        assert msg["content"] == "Hello"

        resp2 = authed.get(f"/api/chat/sessions/{sess['id']}/messages")
        assert len(resp2.json()) >= 1


class TestExternalKB:
    def test_create_connection(self, authed: TestClient) -> None:
        resp = authed.post(
            "/api/external-kb/connections",
            json={
                "name": "Test KB",
                "provider_type": "openapi",
                "api_base_url": "https://example.com/api",
                "auth_type": "api_key",
                "auth_credentials": json.dumps({"api_key": "sk-test"}),
            },
        )
        assert resp.status_code == 200
        conn = resp.json()
        assert conn["name"] == "Test KB"

    def test_list_connections(self, authed: TestClient) -> None:
        authed.post(
            "/api/external-kb/connections",
            json={
                "name": "KB 1",
                "provider_type": "openapi",
                "api_base_url": "https://ex.com/api",
            },
        )
        resp = authed.get("/api/external-kb/connections")
        assert resp.status_code == 200
        assert len(resp.json()) == 1


class TestGeneration:
    def test_generate_document(self, authed: TestClient) -> None:
        nb = authed.post("/api/notebooks", json={"title": "NB"}).json()
        resp = authed.post(
            "/api/generation/generate",
            json={
                "notebook_id": nb["id"],
                "content_type": "document",
                "prompt": "写一份关于AI的摘要",
                "template": "summary",
            },
        )
        assert resp.status_code == 200
        gen = resp.json()
        assert gen["content_type"] == "document"
        assert gen["status"] in ("processing", "completed")

    def test_list_generations(self, authed: TestClient) -> None:
        nb = authed.post("/api/notebooks", json={"title": "NB"}).json()
        authed.post(
            "/api/generation/generate",
            json={
                "notebook_id": nb["id"],
                "content_type": "document",
                "prompt": "摘要",
            },
        )
        resp = authed.get(f"/api/generation/list?notebook_id={nb['id']}")
        assert resp.status_code == 200
        assert len(resp.json()) >= 1


class TestAuthMiddleware:
    def test_no_token_returns_401(self, client: TestClient) -> None:
        resp = client.get("/api/notebooks")
        assert resp.status_code == 401

    def test_bad_token_returns_401(self, client: TestClient) -> None:
        resp = client.get(
            "/api/notebooks", headers={"Authorization": "Bearer invalidtoken"}
        )
        assert resp.status_code == 401

    def test_expired_token_returns_401(self, client: TestClient) -> None:
        resp = client.get(
            "/api/notebooks",
            headers={"Authorization": "Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJleHAiOjB9.invalid"},
        )
        assert resp.status_code == 401


class TestCORS:
    def test_cors_headers_present_in_dev(self) -> None:
        os.environ["BAOKU_DEV"] = "1"
        try:
            app = create_app()
            with TestClient(app) as c:
                resp = c.options(
                    "/api/health",
                    headers={
                        "Origin": "http://localhost:5173",
                        "Access-Control-Request-Method": "GET",
                    },
                )
                assert resp.status_code == 200
                assert resp.headers.get("access-control-allow-origin") == "http://localhost:5173"
        finally:
            os.environ.pop("BAOKU_DEV", None)

    def test_no_cors_in_production(self) -> None:
        os.environ.pop("BAOKU_DEV", None)
        app = create_app()
        with TestClient(app) as c:
            resp = c.options(
                "/api/health",
                headers={
                    "Origin": "http://localhost:5173",
                    "Access-Control-Request-Method": "GET",
                },
            )
            assert resp.headers.get("access-control-allow-origin") is None or resp.headers.get(
                "access-control-allow-origin"
            ) == ""
```

### ▶ 功能验证 4.4

```bash
# 运行集成测试
uv run pytest tests/server/test_api_integration.py -v 2>&1

# 预期输出: 所有测试通过 (PASSED)
```

---

## Task 4.5: Docker 部署

### 4.5.1 — 创建 Dockerfile

**文件：** `Dockerfile.baoku`

```dockerfile
# Baoku clone — multi-stage Docker image
#
# Stage 1: Build frontend
FROM node:20-alpine AS frontend-builder

WORKDIR /app/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci

COPY frontend/ ./
RUN npm run build

# Stage 2: Python runtime
FROM python:3.12-slim

WORKDIR /app

# Install system deps needed for generation engines (optional)
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    graphviz \
    && rm -rf /var/lib/apt/lists/*

# Copy backend source
COPY src/ ./src/
COPY pyproject.toml hatch_build.py ./
COPY scripts/ ./scripts/

# Install Python dependencies
RUN pip install --no-cachedir -e ".[server,markdown]"

# Copy built frontend from stage 1
COPY --from=frontend-builder /app/frontend/dist ./frontend/dist

# Runtime config
ENV BAOKU_DATABASE_URL=sqlite:///data/baoku.db \
    BAOKU_MEDIA_ROOT=/data/media \
    BAOKU_JWT_SECRET=change-me-in-production

RUN useradd --create-home --uid 10001 app
RUN mkdir -p /data /data/media && chown -R app:app /data
USER app

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; import sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/health').status == 200 else 1)"

CMD ["uvicorn", "notebooklm.server.server:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
```

### 4.5.2 — 创建 docker-compose.baoku.yml

**文件：** `docker-compose.baoku.yml`

```yaml
# Baoku clone — Docker Compose
#
# Start:   docker compose -f docker-compose.baoku.yml up -d --build
# Stop:    docker compose -f docker-compose.baoku.yml down
# Logs:    docker compose -f docker-compose.baoku.yml logs -f

services:
  app:
    build:
      context: .
      dockerfile: Dockerfile.baoku
    ports:
      - "8000:8000"
    environment:
      BAOKU_DATABASE_URL: ${BAOKU_DATABASE_URL:-sqlite:///data/baoku.db}
      BAOKU_MEDIA_ROOT: /data/media
      BAOKU_JWT_SECRET: ${BAOKU_JWT_SECRET:-change-me-in-production}
    volumes:
      - baoku-data:/data
    restart: unless-stopped
    healthcheck:
      test: ["CMD", "python", "-c", "import urllib.request; import sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/health').status == 200 else 1)"]
      interval: 30s
      timeout: 5s
      retries: 3
      start_period: 10s

  # Optional PostgreSQL for production (uncomment and set BAOKU_DATABASE_URL)
  # db:
  #   image: postgres:16-alpine
  #   environment:
  #     POSTGRES_USER: baoku
  #     POSTGRES_PASSWORD: ${POSTGRES_PASSWORD:-changeme}
  #     POSTGRES_DB: baoku
  #   volumes:
  #     - postgres-data:/var/lib/postgresql/data
  #   restart: unless-stopped
  #   healthcheck:
  #     test: ["CMD-SHELL", "pg_isready -U baoku"]
  #     interval: 10s
  #     timeout: 5s
  #     retries: 5

volumes:
  baoku-data:
  # postgres-data:
```

### 4.5.3 — 更新 .dockerignore

**文件：** `.dockerignore`

在现有内容基础上追加以下规则（保留已有内容不变）：

```
# Baoku-specific excludes
.venv
node_modules/
frontend/node_modules/
frontend/dist/
*.db
/data/
```

### ▶ 功能验证 4.5

```bash
# 验证 Docker 镜像构建
docker build -f Dockerfile.baoku -t baoku-test:latest .

# 验证容器启动
docker run --rm -p 8000:8000 -e BAOKU_JWT_SECRET=test-secret baoku-test:latest &
sleep 5
curl -s -o /dev/null -w "%{http_code}" http://localhost:8000/api/health
# 预期: 200
docker kill %1 2>/dev/null || true
```

---

## Task 4.6: 最终集成冒烟测试

### 4.6.1 — 创建冒烟测试脚本

**文件：** `scripts/smoke_test.sh`

```bash
#!/usr/bin/env bash
# Baoku clone — integration smoke test
# Prerequisites: backend running on port 8000, frontend running on port 5173
set -euo pipefail

BASE_URL="${API_BASE:-http://localhost:8000}"
FRONTEND_URL="${FRONTEND_BASE:-http://localhost:5173}"
PASS=0
FAIL=0

green() { printf "\033[32m%s\033[0m\n" "$1"; }
red()   { printf "\033[31m%s\033[0m\n" "$1"; }
bold()  { printf "\033[1m%s\033[0m\n" "$1"; }

check() {
    local name="$1"
    local code="$2"
    shift 2
    local actual
    actual=$(curl -s -o /dev/null -w "%{http_code}" "$@" 2>/dev/null || true)
    if [ "$actual" = "$code" ]; then
        green "  ✓ $name"
        PASS=$((PASS + 1))
    else
        red "  ✗ $name (expected $code, got $actual)"
        FAIL=$((FAIL + 1))
    fi
}

check_json() {
    local name="$1"
    local field="$2"
    local expected="$3"
    shift 3
    local actual
    actual=$(curl -s "$@" 2>/dev/null | python3 -c "import sys,json; print(json.load(sys.stdin).get('$field', ''))" 2>/dev/null || true)
    if [ "$actual" = "$expected" ]; then
        green "  ✓ $name"
        PASS=$((PASS + 1))
    else
        red "  ✗ $name (expected $field=$expected, got $actual)"
        FAIL=$((FAIL + 1))
    fi
}

bold "=== Baoku Clone — Integration Smoke Test ==="
bold "Backend: $BASE_URL"
bold ""

# 1. Health check
bold "1. Basic connectivity"
check "Health endpoint returns 200" 200 "$BASE_URL/api/health"

# 2. Auth flow
bold "2. Auth flow"
check "Register returns 200" 200 -X POST "$BASE_URL/api/auth/register" \
    -H "Content-Type: application/json" \
    -d '{"username":"smoketest","password":"SmokePass1","display_name":"Smoke Test"}'

TOKEN=$(curl -s -X POST "$BASE_URL/api/auth/login" \
    -H "Content-Type: application/json" \
    -d '{"username":"smoketest","password":"SmokePass1"}' | python3 -c "import sys,json; print(json.load(sys.stdin).get('access_token',''))" 2>/dev/null || true)

if [ -n "$TOKEN" ]; then
    green "  ✓ Login returns JWT token"
    PASS=$((PASS + 1))
else
    red "  ✗ Login failed to return token"
    FAIL=$((FAIL + 1))
fi

AUTH="Authorization: Bearer $TOKEN"

check_json "Me endpoint returns correct username" "username" "smoketest" \
    "$BASE_URL/api/auth/me" -H "$AUTH"

# 3. Notebook CRUD
bold "3. Notebook CRUD"
NOTEBOOK_ID=$(curl -s -X POST "$BASE_URL/api/notebooks" \
    -H "Content-Type: application/json" \
    -H "$AUTH" \
    -d '{"title":"Smoke Test Notebook"}' | python3 -c "import sys,json; print(json.load(sys.stdin).get('id',''))" 2>/dev/null || true)

if [ -n "$NOTEBOOK_ID" ]; then
    green "  ✓ Create notebook returns ID"
    PASS=$((PASS + 1))
else
    red "  ✗ Create notebook failed"
    FAIL=$((FAIL + 1))
fi

check "List notebooks returns 200" 200 "$BASE_URL/api/notebooks" -H "$AUTH"
check "Get notebook returns 200" 200 "$BASE_URL/api/notebooks/$NOTEBOOK_ID" -H "$AUTH"

# 4. Sources
bold "4. Source upload"
check "Upload source returns 200" 200 -X POST "$BASE_URL/api/sources/upload" \
    -H "$AUTH" \
    -F "notebook_id=$NOTEBOOK_ID" \
    -F "file=@pyproject.toml"

# 5. Chat
bold "5. Chat"
SESSION_ID=$(curl -s -X POST "$BASE_URL/api/chat/sessions" \
    -H "Content-Type: application/json" \
    -H "$AUTH" \
    -d "{\"notebook_id\":$NOTEBOOK_ID,\"title\":\"Smoke Chat\"}" | python3 -c "import sys,json; print(json.load(sys.stdin).get('id',''))" 2>/dev/null || true)

if [ -n "$SESSION_ID" ]; then
    green "  ✓ Create chat session returns ID"
    PASS=$((PASS + 1))
else
    red "  ✗ Create chat session failed"
    FAIL=$((FAIL + 1))
fi

check "Send message returns 200" 200 -X POST "$BASE_URL/api/chat/sessions/$SESSION_ID/messages" \
    -H "Content-Type: application/json" \
    -H "$AUTH" \
    -d '{"content":"Hello, this is a smoke test message"}'

# 6. Generation
bold "6. Generation"
check "Generate document returns 200" 200 -X POST "$BASE_URL/api/generation/generate" \
    -H "Content-Type: application/json" \
    -H "$AUTH" \
    -d "{\"notebook_id\":$NOTEBOOK_ID,\"content_type\":\"document\",\"prompt\":\"测试摘要\",\"template\":\"summary\"}"

# 7. External KB
bold "7. External KB"
check "Create external KB connection returns 200" 200 -X POST "$BASE_URL/api/external-kb/connections" \
    -H "Content-Type: application/json" \
    -H "$AUTH" \
    -d '{"name":"Smoke Test KB","provider_type":"openapi","api_base_url":"https://example.com/api"}'

# 8. Auth middleware
bold "8. Auth middleware"
check "No token returns 401" 401 "$BASE_URL/api/notebooks"
check "Bad token returns 401" 401 "$BASE_URL/api/notebooks" -H "Authorization: Bearer invalid"

# 9. CORS (dev mode)
if [ "${BAOKU_DEV:-0}" = "1" ]; then
    bold "9. CORS headers"
    CORS_CHECK=$(curl -s -o /dev/null -w "%{http_code}" -X OPTIONS "$BASE_URL/api/health" \
        -H "Origin: http://localhost:5173" \
        -H "Access-Control-Request-Method: GET")
    if [ "$CORS_CHECK" = "200" ]; then
        green "  ✓ CORS preflight returns 200"
        PASS=$((PASS + 1))
    else
        red "  ✗ CORS preflight failed (is BAOKU_DEV=1?)"
        FAIL=$((FAIL + 1))
    fi
fi

# Summary
bold ""
bold "=== Results ==="
bold "Passed: $PASS"
bold "Failed: $FAIL"
if [ "$FAIL" -eq 0 ]; then
    green "All smoke tests passed!"
else
    red "Some smoke tests failed."
    exit 1
fi
```

### 4.6.2 — 冒烟测试执行

```bash
# Step 1: 启动后端 (dev mode + CORS)
BAOKU_DEV=1 BAOKU_DATABASE_URL=sqlite:///:memory: \
    uv run uvicorn notebooklm.server.server:create_app --factory --reload --port 8000 &
BACKEND_PID=$!
sleep 3

# Step 2: 启动前端
cd frontend && npm run dev &
FRONTEND_PID=$!
sleep 3

# Step 3: 运行冒烟测试
bash scripts/smoke_test.sh

# Step 4: 清理
kill $FRONTEND_PID 2>/dev/null || true
kill $BACKEND_PID 2>/dev/null || true
```

### 冒烟测试清单

| # | 检查项 | 预期 | 状态 |
|---|---|---|---|
| 1 | Health 端点 | 返回 `200 {"status": "ok"}` | — |
| 2 | 用户注册 | 返回 `200` 含 `access_token` | — |
| 3 | 用户登录 | 返回 JWT token | — |
| 4 | Me 端点 | 返回当前用户信息 | — |
| 5 | 创建知识库 | 返回含 `id` 的知识库对象 | — |
| 6 | 列出知识库 | 返回数组，至少包含刚创建的 | — |
| 7 | 获取单个知识库 | 返回正确标题和 ID | — |
| 8 | 上传资料 (PDF/文本) | `200`，文件持久化 | — |
| 9 | 列出资料 | 返回数组包含刚上传的资料 | — |
| 10 | 创建对话会话 | 返回含 `id` 的会话对象 | — |
| 11 | 发送聊天消息 | `200`，消息包含 `role: "user"` | — |
| 12 | 获取聊天消息 | 返回消息列表 | — |
| 13 | 创建外部 KB 连接 | `200`，返回连接对象 | — |
| 14 | 列出外部 KB 连接 | 返回数组 | — |
| 15 | 触发内容生成 | `200`，返回 `status: "processing"` | — |
| 16 | 列出生成历史 | 返回数组 | — |
| 17 | 无 token 访问 | `401` | — |
| 18 | 无效 token 访问 | `401` | — |
| 19 | CORS 头 (dev mode) | `access-control-allow-origin: http://localhost:5173` | — |
| 20 | 暗色主题切换 | 页面加载后 `<html>` 有/无 `.dark` 类 | — |
| 21 | 移动端响应式 | 浏览器宽度 < 640px 时布局切换 | — |

## 验证

每个 Task 完成后必须运行对应的命令验证正确性。全部 Task 完成后运行完整的冒烟测试脚本确认集成正常。

```bash
# 最终验证命令
uv run pytest tests/server/test_api_integration.py -v
bash scripts/smoke_test.sh
```
