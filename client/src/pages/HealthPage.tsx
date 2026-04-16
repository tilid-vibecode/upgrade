import { useCallback, useEffect, useState } from 'react'

import { API_BASE_URL, isApiError } from '../shared/api'
import { CollapsibleHero } from '../shared/ui/CollapsibleHero'
import { getDetailedHealth, type DetailedHealthResponse } from '../shared/prototypeApi'
import ErrorState from '../shared/ui/ErrorState'
import LoadingState from '../shared/ui/LoadingState'
import StatusChip from '../shared/ui/StatusChip'

function getHealthErrorDescription(error: unknown) {
  if (isApiError(error) && error.body && typeof error.body === 'object') {
    const degradedResponse = error.body as Partial<DetailedHealthResponse>
    if (Array.isArray(degradedResponse.checks) && typeof degradedResponse.status === 'string') {
      const failingChecks = degradedResponse.checks
        .filter((check) => !check.healthy)
        .map((check) => `${check.service}: ${check.error || 'unhealthy'}`)

      if (failingChecks.length > 0) {
        return `Backend responded with ${degradedResponse.status}. ${failingChecks.join(' | ')}`
      }
    }
  }

  return error instanceof Error ? error.message : 'Failed to reach backend.'
}

export default function HealthPage() {
  const [health, setHealth] = useState<DetailedHealthResponse | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)
  const [lastChecked, setLastChecked] = useState<Date | null>(null)
  const [reloadToken, setReloadToken] = useState(0)

  const fetchHealth = useCallback(async () => {
    setLoading(true)
    setError(null)

    try {
      const response = await getDetailedHealth()
      setHealth(response)
      setLastChecked(new Date())
    } catch (requestError) {
      if (isApiError(requestError) && requestError.body && typeof requestError.body === 'object') {
        const degradedResponse = requestError.body as Partial<DetailedHealthResponse>
        if (
          typeof degradedResponse.status === 'string' &&
          typeof degradedResponse.critical_healthy === 'boolean' &&
          typeof degradedResponse.all_healthy === 'boolean' &&
          Array.isArray(degradedResponse.checks)
        ) {
          setHealth(degradedResponse as DetailedHealthResponse)
          setLastChecked(new Date())
          setError(getHealthErrorDescription(requestError))
          return
        }
      }

      setHealth(null)
      setError(getHealthErrorDescription(requestError))
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    let intervalId = 0

    fetchHealth()
    intervalId = window.setInterval(fetchHealth, 30_000)

    return () => window.clearInterval(intervalId)
  }, [fetchHealth, reloadToken])

  const refreshHealth = () => setReloadToken((value) => value + 1)

  return (
    <div className="page-stack">
      <CollapsibleHero
        tag="Infrastructure"
        title="System health"
      >
        <div className="hero-copy">
          <p>
            This page stays outside the workspace shell so operators always have one environment-debug route, even when
            a workspace payload is failing elsewhere in the pilot.
          </p>
        </div>
        <div className="hero-actions">
          <div className="route-badge">
            <span className="summary-label">API base</span>
            <strong>{API_BASE_URL || 'localhost proxy'}</strong>
          </div>
          <button className="secondary-button" onClick={refreshHealth}>
            Refresh now
          </button>
        </div>
      </CollapsibleHero>

      {loading && !health ? (
        <LoadingState
          title="Checking backend health"
          description="Polling the detailed health endpoint to confirm the app shell can reach the backend stack."
        />
      ) : null}

      {!loading && error ? (
        <ErrorState
          title={health ? 'Health check reported degraded services' : 'Health check failed'}
          description={error}
          onRetry={refreshHealth}
        />
      ) : null}

      {health ? (
        <section className="board-panel">
          <div className="panel-heading">
            <span className="section-tag">Current state</span>
            <h3>Detailed service checks</h3>
            <p>{lastChecked ? `Last checked ${lastChecked.toLocaleTimeString()}.` : 'Awaiting first result.'}</p>
          </div>

          <div className="summary-grid">
            <article className="summary-card">
              <span className="summary-label">Overall status</span>
              <div className="status-row">
                <strong>{health.status.toUpperCase()}</strong>
                <StatusChip status={health.status} />
              </div>
            </article>
            <article className="summary-card">
              <span className="summary-label">Critical services</span>
              <strong>{String(health.critical_healthy)}</strong>
              <p>Critical service health as reported by the backend.</p>
            </article>
            <article className="summary-card">
              <span className="summary-label">All services</span>
              <strong>{String(health.all_healthy)}</strong>
              <p>Overall health across every configured service check.</p>
            </article>
          </div>

          <div className="health-grid">
            {health.checks.map((check) => (
              <article key={check.service} className="info-card">
                <div className="workspace-card-head">
                  <div>
                    <span className="summary-label">{check.critical ? 'Critical' : 'Non-critical'}</span>
                    <h4>{check.service}</h4>
                  </div>
                  <StatusChip status={check.healthy ? 'healthy' : 'failed'} />
                </div>
                <p>{check.error || 'No issues reported.'}</p>
              </article>
            ))}
          </div>
        </section>
      ) : null}
    </div>
  )
}
