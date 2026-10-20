import { nextTick, onBeforeUnmount, watch, type Ref } from 'vue'

// Wires a native <dialog> to an `open` flag: showModal() gives focus containment, an inert background and Escape.
// On open focus moves to `initialFocus` (selector inside the dialog, default: first [data-autofocus], else first
// focusable); on close focus returns to whatever had it before. Backdrop click and Escape both call `onCancel`.
export function useModal(
  dialog: Ref<HTMLDialogElement | null>,
  isOpen: () => boolean,
  onCancel: () => void,
  initialFocus = '[data-autofocus]',
) {
  let previous: HTMLElement | null = null

  function show() {
    const d = dialog.value
    if (!d || d.open) return
    previous = document.activeElement as HTMLElement | null
    d.showModal()
    document.documentElement.classList.add('overflow-hidden')
    nextTick(() => {
      const target =
        d.querySelector<HTMLElement>(initialFocus) ??
        d.querySelector<HTMLElement>('input:not([disabled]), select, textarea, button:not([disabled])')
      target?.focus()
    })
  }

  function hide() {
    const d = dialog.value
    if (d?.open) d.close()
    document.documentElement.classList.remove('overflow-hidden')
    previous?.focus?.()
    previous = null
  }

  watch(
    [isOpen, dialog],
    () => {
      if (isOpen()) show()
      else hide()
    },
    { flush: 'post', immediate: true },
  )
  onBeforeUnmount(hide)

  return {
    // bind on <dialog>: @cancel="m.onNativeCancel" @click="m.onBackdrop"
    onNativeCancel(e: Event) {
      e.preventDefault() // parent decides: it flips `open`, the watcher closes
      onCancel()
    },
    onBackdrop(e: MouseEvent) {
      if (e.target === dialog.value) onCancel()
    },
  }
}
