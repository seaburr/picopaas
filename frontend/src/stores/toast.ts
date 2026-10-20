import { defineStore } from 'pinia'
import { ref } from 'vue'

export type ToastKind = 'success' | 'error' | 'info'
export interface Toast {
  id: number
  kind: ToastKind
  message: string
}

// Usage: const toast = useToastStore(); toast.success('App deleted'); toast.error("Couldn't delete the app: ...")
export const useToastStore = defineStore('toast', () => {
  const toasts = ref<Toast[]>([])
  let next = 1

  function dismiss(id: number) {
    toasts.value = toasts.value.filter((t) => t.id !== id)
  }

  function push(kind: ToastKind, message: string, ms?: number) {
    const id = next++
    toasts.value = [...toasts.value.slice(-3), { id, kind, message }]
    setTimeout(() => dismiss(id), ms ?? (kind === 'error' ? 9000 : 5000))
    return id
  }

  const success = (message: string, ms?: number) => push('success', message, ms)
  const error = (message: string, ms?: number) => push('error', message, ms)
  const info = (message: string, ms?: number) => push('info', message, ms)

  return { toasts, push, dismiss, success, error, info }
})
