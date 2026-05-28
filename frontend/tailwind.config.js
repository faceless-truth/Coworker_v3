/** @type {import('tailwindcss').Config} */
export default {
  content: ['./index.html', './src/**/*.{js,ts,jsx,tsx}'],
  darkMode: 'class',
  theme: {
    extend: {
      colors: {
        navy: '#142234',
        'navy-light': '#1e3a52',
        orange: '#eb881f',
        cobalt: '#3080bc',
        offwhite: '#f3f1ee',
        warm: '#34322d',
        muted: '#858481',
        border: '#d9d8d8',
        danger: '#e11d48',
      },
      fontFamily: {
        sans: ['DM Sans', 'system-ui', 'sans-serif'],
        serif: ['DM Serif Display', 'Georgia', 'serif'],
        mono: ['JetBrains Mono', 'Menlo', 'monospace'],
      },
    },
  },
  plugins: [],
}

