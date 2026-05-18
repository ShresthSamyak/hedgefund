import type { Config } from "tailwindcss";

const config: Config = {
  content: ["./app/**/*.{ts,tsx}", "./components/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        bg: "#0a0a0a",
        card: "#111111",
        border: "#222222",
        muted: "#666666",
        text: "#e5e5e5",
        pos: "#00c48c",
        neg: "#ff4d4f",
        warn: "#fbbf24",
        accent: "#3b82f6",
        danger: "#ef4444",
      },
      fontFamily: {
        mono: ["ui-monospace", "Menlo", "Consolas", "monospace"],
        sans: ["ui-sans-serif", "system-ui", "-apple-system", "Segoe UI", "sans-serif"],
      },
      keyframes: {
        pulse_blue: {
          "0%, 100%": { boxShadow: "0 0 0 0 rgba(59,130,246,0.6)" },
          "50%": { boxShadow: "0 0 0 6px rgba(59,130,246,0)" },
        },
      },
      animation: { "pulse-blue": "pulse_blue 2s ease-in-out infinite" },
    },
  },
  plugins: [],
};
export default config;
