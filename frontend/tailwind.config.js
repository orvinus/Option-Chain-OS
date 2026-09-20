/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        bg: "#0b0f17",
        panel: "#111827",
        border: "#1f2937",
        ce: "#ef4444",
        pe: "#22c55e",
        accent: "#3b82f6",
        muted: "#6b7280",
        // Both were USED but never defined (39 `text-foreground`, 4
        // `bg-surface` call sites as of 2026-09-14), so they silently produced
        // no styles — which is why the two existing loading chips rendered as
        // bare text with no background over a dimmed chart. Defining them here
        // repairs every call site at once.
        foreground: "#f3f4f6",
        surface: "#1a2332",
      },
      fontFamily: {
        sans: ["Inter", "ui-sans-serif", "system-ui", "sans-serif"],
        mono: ["JetBrains Mono", "ui-monospace", "monospace"],
      },
    },
  },
  plugins: [],
};
