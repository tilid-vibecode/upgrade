import { useCallback, useEffect, useRef, type ReactNode } from 'react'

type SlideOverPanelProps = {
  title: string
  open: boolean
  onClose: () => void
  children: ReactNode
  wide?: boolean
}

export default function SlideOverPanel({ title, open, onClose, children, wide = false }: SlideOverPanelProps) {
  const panelRef = useRef<HTMLDivElement | null>(null)
  const previousActiveElementRef = useRef<HTMLElement | null>(null)

  const handleKeyDown = useCallback(
    (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        event.preventDefault()
        onClose()
      }
    },
    [onClose],
  )

  useEffect(() => {
    if (!open) {
      return undefined
    }

    previousActiveElementRef.current = document.activeElement instanceof HTMLElement ? document.activeElement : null
    const previousOverflow = document.body.style.overflow
    document.body.style.overflow = 'hidden'

    window.addEventListener('keydown', handleKeyDown)

    return () => {
      window.removeEventListener('keydown', handleKeyDown)
      document.body.style.overflow = previousOverflow
      if (previousActiveElementRef.current && document.contains(previousActiveElementRef.current)) {
        previousActiveElementRef.current.focus()
      }
      previousActiveElementRef.current = null
    }
  }, [open, handleKeyDown])

  if (!open) {
    return null
  }

  return (
    <>
      <div className="slide-over-scrim" onClick={onClose} />
      <div
        ref={panelRef}
        className="slide-over-panel"
        style={wide ? { width: 'min(90vw, 860px)' } : undefined}
        role="dialog"
        aria-modal="true"
        aria-label={title}
      >
        <div className="slide-over-header">
          <h3>{title}</h3>
          <button
            className="slide-over-close"
            onClick={onClose}
            aria-label="Close panel"
            type="button"
          >
            &times;
          </button>
        </div>
        <div className="slide-over-body">{children}</div>
      </div>
    </>
  )
}
