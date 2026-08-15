"use client";

import { useEffect, useState } from "react";

// web-engagement-tool(MA)のThemeToggle.tsxと同じ実装(2026-08-15移植)。
type ThemePref = "system" | "light" | "dark";
const STORAGE_KEY = "dashboard-theme";

function resolve(pref: ThemePref): "light" | "dark" {
  if (pref === "system") {
    return window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
  }
  return pref;
}

const OPTIONS: { value: ThemePref; label: string }[] = [
  { value: "system", label: "システム" },
  { value: "light", label: "ライト" },
  { value: "dark", label: "ダーク" },
];

export default function ThemeToggle() {
  // Matches the blocking script in the root layout's <head>, which already
  // applied the right data-theme before first paint — this just syncs the
  // toggle's own UI state to whatever was stored.
  const [pref, setPref] = useState<ThemePref>("system");

  useEffect(() => {
    let stored: string | null = null;
    try {
      stored = localStorage.getItem(STORAGE_KEY);
    } catch {
      // localStorage unavailable — stay on "system"
    }
    // A lazy useState initializer can't safely read localStorage here: it
    // runs during the client's first render too, which would immediately
    // diverge from the server-rendered "system" default and trigger a
    // hydration mismatch. Reading post-mount in an effect is the correct
    // pattern for this one-time "sync from a browser-only external source"
    // case (react-hooks/set-state-in-effect's own guidance).
    // eslint-disable-next-line react-hooks/set-state-in-effect
    if (stored === "light" || stored === "dark") setPref(stored);
  }, []);

  useEffect(() => {
    document.documentElement.setAttribute("data-theme", resolve(pref));
    if (pref !== "system") return;

    // Only "system" needs to keep tracking OS changes live.
    const mq = window.matchMedia("(prefers-color-scheme: dark)");
    const handler = () => document.documentElement.setAttribute("data-theme", resolve("system"));
    mq.addEventListener("change", handler);
    return () => mq.removeEventListener("change", handler);
  }, [pref]);

  function select(next: ThemePref) {
    setPref(next);
    try {
      if (next === "system") localStorage.removeItem(STORAGE_KEY);
      else localStorage.setItem(STORAGE_KEY, next);
    } catch {
      // localStorage unavailable — the choice just won't persist across reloads
    }
  }

  return (
    <div className="flex items-center gap-0.5 rounded-lg bg-(--color-surface-muted) p-0.5 text-xs">
      {OPTIONS.map((opt) => (
        <button
          key={opt.value}
          type="button"
          onClick={() => select(opt.value)}
          className={`rounded-md px-2 py-1 font-medium transition-colors ${
            pref === opt.value
              ? "bg-(--color-surface) text-(--brand-blue-dark) shadow-sm"
              : "text-(--color-foreground)/50 hover:text-(--color-foreground)/80"
          }`}
        >
          {opt.label}
        </button>
      ))}
    </div>
  );
}
