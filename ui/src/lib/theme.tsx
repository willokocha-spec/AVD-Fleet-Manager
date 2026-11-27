import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from "react";

import { THEME_STORAGE_KEY } from "./config";

export type ThemePreference = "dark" | "light" | "system";
export type ResolvedTheme = "dark" | "light";

interface ThemeContextValue {
  /** Persisted preference ("system" follows the OS). */
  theme: ThemePreference;
  /** Palette currently applied to <html data-theme>. */
  resolved: ResolvedTheme;
  setTheme: (theme: ThemePreference) => void;
  toggle: () => void;
}

const ThemeContext = createContext<ThemeContextValue | null>(null);

function readStoredPreference(): ThemePreference {
  try {
    const stored = window.localStorage.getItem(THEME_STORAGE_KEY);
    if (stored === "dark" || stored === "light" || stored === "system") return stored;
  } catch {
    /* storage unavailable */
  }
  return "dark";
}

function systemPrefersLight(): boolean {
  return typeof window !== "undefined" && window.matchMedia
    ? window.matchMedia("(prefers-color-scheme: light)").matches
    : false;
}

/** Dark-first theme provider with explicit light and system modes. */
export function ThemeProvider({ children }: { children: ReactNode }) {
  const [theme, setThemeState] = useState<ThemePreference>(() => readStoredPreference());
  const [systemLight, setSystemLight] = useState<boolean>(() => systemPrefersLight());

  useEffect(() => {
    if (!window.matchMedia) return;
    const query = window.matchMedia("(prefers-color-scheme: light)");
    const onChange = (event: MediaQueryListEvent): void => setSystemLight(event.matches);
    query.addEventListener("change", onChange);
    return () => query.removeEventListener("change", onChange);
  }, []);

  const resolved: ResolvedTheme = theme === "system" ? (systemLight ? "light" : "dark") : theme;

  useEffect(() => {
    const root = document.documentElement;
    root.dataset.theme = resolved;
    root.style.colorScheme = resolved;
    const meta = document.querySelector('meta[name="theme-color"]');
    meta?.setAttribute("content", resolved === "dark" ? "#0b0f16" : "#f4f6fa");
  }, [resolved]);

  const setTheme = useCallback((next: ThemePreference) => {
    setThemeState(next);
    try {
      window.localStorage.setItem(THEME_STORAGE_KEY, next);
    } catch {
      /* ignore */
    }
  }, []);

  const toggle = useCallback(() => {
    setTheme(resolved === "dark" ? "light" : "dark");
  }, [resolved, setTheme]);

  const value = useMemo<ThemeContextValue>(
    () => ({ theme, resolved, setTheme, toggle }),
    [resolved, setTheme, theme, toggle],
  );

  return <ThemeContext.Provider value={value}>{children}</ThemeContext.Provider>;
}

export function useTheme(): ThemeContextValue {
  const context = useContext(ThemeContext);
  if (!context) throw new Error("useTheme must be used inside <ThemeProvider>");
  return context;
}
