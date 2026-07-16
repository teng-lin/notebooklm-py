<template>
  <div class="split-pane" :style="{ flexDirection: direction }">
    <div class="pane pane-left" :style="leftStyle">
      <slot name="left" />
    </div>
    <div class="splitter" @mousedown="startDrag" @touchstart="startDrag">
      <div class="splitter-line" />
    </div>
    <div class="pane pane-right" :style="rightStyle">
      <slot name="right" />
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted } from "vue"

const props = withDefaults(defineProps<{
  minLeft?: number
  minRight?: number
  initialLeft?: number
  storageKey?: string
  direction?: "row" | "column"
}>(), {
  minLeft: 200, minRight: 200, initialLeft: 300, direction: "row",
})

const leftWidth = ref(props.initialLeft)
const dragging = ref(false)

onMounted(() => {
  if (props.storageKey) {
    const saved = localStorage.getItem(props.storageKey)
    if (saved) leftWidth.value = Number(saved)
  }
})

const leftStyle = computed(() => ({
  width: props.direction === "row" ? `${leftWidth.value}px` : "100%",
  height: props.direction === "column" ? `${leftWidth.value}px` : "100%",
  flexShrink: 0,
}))
const rightStyle = computed(() => ({ flex: 1, minWidth: 0 }))

function startDrag(e: MouseEvent | TouchEvent) {
  e.preventDefault()
  dragging.value = true
  const startX = "touches" in e ? e.touches[0].clientX : e.clientX
  const startY = "touches" in e ? e.touches[0].clientY : e.clientY
  const startWidth = leftWidth.value
  const parent = (e.target as HTMLElement).closest(".split-pane") as HTMLElement
  const parentSize = props.direction === "row" ? parent.offsetWidth : parent.offsetHeight

  function onMove(ev: MouseEvent | TouchEvent) {
    if (!dragging.value) return
    const currentX = "touches" in ev ? ev.touches[0].clientX : ev.clientX
    const currentY = "touches" in ev ? ev.touches[0].clientY : ev.clientY
    const delta = props.direction === "row" ? currentX - startX : currentY - startY
    let newWidth = startWidth + delta
    const max = parentSize - props.minRight
    newWidth = Math.max(props.minLeft, Math.min(max, newWidth))
    leftWidth.value = newWidth
  }
  function onUp() {
    dragging.value = false
    if (props.storageKey) localStorage.setItem(props.storageKey, String(leftWidth.value))
    document.removeEventListener("mousemove", onMove)
    document.removeEventListener("mouseup", onUp)
    document.removeEventListener("touchmove", onMove)
    document.removeEventListener("touchend", onUp)
  }
  document.addEventListener("mousemove", onMove)
  document.addEventListener("mouseup", onUp)
  document.addEventListener("touchmove", onMove)
  document.addEventListener("touchend", onUp)
}
</script>

<style scoped>
.split-pane { display: flex; width: 100%; height: 100%; overflow: hidden; }
.pane { overflow: auto; height: 100%; }
.splitter { width: 6px; height: 100%; cursor: col-resize; background: transparent; flex-shrink: 0; position: relative; z-index: 10; }
.splitter:hover .splitter-line, .splitter:active .splitter-line { background: var(--baoku-primary, #ff3650); }
.splitter-line { position: absolute; left: 2px; top: 0; bottom: 0; width: 2px; background: var(--baoku-border, #e8e8e8); transition: background 0.2s; }
</style>
