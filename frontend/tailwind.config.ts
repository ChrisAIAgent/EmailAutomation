/** @type {import('tailwindcss').Config} */
module.exports = {
  content: [
    "./app/**/*.{js,ts,jsx,tsx}",
    "./components/**/*.{js,ts,jsx,tsx}",
  ],
  theme: {
    extend: {
      colors: {
        bg: "#0b0f17",
        panel: "#121826",
        panel2: "#1a2233",
        border: "#243044",
        muted: "#8b97ab",
        accent: "#4f8cff",
        brand: "#ef1b2d",
        accentDark: "#b90f1d",
        danger: "#ff5d5d",
        warn: "#ffb020",
        ok: "#3ddc84",
      },
    },
  },
  plugins: [],
};
