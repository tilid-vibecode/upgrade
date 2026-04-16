import type { ReactNode } from 'react'

type EmptyStateProps = {
  title: string
  description: string
  action?: ReactNode
}

export default function EmptyState({ title, description, action }: EmptyStateProps) {
  return (
    <section className="state-card state-card-empty">
      <div className="state-copy">
        <span className="section-tag">Empty</span>
        <h3>{title}</h3>
        <p>{description}</p>
      </div>
      {action ? <div className="hero-actions">{action}</div> : null}
    </section>
  )
}
