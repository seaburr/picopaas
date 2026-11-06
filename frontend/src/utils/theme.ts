export type Theme = 'dark' | 'light'
export const THEME_KEY = 'picopaas-theme'
const THEME_COLOR: Record<Theme, string> = { dark: '#0d0d14', light: '#e7e0d0' }

export function cachedTheme(): Theme {
  try {
    return localStorage.getItem(THEME_KEY) === 'light' ? 'light' : 'dark'
  } catch {
    return 'dark'
  }
}

// Sets <html data-theme>, the browser chrome colours, and the localStorage cache that public/theme-init.js reads
// before first paint. The cache is a device preference: it is kept across logout (the sign-in page then matches the
// last person's choice, and the next sign-in's server value replaces it).
export function applyTheme(theme: Theme) {
  document.documentElement.setAttribute('data-theme', theme)
  document.querySelector('meta[name="color-scheme"]')?.setAttribute('content', theme)
  document.querySelector('meta[name="theme-color"]')?.setAttribute('content', THEME_COLOR[theme])
  try {
    localStorage.setItem(THEME_KEY, theme)
  } catch {
    /* storage blocked */
  }
}
