import type { Config } from 'tailwindcss'

// Dev-tool palette, not a marketing site: dark, dense, low on chrome.
export default {
  content: ['./index.html', './src/**/*.{vue,ts}'],
  theme: {
    extend: {
      colors: {
        pico: {
          bg: '#0d0d14',
          panel: '#16161f',
          card: '#1c1c28',
          border: '#2c2c3c',
          muted: '#4a4a5e',
        },
        accent: {
          DEFAULT: '#4a78c2',
          dark: '#233775',
          light: '#7aa0d4',
        },
        danger: {
          DEFAULT: '#d95555',
          dark: '#9e2424',
          light: '#e87070',
        },
        status: {
          pending: '#d4aa20',
          running: '#6a9e4a',
          error: '#d95555',
          stopped: '#7a7a8c',
        },
      },
    },
  },
} satisfies Config
