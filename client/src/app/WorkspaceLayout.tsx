/* eslint-disable react-refresh/only-export-components */
import type { ReactNode } from 'react'
import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState } from 'react'

import { AppLink, useLocation, useNavigate } from './navigation'
import { formatDateTime, formatPercent, truncateText } from '../shared/formatters'
import { getApiErrorMessage } from '../shared/api'
import {
  getPrototypeWorkflowStatus,
  listPrototypePlanningContexts,
  type PrototypePlanningContextOptions,
  type PrototypePlanningContextSummary,
  type PrototypeWorkflowStatusResponse,
  type PrototypeWorkspaceDetail,
} from '../shared/prototypeApi'
import {
  buildWorkspacePath,
  getWorkspaceNavItem,
  humanizeToken,
  readWorkspaceContextSlug,
  WORKSPACE_NAV_ITEMS,
  type WorkspacePageKey,
} from '../shared/workflow'
import LoadingState from '../shared/ui/LoadingState'
import ErrorState from '../shared/ui/ErrorState'
import StatusChip from '../shared/ui/StatusChip'

type WorkspaceShellContextValue = {
  workspace: PrototypeWorkspaceDetail
  workflow: PrototypeWorkflowStatusResponse
  activePage: WorkspacePageKey
  planningContexts: PrototypePlanningContextSummary[]
  activePlanningContext: PrototypePlanningContextSummary | null
  planningContextOptions: PrototypePlanningContextOptions | undefined
  contextWarning: string | null
  refreshShell: (options?: { contextSlug?: string | null }) => Promise<void>
  setActivePlanningContextSlug: (contextSlug: string | null, options?: { replace?: boolean }) => void
  buildScopedWorkspacePath: (pageKey: WorkspacePageKey, options?: { contextSlug?: string | null }) => string
}

const WorkspaceShellContext = createContext<WorkspaceShellContextValue | null>(null)

export function useWorkspaceShell() {
  const value = useContext(WorkspaceShellContext)
  if (value === null) {
    throw new Error('Workspace shell hooks must be used inside WorkspaceLayout.')
  }
  return value
}

type WorkspaceLayoutProps = {
  workspaceSlug: string
  activePage: WorkspacePageKey
  children: ReactNode
}

