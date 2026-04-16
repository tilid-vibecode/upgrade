type ErrorStateProps = {
  title: string
  description: string
  onRetry?: () => void
  compact?: boolean
}

export default function ErrorState({ title, description, onRetry, compact = false }: ErrorStateProps) {
  return (
    <section className={compact ? 'state-card state-card-compact state-card-error' : 'state-card state-card-error'}>
      <div className="state-copy">
        <span className="section-tag">Error</span>
        <h3>{title}</h3>
        <p>{description}</p>
      </div>
      {onRetry ? (
        <div className="hero-actions">
          <button className="secondary-button" onClick={onRetry}>
            Try again
          </button>
        </div>
      ) : null}
    </section>
  )
}
