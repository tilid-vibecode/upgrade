import { useEffect, useRef, useState, type FormEvent } from 'react'

import { AppLink, useNavigate } from '../app/navigation'
import { getApiErrorMessage, getApiErrorMessages } from '../shared/api'
import { formatDateTime } from '../shared/formatters'
import {
  createPrototypeWorkspace,
  listPrototypeWorkspaces,
  type PrototypeWorkspaceSummary,
} from '../shared/prototypeApi'
import {
  buildWorkspacePath,
  buildWorkspaceSlugCandidate,
} from '../shared/workflow'
import EmptyState from '../shared/ui/EmptyState'
import ErrorState from '../shared/ui/ErrorState'
import LoadingState from '../shared/ui/LoadingState'
import StatusChip from '../shared/ui/StatusChip'

export default function WorkspacesHomePage() {
  const navigate = useNavigate()
  const isMountedRef = useRef(true)
  const [workspaces, setWorkspaces] = useState<PrototypeWorkspaceSummary[]>([])
  const [listLoading, setListLoading] = useState(true)
  const [listError, setListError] = useState<unknown>(null)
  const [reloadToken, setReloadToken] = useState(0)
  const [companyName, setCompanyName] = useState('')
  const [submitLoading, setSubmitLoading] = useState(false)
  const [submitError, setSubmitError] = useState<unknown>(null)

  useEffect(() => {
    isMountedRef.current = true

    return () => {
      isMountedRef.current = false
    }
  }, [])

  useEffect(() => {
    let cancelled = false

    async function loadWorkspaces() {
      setListLoading(true)
      setListError(null)

      try {
        const response = await listPrototypeWorkspaces()
        if (!cancelled) {
          setWorkspaces(response)
        }
      } catch (requestError) {
        if (!cancelled) {
          setListError(requestError)
        }
      } finally {
        if (!cancelled) {
          setListLoading(false)
        }
      }
    }

    loadWorkspaces()

    return () => {
      cancelled = true
    }
  }, [reloadToken])

  const trimmedCompanyName = companyName.trim()
  const candidateSlug = trimmedCompanyName ? buildWorkspaceSlugCandidate(trimmedCompanyName) : ''
  const likelyExistingWorkspace = candidateSlug
    ? workspaces.find((workspace) => workspace.slug === candidateSlug)
    : null
  const submitErrorMessages = submitError ? getApiErrorMessages(submitError) : []

  const refreshList = () => setReloadToken((value) => value + 1)

  const handleSubmit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault()

    if (!trimmedCompanyName) {
      setSubmitError(new Error('Company name is required.'))
      return
    }

    setSubmitLoading(true)
    setSubmitError(null)

    try {
      const workspace = await createPrototypeWorkspace({
        company_name: trimmedCompanyName,
      })
      if (isMountedRef.current) {
        navigate(buildWorkspacePath(workspace.slug))
      }
    } catch (requestError) {
      if (isMountedRef.current) {
        setSubmitError(requestError)
      }
    } finally {
      if (isMountedRef.current) {
        setSubmitLoading(false)
      }
    }
  }

  return (
    <div className="page-stack">
      <section className="hero-panel">
        <div className="hero-copy">
          <span className="section-tag">Operator entry</span>
          <h2>Open an existing workspace or start a new one from the same entry point.</h2>
          <p>
            The backend already reopens same-name workspaces by slug, so this entry flow intentionally uses
            create-or-open language instead of treating every submit as a brand new company.
          </p>
        </div>
        <div className="hero-actions">
          <div className="route-badge">
            <span className="summary-label">Current route</span>
            <strong>/</strong>
          </div>
          <div className="route-badge">
            <span className="summary-label">After submit</span>
            <strong>/workspaces/:workspaceSlug</strong>
          </div>
        </div>
      </section>

      <section className="workspace-entry-grid">
        <section className="board-panel">
          <div className="panel-heading">
            <span className="section-tag">Create or reopen</span>
            <h3>Start with the company name</h3>
            <p>
              If the company already has a workspace, the backend will reopen it instead of creating a duplicate.
            </p>
          </div>

          {likelyExistingWorkspace ? (
            <div className="inline-banner inline-banner-info">
              <strong>Likely existing workspace:</strong> {likelyExistingWorkspace.name}
              <span>Submitting will reopen `{likelyExistingWorkspace.slug}` if it is the same company.</span>
              <div className="hero-actions">
                <AppLink
                  className="secondary-button link-button"
                  to={buildWorkspacePath(likelyExistingWorkspace.slug)}
                >
                  Open existing workspace
                </AppLink>
              </div>
            </div>
          ) : null}

          {submitError ? (
            <div className="inline-banner inline-banner-error">
              <strong>{getApiErrorMessage(submitError, 'Workspace request failed.')}</strong>
              {submitErrorMessages.length > 1 ? (
                <ul className="inline-detail-list">
                  {submitErrorMessages.slice(1).map((message) => (
                    <li key={message}>{message}</li>
                  ))}
                </ul>
              ) : null}
            </div>
          ) : null}

          <form className="workspace-create-form" onSubmit={handleSubmit}>
            <label className="field-label">
              <span>Company name</span>
              <input
                className="text-input"
                name="company_name"
                value={companyName}
                onChange={(event) => setCompanyName(event.target.value)}
                placeholder="Acme Cloud"
                autoComplete="organization"
                disabled={submitLoading}
              />
            </label>

            <div className="form-actions">
              <button className="primary-button" type="submit" disabled={submitLoading}>
                {submitLoading ? 'Opening workspace...' : 'Create or open workspace'}
              </button>
              <span className="form-helper-copy">
                This entry flow only chooses the workspace. Profile notes and richer context editing begin in stage 03.
              </span>
            </div>
          </form>
        </section>

        <section className="board-panel">
          <div className="panel-heading">
            <span className="section-tag">Existing workspaces</span>
            <h3>Reopen a workspace directly</h3>
            <p>
              The entry page lists workspaces in latest-update order so operators can jump back into active pilots quickly.
            </p>
          </div>

          {listLoading ? (
            <LoadingState
              title="Loading workspaces"
              description="Fetching the prototype workspace list from the backend."
            />
          ) : null}

          {!listLoading && listError ? (
            <ErrorState
              title="Workspace list failed to load"
              description={getApiErrorMessage(listError, 'The workspace list is unavailable right now.')}
              onRetry={refreshList}
              compact
            />
          ) : null}

          {!listLoading && !listError && workspaces.length === 0 ? (
            <EmptyState
              title="No workspaces yet"
              description="Create the first workspace from the form on the left to begin the operator workflow."
              action={
                <button className="secondary-button" onClick={refreshList}>
                  Refresh list
                </button>
              }
            />
          ) : null}

          {!listLoading && !listError && workspaces.length > 0 ? (
            <div className="workspace-card-grid">
              {workspaces.map((workspace) => {
                const isLikelyMatch = workspace.slug === candidateSlug && candidateSlug !== ''

                return (
                  <article key={workspace.uuid} className="workspace-card">
                    <div className="workspace-card-head">
                      <div>
                        <span className="section-tag">Workspace</span>
                        <h4>{workspace.name}</h4>
                      </div>
                      <StatusChip status={workspace.status} />
                    </div>

                    <div className="workspace-card-meta">
                      <div>
                        <span className="summary-label">Slug</span>
                        <strong>{workspace.slug}</strong>
                      </div>
                      <div>
                        <span className="summary-label">Updated</span>
                        <strong>{formatDateTime(workspace.updated_at)}</strong>
                      </div>
                    </div>

                    {isLikelyMatch ? (
                      <span className="quiet-pill">Likely match for the current company name</span>
                    ) : null}

                    <div className="hero-actions">
                      <AppLink className="primary-button link-button" to={buildWorkspacePath(workspace.slug)}>
                        Open workspace
                      </AppLink>
                    </div>
                  </article>
                )
              })}
            </div>
          ) : null}
        </section>
      </section>
    </div>
  )
}
