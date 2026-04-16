import { useEffect } from 'react'

import { AppLink } from '../app/navigation'
import { useNavigate } from '../app/navigation'
import LoadingState from '../shared/ui/LoadingState'

type LegacyRedirectPageProps = {
  to: string
}

export default function LegacyRedirectPage({ to }: LegacyRedirectPageProps) {
  const navigate = useNavigate()

  useEffect(() => {
    navigate(to, { replace: true })
  }, [navigate, to])

  return (
    <section className="state-card">
      <div className="state-copy">
        <span className="section-tag">Legacy route</span>
        <h3>Redirecting to the current pilot shell</h3>
        <p>
          This older entry path is still accepted for pilot compatibility, but the active operator flow now starts in
          the workspace shell.
        </p>
      </div>
      <div className="hero-actions">
        <AppLink className="secondary-button link-button" to={to}>
          Continue now
        </AppLink>
      </div>
      <LoadingState
        title="Redirecting"
        description="If nothing happens automatically, use the continue button above."
        compact
      />
    </section>
  )
}