export default function WorkspaceLayout({ workspaceSlug, activePage, children }: WorkspaceLayoutProps) {
  const location = useLocation()
  const navigate = useNavigate()
  const requestedContextSlug = readWorkspaceContextSlug(location.search)
  const [workflow, setWorkflow] = useState<PrototypeWorkflowStatusResponse | null>(null)
  const [planningContexts, setPlanningContexts] = useState<PrototypePlanningContextSummary[]>([])
  const [activePlanningContext, setActivePlanningContext] = useState<PrototypePlanningContextSummary | null>(null)
  const [contextWarning, setContextWarning] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [summaryExpanded, setSummaryExpanded] = useState(false)
  const shellRequestIdRef = useRef(0)

  const buildScopedWorkspacePath = useCallback(
    (pageKey: WorkspacePageKey, options: { contextSlug?: string | null } = {}) =>
      buildWorkspacePath(workspaceSlug, pageKey, {
        contextSlug:
          options.contextSlug !== undefined
            ? options.contextSlug
            : requestedContextSlug,
      }),
    [requestedContextSlug, workspaceSlug],
  )

  const setActivePlanningContextSlug = useCallback(
    (contextSlug: string | null, options: { replace?: boolean } = {}) => {
      navigate(
        buildWorkspacePath(workspaceSlug, activePage, {
          contextSlug,
        }),
        { replace: options.replace },
      )
    },
    [activePage, navigate, workspaceSlug],
  )

  const refresh = useCallback(async (options: { contextSlug?: string | null } = {}) => {
    const requestId = shellRequestIdRef.current + 1
    shellRequestIdRef.current = requestId

    setLoading(true)
    setError(null)

    try {
      const effectiveRequestedContextSlug =
        options.contextSlug !== undefined ? options.contextSlug : readWorkspaceContextSlug(location.search)
      const contextsResponse = await listPrototypePlanningContexts(workspaceSlug)

      if (shellRequestIdRef.current !== requestId) {
        return
      }

      const nextPlanningContexts = contextsResponse.contexts
      const nextActivePlanningContext = effectiveRequestedContextSlug
        ? nextPlanningContexts.find((context) => context.slug === effectiveRequestedContextSlug) ?? null
        : null
      let nextContextSlug = effectiveRequestedContextSlug

      setPlanningContexts(nextPlanningContexts)
      setActivePlanningContext(nextActivePlanningContext)

      if (effectiveRequestedContextSlug && nextActivePlanningContext === null) {
        setContextWarning(`Planning context "${effectiveRequestedContextSlug}" is no longer available. Showing the legacy workspace scope instead.`)
        nextContextSlug = null
        navigate(buildWorkspacePath(workspaceSlug, activePage), { replace: true, bypassBlockers: true })
      } else {
        setContextWarning(null)
      }

      const workflowResponse = await getPrototypeWorkflowStatus(
        workspaceSlug,
        nextActivePlanningContext
          ? {
              planningContextUuid: nextActivePlanningContext.uuid,
            }
          : undefined,
      )

      if (shellRequestIdRef.current !== requestId) {
        return
      }

      if (nextContextSlug === null) {
        setActivePlanningContext(null)
      }
      setWorkflow(workflowResponse)
    } catch (requestError) {
      if (shellRequestIdRef.current !== requestId) {
        return
      }

      setError(getApiErrorMessage(requestError, 'Failed to load workspace shell.'))
      throw requestError
    } finally {
      if (shellRequestIdRef.current === requestId) {
        setLoading(false)
      }
    }
  }, [activePage, location.search, navigate, workspaceSlug])

  useEffect(() => {
    void refresh().catch(() => undefined)
  }, [refresh])

  const triggerRefresh = () => {
    void refresh().catch(() => undefined)
  }

  const activeItem = getWorkspaceNavItem(activePage)
  const workspace: PrototypeWorkspaceDetail | null = workflow?.workspace ?? null
  const planningContextOptions = useMemo(
    () =>
      activePlanningContext
        ? {
            planningContextUuid: activePlanningContext.uuid,
          }
        : undefined,
    [activePlanningContext],
  )
  const currentStage = workflow?.stages.find((stage) => stage.key === workflow.summary.current_stage_key) ?? null
  const nextStage = workflow?.stages.find((stage) => stage.key === workflow.summary.next_stage_key) ?? null
  const nextStageLabel = workflow?.summary.next_stage_key
    ? nextStage?.label || humanizeToken(workflow.summary.next_stage_key)
    : 'Complete'
  const currentStageLabel = currentStage?.label || 'Complete'
  const scopeLabel = activePlanningContext?.name || 'Legacy workspace'
  const scopeSupportingCopy = activePlanningContext
    ? `${humanizeToken(activePlanningContext.kind)} context · ${humanizeToken(activePlanningContext.status)}`
    : 'Workspace baseline without a selected planning context'

  if (loading && (workspace === null || workflow === null)) {
    return (
      <section className="workspace-shell">
        <aside className="workspace-sidebar">
          <div className="workspace-sidebar-card">
            <span className="section-tag">Workspace</span>
            <h2>{workspaceSlug}</h2>
            <p>Loading workspace context...</p>
          </div>
        </aside>
        <div className="workspace-content">
          <LoadingState
            title="Loading workspace"
            description="Fetching stage summary, workflow status, and planning contexts."
          />
        </div>
      </section>
    )
  }

  if (workspace === null || workflow === null) {
    return (
      <section className="workspace-shell">
        <aside className="workspace-sidebar">
          <div className="workspace-sidebar-card">
            <span className="section-tag">Workspace</span>
            <h2>{workspaceSlug}</h2>
          </div>
        </aside>
        <div className="workspace-content">
          <ErrorState
            title="Failed to load workspace"
            description={error || 'The workspace data was unavailable.'}
            onRetry={triggerRefresh}
          />
        </div>
      </section>
    )
  }

  return (
    <WorkspaceShellContext.Provider
      value={{
        workspace,
        workflow,
        activePage,
        planningContexts,
        activePlanningContext,
        planningContextOptions,
        contextWarning,
        refreshShell: refresh,
        setActivePlanningContextSlug,
        buildScopedWorkspacePath,
      }}
    >
      <section className="workspace-shell">
        <aside className="workspace-sidebar">
          <div className="workspace-sidebar-card">
            <span className="section-tag">Workspace</span>
            <h2>{workspace.name}</h2>
            <p>{truncateText(workspace.operator_notes || workspace.notes || 'No notes yet.', 80)}</p>
            <div className="sidebar-meta">
              <div>
                <span className="summary-label">Slug</span>
                <strong>{workspace.slug}</strong>
              </div>
              <div>
                <span className="summary-label">Updated</span>
                <strong>{formatDateTime(workspace.updated_at)}</strong>
              </div>
            </div>
          </div>

          <div className="workspace-sidebar-card">
            <span className="section-tag">Planning Scope</span>
            <h2>{scopeLabel}</h2>
            <p>{scopeSupportingCopy}</p>
            <div className="sidebar-meta">
              <div>
                <span className="summary-label">Available contexts</span>
                <strong>{planningContexts.length}</strong>
              </div>
              {activePlanningContext ? (
                <div>
                  <span className="summary-label">Sources in scope</span>
                  <strong>{activePlanningContext.source_count}</strong>
                </div>
              ) : null}
            </div>
            <div className="form-actions">
              <AppLink className="secondary-button link-button" to={buildScopedWorkspacePath('contexts')}>
                Manage contexts
              </AppLink>
            </div>
          </div>

          <nav className="workspace-stage-nav" aria-label="Workspace stages">
            {WORKSPACE_NAV_ITEMS.map((item) => (
              <AppLink
                key={item.key}
                to={buildScopedWorkspacePath(item.key)}
                className={item.key === activePage ? 'stage-link is-active' : 'stage-link'}
              >
                <span className="stage-link-label">{item.label}</span>
                {item.key === 'overview' ? null : <span className="stage-link-hint">{item.nextStageLabel}</span>}
              </AppLink>
            ))}
          </nav>
        </aside>

        <div className="workspace-content">
          <section className={`workspace-summary-band ${summaryExpanded ? 'is-expanded' : 'is-collapsed'}`}>
            <div className="summary-head">
              <button
                type="button"
                className="summary-head-toggle"
                onClick={() => setSummaryExpanded((prev) => !prev)}
                aria-expanded={summaryExpanded}
              >
                <span className="section-tag">{activeItem.label}</span>
                <span className="summary-head-title">{activeItem.description}</span>
                <StatusChip status={currentStage?.status || 'completed'} />
                <span className="hero-collapse-icon" aria-hidden="true">{summaryExpanded ? '▲' : '▼'}</span>
              </button>
              <div className="hero-actions">
                <button className="secondary-button" onClick={triggerRefresh}>
                  Refresh
                </button>
              </div>
            </div>

            {summaryExpanded ? (
              <>
                {contextWarning ? (
                  <section className="inline-banner inline-banner-warn">
                    <strong>Scope updated</strong>
                    <span>{contextWarning}</span>
                  </section>
                ) : null}

                {error ? (
                  <ErrorState
                    title="Refresh failed"
                    description={`${error} Showing last successful data.`}
                    onRetry={triggerRefresh}
                    compact
                  />
                ) : null}

                <div className="summary-grid">
                  <article className="summary-card">
                    <span className="summary-label">Current scope</span>
                    <strong>{scopeLabel}</strong>
                    <p>{scopeSupportingCopy}</p>
                  </article>
                  <article className="summary-card">
                    <span className="summary-label">Current stage</span>
                    <strong>{currentStageLabel}</strong>
                    <StatusChip status={currentStage?.status || 'completed'} />
                  </article>
                  <article className="summary-card">
                    <span className="summary-label">Next stage</span>
                    <strong>{nextStageLabel}</strong>
                    <p>{workflow.summary.total_blocker_count} blocker(s)</p>
                  </article>
                  <article className="summary-card">
                    <span className="summary-label">Blueprint</span>
                    <strong>{workflow.summary.latest_blueprint_status ? humanizeToken(workflow.summary.latest_blueprint_status) : 'Not started'}</strong>
                    <p>{workflow.summary.blueprint_published ? 'Published' : 'Not published'}</p>
                  </article>
                  <article className="summary-card">
                    <span className="summary-label">Assessments</span>
                    <strong>{workflow.summary.latest_assessment_status ? humanizeToken(workflow.summary.latest_assessment_status) : 'Not started'}</strong>
                    <p>{formatPercent(workflow.summary.assessment_completion_rate)} completion</p>
                  </article>
                  <article className="summary-card">
                    <span className="summary-label">Matrix</span>
                    <strong>{workflow.summary.latest_matrix_status ? humanizeToken(workflow.summary.latest_matrix_status) : 'Not started'}</strong>
                  </article>
                  <article className="summary-card">
                    <span className="summary-label">Plans</span>
                    <strong>{workflow.summary.latest_plan_status ? humanizeToken(workflow.summary.latest_plan_status) : 'Not started'}</strong>
                  </article>
                </div>

                <div className="workflow-strip" aria-label="Workflow stages">
                  {workflow.stages.map((stage) => (
                    <article key={stage.key} className="workflow-strip-card">
                      <div className="workflow-strip-row">
                        <strong style={{ fontSize: '13px' }}>{stage.label}</strong>
                        <StatusChip status={stage.status} />
                      </div>
                      <p>{truncateText(stage.recommended_action || 'No action yet.', 100)}</p>
                    </article>
                  ))}
                </div>
              </>
            ) : null}
          </section>

          {children}
        </div>
      </section>
    </WorkspaceShellContext.Provider>
  )
}
