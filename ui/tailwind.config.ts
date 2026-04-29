import type { Config } from "tailwindcss";

export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        ink: "#181716",
        paper: "#f7f7f4",
        line: "#d8d6ce",
        pine: "#2f6f5e",
        amber: "#a86514",
        brick: "#aa3d2f"
      }
    }
  },
  plugins: []
} satisfies Config;
