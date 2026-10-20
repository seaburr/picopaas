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
        // Primary button fill: white text is 5.0:1 on DEFAULT, 5.9:1 on hover.
        primary: {
          DEFAULT: '#3a6bd0',
          hover: '#2f5fc4',
          active: '#2653b0',
        },
        // Contrast floor: zinc 500/600 are remapped so text-zinc-500/600 (and placeholders) stay >= 4.5:1
        // on the panel/card surfaces (#8b8b96 is 5.3:1 on panel, 5.0:1 on card). Do not use darker greys for text.
        zinc: {
          500: '#8b8b96',
          600: '#8b8b96',
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
