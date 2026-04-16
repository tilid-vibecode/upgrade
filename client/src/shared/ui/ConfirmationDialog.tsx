/* eslint-disable react-refresh/only-export-components */
import type { ReactNode } from 'react'
import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState } from 'react'

export type ConfirmationTone = 'default' | 'warn' | 'danger'

export type ConfirmationOptions = {
  title: string
  description: string
  details?: string[]
  confirmLabel?: string
  cancelLabel?: string
  tone?: ConfirmationTone
}

type ConfirmationRequest = ConfirmationOptions & {
  id: number
}

type ConfirmationHandler = (options: ConfirmationOptions) => Promise<boolean>

const ConfirmationContext = createContext<ConfirmationHandler | null>(null)

let globalConfirmationHandler: ConfirmationHandler | null = null

function buildNativeFallbackMessage(options: ConfirmationOptions) {
  return [options.title, options.description, ...(options.details || [])].filter(Boolean).join('\n\n')
}

export function requestGlobalConfirmation(options: ConfirmationOptions) {
  if (globalConfirmationHandler) {
    return globalConfirmationHandler(options)
  }

  return Promise.resolve(window.confirm(buildNativeFallbackMessage(options)))
}

export function ConfirmationProvider({ children }: { children: ReactNode }) {
  const [request, setRequest] = useState<ConfirmationRequest | null>(null)
  const resolverRef = useRef<((confirmed: boolean) => void) | null>(null)
  const requestIdRef = useRef(0)
  const cancelButtonRef = useRef<HTMLButtonElement | null>(null)
  const dialogRef = useRef<HTMLElement | null>(null)
  const previousActiveElementRef = useRef<HTMLElement | null>(null)

  const settle = useCallback((confirmed: boolean) => {
    const resolve = resolverRef.current
    resolverRef.current = null
    setRequest(null)
    resolve?.(confirmed)
  }, [])

  const confirm = useCallback((options: ConfirmationOptions) => {
    if (resolverRef.current) {
      resolverRef.current(false)
      resolverRef.current = null
    }

    const requestId = requestIdRef.current + 1
    requestIdRef.current = requestId

    setRequest({
      id: requestId,
      title: options.title,
      description: options.description,
      details: options.details || [],
      confirmLabel: options.confirmLabel || 'Confirm',
      cancelLabel: options.cancelLabel || 'Cancel',
      tone: options.tone || 'default',
    })

    return new Promise<boolean>((resolve) => {
      resolverRef.current = resolve
    })
  }, [])

  useEffect(() => {
    globalConfirmationHandler = confirm
    return () => {
      if (globalConfirmationHandler === confirm) {
        globalConfirmationHandler = null
      }
    }
  }, [confirm])

  useEffect(() => {
    if (request === null) {
      return undefined
    }

    const previousOverflow = document.body.style.overflow
    previousActiveElementRef.current = document.activeElement instanceof HTMLElement ? document.activeElement : null
    document.body.style.overflow = 'hidden'
    cancelButtonRef.current?.focus()

    const getFocusableElements = () => {
      if (!dialogRef.current) {
        return []
      }

      return Array.from(
        dialogRef.current.querySelectorAll<HTMLElement>(
          'button:not([disabled]), [href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])',
        ),
      ).filter((element) => !element.hasAttribute('hidden') && element.getAttribute('aria-hidden') !== 'true')
    }

    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        event.preventDefault()
        settle(false)
        return
      }

      if (event.key !== 'Tab') {
        return
      }

      const focusableElements = getFocusableElements()
      if (focusableElements.length === 0) {
        event.preventDefault()
        dialogRef.current?.focus()
        return
      }

      const firstElement = focusableElements[0]
      const lastElement = focusableElements[focusableElements.length - 1]
      const activeElement = document.activeElement instanceof HTMLElement ? document.activeElement : null

      if (!activeElement || !dialogRef.current?.contains(activeElement)) {
        event.preventDefault()
        firstElement.focus()
        return
      }

      if (event.shiftKey && activeElement === firstElement) {
        event.preventDefault()
        lastElement.focus()
        return
      }

      if (!event.shiftKey && activeElement === lastElement) {
        event.preventDefault()
        firstElement.focus()
      }
    }

    window.addEventListener('keydown', handleKeyDown)
    return () => {
      window.removeEventListener('keydown', handleKeyDown)
      document.body.style.overflow = previousOverflow
      if (previousActiveElementRef.current && document.contains(previousActiveElementRef.current)) {
        previousActiveElementRef.current.focus()
      }
      previousActiveElementRef.current = null
    }
  }, [request, settle])

  const toneLabel = useMemo(() => {
    if (!request) {
      return 'Confirm'
    }

    if (request.tone === 'danger') {
      return 'Destructive action'
    }

    if (request.tone === 'warn') {
      return 'Please confirm'
    }

    return 'Confirm'
  }, [request])

  return (
    <ConfirmationContext.Provider value={confirm}>
      {children}
      {request ? (
        <div
          className="confirm-dialog-scrim"
          role="presentation"
          onClick={() => settle(false)}
        >
          <section
            ref={dialogRef}
            className="confirm-dialog-card"
            role="alertdialog"
            aria-modal="true"
            aria-labelledby={`confirmation-title-${request.id}`}
            aria-describedby={`confirmation-description-${request.id}`}
            tabIndex={-1}
            onClick={(event) => event.stopPropagation()}
          >
            <div className="confirm-dialog-copy">
              <span className="section-tag">{toneLabel}</span>
              <h3 id={`confirmation-title-${request.id}`}>{request.title}</h3>
              <p id={`confirmation-description-${request.id}`}>{request.description}</p>
              {request.details && request.details.length > 0 ? (
                <ul className="inline-detail-list">
                  {request.details.map((detail) => (
                    <li key={detail}>{detail}</li>
                  ))}
                </ul>
              ) : null}
            </div>

            <div className="confirm-dialog-actions">
              <button
                ref={cancelButtonRef}
                className="secondary-button"
                type="button"
                onClick={() => settle(false)}
              >
                {request.cancelLabel}
              </button>
              <button
                className={request.tone === 'danger' ? 'danger-button' : 'primary-button'}
                type="button"
                onClick={() => settle(true)}
              >
                {request.confirmLabel}
              </button>
            </div>
          </section>
        </div>
      ) : null}
    </ConfirmationContext.Provider>
  )
}

export function useConfirmation() {
  const value = useContext(ConfirmationContext)
  if (value === null) {
    throw new Error('Confirmation hooks must be used inside ConfirmationProvider.')
  }
  return value
}
