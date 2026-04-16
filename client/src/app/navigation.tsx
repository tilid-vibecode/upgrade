/* eslint-disable react-refresh/only-export-components */
import type { AnchorHTMLAttributes, MouseEvent, ReactNode } from 'react'
import { createContext, startTransition, useCallback, useContext, useEffect, useRef, useState } from 'react'

import { requestGlobalConfirmation } from '../shared/ui/ConfirmationDialog'

type NavigateOptions = {
  replace?: boolean
  bypassBlockers?: boolean
}

type NavigationLocation = {
  href: string
  pathname: string
  search: string
  hash: string
}

type NavigationBlocker = () => string | null

type PendingBlockedPopNavigation = {
  target: NavigationLocation
  delta: number | null
  message: string
}

const HISTORY_INDEX_KEY = '__appNavIndex'

type NavigationContextValue = {
  location: NavigationLocation
  navigate: (to: string, options?: NavigateOptions) => void
  registerBlocker: (blocker: NavigationBlocker) => () => void
}

const NavigationContext = createContext<NavigationContextValue | null>(null)

function normalizePathname(pathname: string): string {
  if (!pathname || pathname === '') {
    return '/'
  }

  if (pathname.length > 1 && pathname.endsWith('/')) {
    return pathname.slice(0, -1)
  }

  return pathname
}

function resolveTargetUrl(to: string) {
  const url = new URL(to, window.location.origin)
  return {
    href: `${url.pathname}${url.search}${url.hash}`,
    pathname: normalizePathname(url.pathname),
    search: url.search,
    hash: url.hash,
  }
}

function readCurrentLocation(): NavigationLocation {
  return {
    href: `${window.location.pathname}${window.location.search}${window.location.hash}`,
    pathname: normalizePathname(window.location.pathname),
    search: window.location.search,
    hash: window.location.hash,
  }
}

function isExternalTarget(to: string) {
  try {
    const url = new URL(to, window.location.origin)
    return url.origin !== window.location.origin
  } catch {
    return false
  }
}

function readHistoryIndex(state: unknown) {
  if (typeof state !== 'object' || state === null || !(HISTORY_INDEX_KEY in state)) {
    return null
  }

  const value = (state as Record<string, unknown>)[HISTORY_INDEX_KEY]
  return typeof value === 'number' ? value : null
}

function buildHistoryState(index: number, currentState: unknown) {
  if (typeof currentState === 'object' && currentState !== null) {
    return {
      ...(currentState as Record<string, unknown>),
      [HISTORY_INDEX_KEY]: index,
    }
  }

  return {
    [HISTORY_INDEX_KEY]: index,
  }
}

