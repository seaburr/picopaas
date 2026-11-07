import { ref } from 'vue'

// Getting started dialog state shared by the dialog (App.vue) and the "Show getting started" action (Account page).
const SEEN_KEY = 'picopaas.onboarding.seen'
const forced = ref(false) // opened on request: ignores the session and dismissed flags

export function seenThisSession(): boolean {
  try {
    return sessionStorage.getItem(SEEN_KEY) === '1'
  } catch {
    return false
  }
}
export function markSeen() {
  try {
    sessionStorage.setItem(SEEN_KEY, '1')
  } catch {
    /* storage blocked: the dialog may show again on reload, nothing else breaks */
  }
}
// In-memory fallback so a blocked sessionStorage does not reopen it on every navigation.
export const seenMemory = ref(false)

export function useOnboarding() {
  return { forced, show: () => (forced.value = true) }
}
