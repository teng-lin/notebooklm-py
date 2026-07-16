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
