// Theme system (2026-05-26) — light/dark toggle persisted in localStorage.
// Default = dark (matches the existing app look). Users opt into light.
// The active theme is written to <html data-theme="..."> so all CSS
// variables (--v4-* tokens, Tailwind shadcn HSL tokens) flip automatically.
import { createContext, useContext, useEffect, useState, useCallback } from "react";

const ThemeContext = createContext({ theme: "dark", setTheme: () => {} });

const STORAGE_KEY = "siteops.theme";

function applyTheme(theme) {
  document.documentElement.setAttribute("data-theme", theme);
}

export function ThemeProvider({ children }) {
  const [theme, setThemeState] = useState(() => {
    if (typeof window === "undefined") return "dark";
    try {
      const saved = window.localStorage.getItem(STORAGE_KEY);
      return saved === "light" || saved === "dark" ? saved : "dark";
    } catch {
      return "dark";
    }
  });

  // Sync to <html> on every change.
  useEffect(() => {
    applyTheme(theme);
    try { window.localStorage.setItem(STORAGE_KEY, theme); } catch {}
  }, [theme]);

  const setTheme = useCallback((next) => {
    setThemeState(next === "light" ? "light" : "dark");
  }, []);

  const toggle = useCallback(() => {
    setThemeState((cur) => (cur === "dark" ? "light" : "dark"));
  }, []);

  return (
    <ThemeContext.Provider value={{ theme, setTheme, toggle }}>
      {children}
    </ThemeContext.Provider>
  );
}

export function useTheme() {
  return useContext(ThemeContext);
}
