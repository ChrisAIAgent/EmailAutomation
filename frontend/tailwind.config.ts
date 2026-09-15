/** @type {import('tailwindcss').Config} */
module.exports = {
  content: [
    "./app/**/*.{js,ts,jsx,tsx}",
    "./components/**/*.{js,ts,jsx,tsx}",
  ],
  theme: {
    extend: {
      colors: {
        bg: "var(--ea-bg)",
        panel: "var(--ea-panel)",
        panel2: "var(--ea-panel2)",
        border: "var(--ea-border)",
        muted: "var(--ea-muted)",
        foreground: "var(--ea-foreground)",
        accent: "var(--ea-accent)",
        brand: "var(--ea-brand)",
        accentDark: "var(--ea-accent-dark)",
        danger: "var(--ea-danger)",
        warn: "var(--ea-warn)",
        ok: "var(--ea-ok)",
      },
    },
  },
  plugins: [],
};
