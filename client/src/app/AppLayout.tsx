import type { ReactNode } from 'react'

import { AppLink } from './navigation'
import { usePathname } from './navigation'

function isActive(pathname: string, target: string) {
  if (target === '/') {
    return pathname === '/' || pathname.startsWith('/workspaces/')
  }

  return pathname === target
}

export default function AppLayout({ children }: { children: ReactNode }) {
  const pathname = usePathname()

  return (
    <div className="app-shell">
      <header className="operator-header">
        <div className="brand-block">
          <svg width="20" height="20" viewBox="0 0 20 20" fill="none" aria-hidden="true">
            <rect width="20" height="20" rx="5" fill="#4f46e5" />
            <path d="M6 10l3 3 5-6" stroke="#fff" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" />
          </svg>
          <h1 className="brand-title">Upgrade Pilot</h1>
        </div>

        <nav className="operator-nav" aria-label="Primary">
          <AppLink
            to="/"
            className={isActive(pathname, '/') ? 'nav-pill is-active' : 'nav-pill'}
          >
            Workspaces
          </AppLink>
          <AppLink
            to="/health"
            className={isActive(pathname, '/health') ? 'nav-pill is-active' : 'nav-pill'}
          >
            Health
          </AppLink>
        </nav>
      </header>

      <main className="operator-main">{children}</main>
    </div>
  )
}
