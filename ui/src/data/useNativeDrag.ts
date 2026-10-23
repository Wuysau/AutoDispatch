import { useEffect } from 'react'
import { beginNativeDrag, nativeWindow } from './hostBridge'

export function useNativeDrag(ready: boolean) {
  useEffect(() => {
    if (!ready || !nativeWindow) return
    const start = (event: MouseEvent) => {
      const target = event.target instanceof Element ? event.target : null
      if (event.button !== 0 || !target?.closest('.pywebview-drag-region') ||
          target.closest('button, input, select, a')) return
      if (!beginNativeDrag()) return
      // Prevent pywebview's body handler from scheduling an RPC for every move.
      event.preventDefault()
      event.stopImmediatePropagation()
    }
    document.addEventListener('mousedown', start, true)
    return () => document.removeEventListener('mousedown', start, true)
  }, [ready])
}
