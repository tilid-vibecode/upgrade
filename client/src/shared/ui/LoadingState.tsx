type LoadingStateProps = {
  title: string
  description?: string
  compact?: boolean
}

export default function LoadingState({ title, description, compact = false }: LoadingStateProps) {
  return (
    <section className={compact ? 'state-card state-card-compact' : 'state-card'}>
      <div className="state-pulse" aria-hidden="true" />
      <div className="state-copy">
        <span className="section-tag">Loading</span>
        <h3>{title}</h3>
        {description ? <p>{description}</p> : null}
      </div>
    </section>
  )
}
