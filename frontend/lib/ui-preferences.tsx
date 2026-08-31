"use client";

import { createContext, useCallback, useContext, useEffect, useState } from "react";

export type UiTheme = "dark" | "light";
export type UiDensity = "standard" | "large";

type UiPreferences = {
  theme: UiTheme;
  density: UiDensity;
  sidebarCollapsed: boolean;
  setTheme: (theme: UiTheme) => void;
  setDensity: (density: UiDensity) => void;
  setSidebarCollapsed: (collapsed: boolean) => void;
};

const STORAGE_KEY = "email-automation.ui-preferences";
const defaults = { theme: "dark" as UiTheme, density: "standard" as UiDensity, sidebarCollapsed: false };
const UiPreferencesContext = createContext<UiPreferences>({
  ...defaults,
  setTheme: () => {}, setDensity: () => {}, setSidebarCollapsed: () => {},
});

function apply(theme: UiTheme, density: UiDensity) {
  document.documentElement.dataset.theme = theme;
  document.documentElement.dataset.density = density;
}

export function UiPreferencesProvider({ children }: { children: React.ReactNode }) {
  const [value, setValue] = useState(defaults);

  useEffect(() => {
    try {
      const stored = JSON.parse(localStorage.getItem(STORAGE_KEY) || "{}");
      const next = {
        theme: stored.theme === "light" ? "light" : "dark",
        density: stored.density === "large" ? "large" : "standard",
        sidebarCollapsed: stored.sidebarCollapsed === true,
      } as typeof defaults;
      setValue(next);
      apply(next.theme, next.density);
    } catch { apply(defaults.theme, defaults.density); }
  }, []);

  const update = useCallback((patch: Partial<typeof defaults>) => {
    setValue((current) => {
      const next = { ...current, ...patch };
      apply(next.theme, next.density);
      try { localStorage.setItem(STORAGE_KEY, JSON.stringify(next)); } catch {}
      return next;
    });
  }, []);

  return <UiPreferencesContext.Provider value={{
    ...value,
    setTheme: (theme) => update({ theme }),
    setDensity: (density) => update({ density }),
    setSidebarCollapsed: (sidebarCollapsed) => update({ sidebarCollapsed }),
  }}>{children}</UiPreferencesContext.Provider>;
}

export const useUiPreferences = () => useContext(UiPreferencesContext);
