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
