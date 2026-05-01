import type { Config } from "tailwindcss";

export default {
  darkMode: "class",
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        ink: "rgb(var(--color-ink) / <alpha-value>)",
        paper: "rgb(var(--color-paper) / <alpha-value>)",
        line: "rgb(var(--color-line) / <alpha-value>)",
        pine: "rgb(var(--color-pine) / <alpha-value>)",
        amber: "rgb(var(--color-amber) / <alpha-value>)",
        brick: "rgb(var(--color-brick) / <alpha-value>)"
      }
    }
  },
  plugins: []
} satisfies Config;