export function NavigationProvider({ children }: { children: ReactNode }) {
  const [location, setLocation] = useState<NavigationLocation>(() => readCurrentLocation())
  const locationRef = useRef(location)
  const blockersRef = useRef(new Set<NavigationBlocker>())
  const historyIndexRef = useRef(readHistoryIndex(window.history.state) ?? 0)
  const restoringNavigationRef = useRef(false)
  const bypassNextBlockedPopRef = useRef(false)
  const pendingBlockedPopNavigationRef = useRef<PendingBlockedPopNavigation | null>(null)
  const blockedPopConfirmationInFlightRef = useRef(false)

  locationRef.current = location

  const getBlockerMessage = useCallback(() => {
    for (const blocker of blockersRef.current) {
      const message = blocker()
      if (message) {
        return message
      }
    }

    return null
  }, [])

  const commitNavigation = useCallback((target: NavigationLocation, options?: NavigateOptions) => {
    const nextIndex = options?.replace ? historyIndexRef.current : historyIndexRef.current + 1
    const nextState = buildHistoryState(nextIndex, window.history.state)

    if (options?.replace) {
      window.history.replaceState(nextState, '', target.href)
    } else {
      window.history.pushState(nextState, '', target.href)
    }

    historyIndexRef.current = nextIndex
    window.scrollTo({ top: 0, behavior: 'auto' })
    startTransition(() => {
      setLocation(target)
    })
  }, [])

  useEffect(() => {
    if (readHistoryIndex(window.history.state) === null) {
      window.history.replaceState(buildHistoryState(historyIndexRef.current, window.history.state), '', locationRef.current.href)
    }

    const handlePopState = () => {
      const targetIndex = readHistoryIndex(window.history.state)
      const target = readCurrentLocation()
      const restoreBlockedPopNavigation = () => {
        restoringNavigationRef.current = true

        if (targetIndex !== null && targetIndex < historyIndexRef.current) {
          window.history.forward()
        } else if (targetIndex !== null && targetIndex > historyIndexRef.current) {
          window.history.back()
        } else {
          window.history.go(1)
        }
      }

      if (restoringNavigationRef.current) {
        restoringNavigationRef.current = false
        if (targetIndex !== null) {
          historyIndexRef.current = targetIndex
        }

        startTransition(() => {
          setLocation(readCurrentLocation())
        })

        const pendingNavigation = pendingBlockedPopNavigationRef.current
        if (pendingNavigation && !blockedPopConfirmationInFlightRef.current) {
          blockedPopConfirmationInFlightRef.current = true
          window.setTimeout(() => {
            void requestGlobalConfirmation({
              title: 'Leave this page?',
              description: pendingNavigation.message,
              confirmLabel: 'Leave page',
              cancelLabel: 'Stay here',
              tone: 'warn',
            }).then((confirmed) => {
              blockedPopConfirmationInFlightRef.current = false
              const queuedNavigation = pendingBlockedPopNavigationRef.current
              pendingBlockedPopNavigationRef.current = null
              if (!confirmed || !queuedNavigation) {
                return
              }

              if (queuedNavigation.delta !== null) {
                bypassNextBlockedPopRef.current = true
                window.history.go(queuedNavigation.delta)
                return
              }

              commitNavigation(queuedNavigation.target)
            })
          }, 0)
        }
        return
      }

      if (bypassNextBlockedPopRef.current) {
        bypassNextBlockedPopRef.current = false
        if (targetIndex !== null) {
          historyIndexRef.current = targetIndex
        }

        startTransition(() => {
          setLocation(target)
        })
        return
      }

      const blockerMessage = getBlockerMessage()
      if (blockerMessage) {
        if (pendingBlockedPopNavigationRef.current || blockedPopConfirmationInFlightRef.current) {
          restoreBlockedPopNavigation()
          return
        }

        pendingBlockedPopNavigationRef.current = {
          target,
          delta: targetIndex !== null ? targetIndex - historyIndexRef.current : null,
          message: blockerMessage,
        }
        restoreBlockedPopNavigation()
        return
      }

      if (targetIndex !== null) {
        historyIndexRef.current = targetIndex
      }

      startTransition(() => {
        setLocation(readCurrentLocation())
      })
    }

    window.addEventListener('popstate', handlePopState)
    return () => window.removeEventListener('popstate', handlePopState)
  }, [commitNavigation, getBlockerMessage])

  useEffect(() => {
    const handleBeforeUnload = (event: BeforeUnloadEvent) => {
      const blockerMessage = getBlockerMessage()
      if (!blockerMessage) {
        return undefined
      }

      event.preventDefault()
      event.returnValue = blockerMessage
      return blockerMessage
    }

    window.addEventListener('beforeunload', handleBeforeUnload)
    return () => window.removeEventListener('beforeunload', handleBeforeUnload)
  }, [getBlockerMessage])

  const navigate = (to: string, options?: NavigateOptions) => {
    const target = resolveTargetUrl(to)
    if (target.href === locationRef.current.href) {
      return
    }

    if (options?.bypassBlockers) {
      commitNavigation(target, options)
      return
    }

    const blockerMessage = getBlockerMessage()
    if (blockerMessage) {
      void requestGlobalConfirmation({
        title: 'Leave this page?',
        description: blockerMessage,
        confirmLabel: 'Leave page',
        cancelLabel: 'Stay here',
        tone: 'warn',
      }).then((confirmed) => {
        if (confirmed) {
          commitNavigation(target, options)
        }
      })
      return
    }

    commitNavigation(target, options)
  }

  const registerBlocker = (blocker: NavigationBlocker) => {
    blockersRef.current.add(blocker)

    return () => {
      blockersRef.current.delete(blocker)
    }
  }

  return (
    <NavigationContext.Provider
      value={{
        location,
        navigate,
        registerBlocker,
      }}
    >
      {children}
    </NavigationContext.Provider>
  )
}

function useNavigationContext() {
  const value = useContext(NavigationContext)
  if (value === null) {
    throw new Error('Navigation hooks must be used inside NavigationProvider.')
  }
  return value
}

export function usePathname() {
  return useNavigationContext().location.pathname
}

export function useLocation() {
  return useNavigationContext().location
}

export function useNavigate() {
  return useNavigationContext().navigate
}

export function useNavigationBlocker(enabled: boolean, message: string) {
  const { registerBlocker } = useNavigationContext()

  useEffect(() => {
    if (!enabled) {
      return undefined
    }

    return registerBlocker(() => message)
  }, [enabled, message, registerBlocker])
}

type AppLinkProps = Omit<AnchorHTMLAttributes<HTMLAnchorElement>, 'href'> & {
  to: string
  replace?: boolean
}

export function AppLink({ to, replace, onClick, children, ...rest }: AppLinkProps) {
  const navigate = useNavigate()

  const handleClick = (event: MouseEvent<HTMLAnchorElement>) => {
    onClick?.(event)
    if (
      event.defaultPrevented ||
      event.button !== 0 ||
      rest.download !== undefined ||
      rest.target === '_blank' ||
      isExternalTarget(to) ||
      event.metaKey ||
      event.ctrlKey ||
      event.shiftKey ||
      event.altKey
    ) {
      return
    }

    event.preventDefault()
    navigate(to, { replace })
  }

  return (
    <a href={to} onClick={handleClick} {...rest}>
      {children}
    </a>
  )
}

export { normalizePathname }
