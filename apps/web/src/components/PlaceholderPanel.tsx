import type { ReactNode } from 'react'

interface PlaceholderPanelProps {
  title: string
  children: ReactNode
  className?: string
}

/** A clearly labeled panel for functionality that is not built yet. */
export function PlaceholderPanel({ title, children, className }: PlaceholderPanelProps) {
  return (
    <section className={['panel', className].filter(Boolean).join(' ')} aria-label={title}>
      <div className="panel__header">
        <h2 className="panel__title">{title}</h2>
        <span className="badge">Placeholder</span>
      </div>
      <div className="panel__body">{children}</div>
    </section>
  )
}
