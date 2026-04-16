import { AppLink } from '../app/navigation'

export default function NotFoundPage() {
  return (
    <section className="state-card">
      <div className="state-copy">
        <span className="section-tag">Not found</span>
        <h3>This route is outside the current pilot map.</h3>
        <p>
          The app recognizes the operator workspace shell, the public assessment route, and the health route. Unknown
          or retired paths fall back here intentionally so the pilot does not land on stale placeholder UI.
        </p>
      </div>
      <div className="hero-actions">
        <AppLink className="primary-button link-button" to="/">
          Return to workspaces
        </AppLink>
      </div>
    </section>
  )
}
