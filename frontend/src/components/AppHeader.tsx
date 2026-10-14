import type { ReactNode } from 'react'

interface AppHeaderProps {
  status: ReactNode
}

export function AppHeader({ status }: AppHeaderProps) {
  return (
    <header className="app-header">
      <div className="app-header__brand">
        <span className="app-header__logo" aria-hidden="true">
          P
        </span>
        <div>
          <h1 className="app-header__title">ProxyLead</h1>
          <p className="app-header__subtitle">A manager&apos;s AI proxy — web playground</p>
        </div>
      </div>
      <div className="app-header__status">{status}</div>
    </header>
  )
}
