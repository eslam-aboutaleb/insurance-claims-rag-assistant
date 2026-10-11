/** @type {import('tailwindcss').Config} */
module.exports = {
  content: [
    "./src/pages/**/*.{js,ts,jsx,tsx,mdx}",
    "./src/components/**/*.{js,ts,jsx,tsx,mdx}",
    "./src/**/*.{js,ts,jsx,tsx,mdx}",
    "./index.html",
  ],
  theme: {
    extend: {
      colors: {
        ochre: {
          50: "#fdf9f3",
          100: "#f9efdb",
          200: "#f2dcb5",
          300: "#e8c48a",
          400: "#dca95c",
          500: "#d2912e",
          600: "#b8731f",
          700: "#96571a",
          800: "#7c471c",
          900: "#683d1b",
          950: "#3d1f0d",
        },
        insurance: {
          bg: "#FAF9F6",
          surface: "#FFFFFF",
          "surface-secondary": "#F5F2EC",
          ink: "#24211E",
          "ink-secondary": "#6F6A63",
          "ink-tertiary": "#767068",
          border: "#E5E0D9",
          "border-strong": "#D7D0C7",
          success: "#2F7D5A",
          warning: "#B87A2A",
          error: "#B94A48",
          info: "#456B9C",
        },
        warm: {
          stone: "#7a7368",
          ink: "#2b2926",
          border: "#d6cfc3",
          muted: "#6b655a",
          surface: "#ffffff",
        },
      },
      fontFamily: {
        sans: ["Inter", "system-ui", "sans-serif"],
      },
      boxShadow: {
        subtle: "0 1px 2px 0 rgba(43, 41, 38, 0.04)",
        soft: "0 2px 8px 0 rgba(43, 41, 38, 0.06)",
        medium: "0 4px 16px 0 rgba(43, 41, 38, 0.08)",
        lifted: "0 8px 24px 0 rgba(43, 41, 38, 0.1)",
      },
      animation: {
        "fade-in": "fadeIn 0.24s ease-out",
        "slide-up": "slideUp 0.28s ease-out",
        "scale-in": "scaleIn 0.2s ease-out",
        "typing-dot": "typingDot 1.2s infinite ease-in-out both",
        shimmer: "shimmer 2.2s infinite linear",
      },
      keyframes: {
        fadeIn: {
          "0%": { opacity: "0" },
          "100%": { opacity: "1" },
        },
        slideUp: {
          "0%": { opacity: "0", transform: "translateY(6px)" },
          "100%": { opacity: "1", transform: "translateY(0)" },
        },
        scaleIn: {
          "0%": { opacity: "0", transform: "scale(0.98)" },
          "100%": { opacity: "1", transform: "scale(1)" },
        },
        typingDot: {
          "0%, 80%, 100%": { transform: "translateY(0)", opacity: "0.4" },
          "20%": { transform: "translateY(-3px)", opacity: "1" },
        },
        shimmer: {
          "0%": { backgroundPosition: "-200% 0" },
          "100%": { backgroundPosition: "200% 0" },
        },
      },
      backgroundImage: {
        "gradient-radial": "radial-gradient(var(--tw-gradient-stops))",
      },
    },
  },
  plugins: [],
};
