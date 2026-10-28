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
        insurance: {
          accent: "rgb(var(--color-primary) / <alpha-value>)",
          "bg": "rgb(var(--color-bg) / <alpha-value>)",
          "surface": "rgb(var(--color-surface) / <alpha-value>)",
          "surface-secondary": "rgb(var(--color-surface-secondary) / <alpha-value>)",
          "ink": "rgb(var(--color-text) / <alpha-value>)",
          "ink-secondary": "rgb(var(--color-text-secondary) / <alpha-value>)",
          "ink-tertiary": "rgb(var(--color-text-tertiary) / <alpha-value>)",
          "border": "rgb(var(--color-border) / <alpha-value>)",
          "border-strong": "rgb(var(--color-border-strong) / <alpha-value>)",
          "success": "rgb(var(--color-success) / <alpha-value>)",
          "warning": "rgb(var(--color-warning) / <alpha-value>)",
          "error": "rgb(var(--color-error) / <alpha-value>)",
          "info": "rgb(var(--color-info) / <alpha-value>)",
          "info-hover": "rgb(var(--color-primary-dark) / <alpha-value>)",
        },
      },
      fontFamily: {
        sans: ["Inter", "system-ui", "sans-serif"],
      },
      boxShadow: {
        subtle: "0 1px 2px 0 rgba(0, 0, 0, 0.04)",
        soft: "0 2px 8px 0 rgba(0, 0, 0, 0.06)",
        medium: "0 4px 16px 0 rgba(0, 0, 0, 0.08)",
        lifted: "0 8px 24px 0 rgba(0, 0, 0, 0.1)",
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
