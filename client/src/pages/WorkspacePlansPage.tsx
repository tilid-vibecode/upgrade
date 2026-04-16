import { useCallback, useEffect, useMemo, useRef, useState } from 'react'

import { AppLink } from '../app/navigation'
import { useWorkspaceShell } from '../app/WorkspaceLayout'
import { CollapsibleHero } from '../shared/ui/CollapsibleHero'
import { getApiErrorMessage, getApiErrorMessages, isApiError } from '../shared/api'
import { formatDateTime, formatFileSize, formatShortId } from '../shared/formatters'
import {
  buildArtifactKey,
  buildIndividualPlanPreview,
  canGeneratePlans,
  formatActionCountPreview,
  getPlanGenerationBlockers,
  getPlanRunCurrentTitle,
  getPlanRunEmployeeName,
  getPlanSummaryStatus,
  hasPlanSummary,
  isSamePlanLineage,
  normalizePlanViewMode,
  readRecord,
  readRecordArray,
  readString,
  readStringArray,
  sumActionCounts,
  type PlanViewMode,
} from '../shared/planPresentation'
import {
  generatePrototypeDevelopmentPlans,
  getPrototypeCurrentIndividualDownloads,
  getPrototypeCurrentIndividualPlan,
  getPrototypeCurrentPlanSummary,
  getPrototypeCurrentTeamActions,
  getPrototypeCurrentTeamDownloads,
  getPrototypeCurrentTeamPlan,
  getPrototypeLatestIndividualDownloads,
  getPrototypeLatestIndividualPlan,
  getPrototypeLatestPlanSummary,
  getPrototypeLatestTeamActions,
  getPrototypeLatestTeamDownloads,
  getPrototypeLatestTeamPlan,
  listPrototypeCurrentIndividualPlans,
  listPrototypeLatestIndividualPlans,
  listPrototypeLatestWorkspacePlanArtifacts,
  type PrototypeDevelopmentPlanArtifactBundleResponse,
  type PrototypeDevelopmentPlanArtifactListResponse,
  type PrototypeDevelopmentPlanRun,
  type PrototypeDevelopmentPlanSliceResponse,
  type PrototypeDevelopmentPlanSummaryResponse,
} from '../shared/prototypeApi'
import { humanizeToken } from '../shared/workflow'
import EmptyState from '../shared/ui/EmptyState'
import ErrorState from '../shared/ui/ErrorState'
import LoadingState from '../shared/ui/LoadingState'
import StatusChip from '../shared/ui/StatusChip'

type BannerTone = 'info' | 'success' | 'warn' | 'error'

type BannerState = {
  tone: BannerTone
  title: string
  messages: string[]
}

type LoadPlansPageOptions = {
  silent?: boolean
  failOnError?: boolean
}

function buildEmptyPlanSummary(workspaceSlug: string): PrototypeDevelopmentPlanSummaryResponse {
  return {
    workspace_slug: workspaceSlug,
    blueprint_run_uuid: null,
    matrix_run_uuid: null,
    planning_context_uuid: null,
    generation_batch_uuid: null,
    team_plan_uuid: null,
    team_plan_status: '',
    batch_status: '',
    is_current: false,
    individual_plan_count: 0,
    employee_count_in_scope: 0,
    completed_individual_plan_count: 0,
    failed_individual_plan_count: 0,
    missing_individual_plan_count: 0,
    action_counts: {},
    updated_at: null,
  }
}

type LoadPlansPageResult = {
  currentSummary: PrototypeDevelopmentPlanSummaryResponse
  latestSummary: PrototypeDevelopmentPlanSummaryResponse
}

function requestOptional<T>(request: Promise<T>) {
  return request.catch((error: unknown) => {
    if (isApiError(error) && error.status === 404) {
      return null
    }

    throw error
  })
}

function normalizeOptionalString(value: string) {
  const normalized = value.trim()
  return normalized || undefined
}

function openDownloadUrl(url: string) {
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.rel = 'noopener noreferrer'
  anchor.download = ''
  anchor.style.display = 'none'
  document.body.appendChild(anchor)
  anchor.click()
  anchor.remove()
}

function renderBanner(banner: BannerState | null) {
  if (banner === null) {
    return null
  }

  const toneClass =
    banner.tone === 'success'
      ? 'inline-banner-success'
      : banner.tone === 'warn'
        ? 'inline-banner-warn'
        : banner.tone === 'error'
          ? 'inline-banner-error'
          : 'inline-banner-info'

  return (
    <section className={`inline-banner ${toneClass}`}>
      <strong>{banner.title}</strong>
      {banner.messages.length > 0 ? (
        <ul className="inline-detail-list">
          {banner.messages.map((message) => (
            <li key={message}>{message}</li>
          ))}
        </ul>
      ) : null}
    </section>
  )
}

function formatListPreview(items: string[], fallback: string) {
  if (items.length === 0) {
    return fallback
  }

  return items.join(', ')
}

function getSummaryStatusDescription(summary: PrototypeDevelopmentPlanSummaryResponse | null) {
  if (!summary || !summary.team_plan_uuid) {
    return 'No plan batch is available yet.'
  }

  return [
    `${summary.completed_individual_plan_count} completed PDP(s)`,
    `${summary.failed_individual_plan_count} failed`,
    `${summary.missing_individual_plan_count} missing`,
  ].join(' · ')
}

function renderArtifactBundleCard(
  label: string,
  bundle: PrototypeDevelopmentPlanArtifactBundleResponse | null,
  onDownload: (url: string) => void,
) {
  if (bundle === null) {
    return null
  }

  return (
    <article className="review-card">
      <div className="review-card-head">
        <div>
          <span className="summary-label">{label}</span>
          <h4>{bundle.title}</h4>
        </div>
        <StatusChip status={bundle.status} />
      </div>

      <div className="detail-stack">
        <p>
          Plan {formatShortId(bundle.plan_uuid)} · batch {formatShortId(bundle.generation_batch_uuid)}
        </p>
        <p>
          {bundle.selected_as_current
            ? 'This bundle was selected through the current-plan selector.'
            : 'This bundle reflects the latest completed plan selector.'}
        </p>
      </div>

      {bundle.artifacts.length > 0 ? (
        <div className="artifact-card-grid">
          {bundle.artifacts.map((artifact) => (
            <article key={buildArtifactKey(artifact)} className="artifact-card">
              <div className="artifact-card-head">
                <div>
                  <span className="summary-label">{humanizeToken(artifact.artifact_format)}</span>
                  <strong>{artifact.original_filename}</strong>
                </div>
                <StatusChip status={artifact.is_current ? 'completed' : 'ready'} />
              </div>
              <p>
                {formatFileSize(artifact.file_size)} · {artifact.content_type || 'Unknown content type'}
              </p>
              <div className="form-actions">
                <button
                  className="secondary-button"
                  onClick={() => artifact.signed_url && onDownload(artifact.signed_url)}
                  disabled={!artifact.signed_url}
                >
                  Download
                </button>
              </div>
            </article>
          ))}
        </div>
      ) : (
        <p>No downloadable artifacts were returned for this bundle.</p>
      )}
    </article>
  )
}

export default function WorkspacePlansPage() {
  const {
    workspace,
    workflow,
    activePlanningContext,
    planningContextOptions,
    refreshShell,
    buildScopedWorkspacePath,
  } = useWorkspaceShell()
  const plansStage = workflow.stages.find((stage) => stage.key === 'plans') ?? null
  const isArchivedContext = activePlanningContext?.status === 'archived'
  const [currentSummary, setCurrentSummary] = useState<PrototypeDevelopmentPlanSummaryResponse | null>(null)
  const [latestSummary, setLatestSummary] = useState<PrototypeDevelopmentPlanSummaryResponse | null>(null)
  const [currentTeamPlan, setCurrentTeamPlan] = useState<PrototypeDevelopmentPlanRun | null>(null)
  const [latestTeamPlan, setLatestTeamPlan] = useState<PrototypeDevelopmentPlanRun | null>(null)
  const [currentTeamActions, setCurrentTeamActions] = useState<PrototypeDevelopmentPlanSliceResponse | null>(null)
  const [latestTeamActions, setLatestTeamActions] = useState<PrototypeDevelopmentPlanSliceResponse | null>(null)
  const [currentIndividuals, setCurrentIndividuals] = useState<PrototypeDevelopmentPlanRun[]>([])
  const [latestIndividuals, setLatestIndividuals] = useState<PrototypeDevelopmentPlanRun[]>([])
  const [currentTeamBundle, setCurrentTeamBundle] = useState<PrototypeDevelopmentPlanArtifactBundleResponse | null>(null)
  const [latestTeamBundle, setLatestTeamBundle] = useState<PrototypeDevelopmentPlanArtifactBundleResponse | null>(null)
  const [latestArtifactList, setLatestArtifactList] = useState<PrototypeDevelopmentPlanArtifactListResponse | null>(null)
  const [selectedMode, setSelectedMode] = useState<PlanViewMode>('current')
  const [selectedEmployeeUuid, setSelectedEmployeeUuid] = useState<string | null>(null)
  const [currentEmployeePlan, setCurrentEmployeePlan] = useState<PrototypeDevelopmentPlanRun | null>(null)
  const [latestEmployeePlan, setLatestEmployeePlan] = useState<PrototypeDevelopmentPlanRun | null>(null)
  const [currentEmployeeBundle, setCurrentEmployeeBundle] =
    useState<PrototypeDevelopmentPlanArtifactBundleResponse | null>(null)
  const [latestEmployeeBundle, setLatestEmployeeBundle] =
    useState<PrototypeDevelopmentPlanArtifactBundleResponse | null>(null)
  const [employeePanelLoading, setEmployeePanelLoading] = useState(false)
  const [employeePanelError, setEmployeePanelError] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)
  const [loadError, setLoadError] = useState<string | null>(null)
  const [banner, setBanner] = useState<BannerState | null>(null)
  const [busyAction, setBusyAction] = useState<string | null>(null)
  const [teamTitleDraft, setTeamTitleDraft] = useState('')
  const loadRequestIdRef = useRef(0)

  const hasPublishedBlueprint = Boolean(workflow.summary.current_published_blueprint_run_uuid)
  const generationBlockers = getPlanGenerationBlockers(
    plansStage?.blockers || [],
    hasPublishedBlueprint,
    workflow.summary.latest_matrix_run_uuid,
  )
  const generateAllowed = canGeneratePlans(
    plansStage?.status || '',
    hasPublishedBlueprint,
    workflow.summary.latest_matrix_run_uuid,
  )

  const loadPlansPage = useCallback(
    async ({ silent = false, failOnError = false }: LoadPlansPageOptions = {}): Promise<LoadPlansPageResult> => {
      const requestId = loadRequestIdRef.current + 1
      loadRequestIdRef.current = requestId

      if (!silent) {
        setLoading(true)
      }
      setLoadError(null)

      try {
        const [currentSummaryResponse, latestSummaryResponse, latestArtifactListResponse] = await Promise.all([
          getPrototypeCurrentPlanSummary(workspace.slug, planningContextOptions),
          getPrototypeLatestPlanSummary(workspace.slug, planningContextOptions),
          requestOptional(listPrototypeLatestWorkspacePlanArtifacts(workspace.slug, planningContextOptions)),
        ])

        const shouldLoadCurrent = Boolean(currentSummaryResponse.team_plan_uuid)
        const shouldLoadLatest = Boolean(latestSummaryResponse.team_plan_uuid)

        const [
          currentTeamPlanResponse,
          latestTeamPlanResponse,
          currentTeamActionsResponse,
          latestTeamActionsResponse,
          currentIndividualsResponse,
          latestIndividualsResponse,
          currentTeamBundleResponse,
          latestTeamBundleResponse,
        ] = await Promise.all([
          shouldLoadCurrent ? requestOptional(getPrototypeCurrentTeamPlan(workspace.slug, planningContextOptions)) : Promise.resolve(null),
          shouldLoadLatest ? requestOptional(getPrototypeLatestTeamPlan(workspace.slug, planningContextOptions)) : Promise.resolve(null),
          shouldLoadCurrent ? requestOptional(getPrototypeCurrentTeamActions(workspace.slug, planningContextOptions)) : Promise.resolve(null),
          shouldLoadLatest ? requestOptional(getPrototypeLatestTeamActions(workspace.slug, planningContextOptions)) : Promise.resolve(null),
          shouldLoadCurrent ? listPrototypeCurrentIndividualPlans(workspace.slug, planningContextOptions) : Promise.resolve([]),
          shouldLoadLatest ? listPrototypeLatestIndividualPlans(workspace.slug, planningContextOptions) : Promise.resolve([]),
          shouldLoadCurrent ? requestOptional(getPrototypeCurrentTeamDownloads(workspace.slug, planningContextOptions)) : Promise.resolve(null),
          shouldLoadLatest ? requestOptional(getPrototypeLatestTeamDownloads(workspace.slug, planningContextOptions)) : Promise.resolve(null),
        ])

        if (loadRequestIdRef.current !== requestId) {
          return {
            currentSummary: currentSummaryResponse,
            latestSummary: latestSummaryResponse,
          }
        }

        setCurrentSummary(currentSummaryResponse)
        setLatestSummary(latestSummaryResponse)
        setCurrentTeamPlan(currentTeamPlanResponse)
        setLatestTeamPlan(latestTeamPlanResponse)
        setCurrentTeamActions(currentTeamActionsResponse)
        setLatestTeamActions(latestTeamActionsResponse)
        setCurrentIndividuals(currentIndividualsResponse)
        setLatestIndividuals(latestIndividualsResponse)
        setCurrentTeamBundle(currentTeamBundleResponse)
        setLatestTeamBundle(latestTeamBundleResponse)
        setLatestArtifactList(
          latestArtifactListResponse || {
            workspace_slug: workspace.slug,
            artifacts: [],
            total: 0,
          },
        )
        setSelectedMode((value) => normalizePlanViewMode(value, currentSummaryResponse, latestSummaryResponse))
        return {
          currentSummary: currentSummaryResponse,
          latestSummary: latestSummaryResponse,
        }
      } catch (requestError) {
        if (loadRequestIdRef.current === requestId) {
          setLoadError(getApiErrorMessage(requestError, 'Failed to load development plan data.'))
        }

        if (failOnError) {
          throw requestError
        }

        return {
          currentSummary: buildEmptyPlanSummary(workspace.slug),
          latestSummary: buildEmptyPlanSummary(workspace.slug),
        }
      } finally {
        if (!silent && loadRequestIdRef.current === requestId) {
          setLoading(false)
        }
      }
    },
    [planningContextOptions, workspace.slug],
  )

  useEffect(() => {
    void loadPlansPage()
  }, [loadPlansPage])

  const activeIndividuals = useMemo(
    () => (selectedMode === 'current' ? currentIndividuals : latestIndividuals),
    [currentIndividuals, latestIndividuals, selectedMode],
  )

  useEffect(() => {
    if (activeIndividuals.length === 0) {
      setSelectedEmployeeUuid(null)
      return
    }

    if (selectedEmployeeUuid && activeIndividuals.some((run) => run.employee_uuid === selectedEmployeeUuid)) {
      return
    }

    setSelectedEmployeeUuid(activeIndividuals[0]?.employee_uuid || null)
  }, [activeIndividuals, selectedEmployeeUuid])

  useEffect(() => {
    if (!selectedEmployeeUuid) {
      setCurrentEmployeePlan(null)
      setLatestEmployeePlan(null)
      setCurrentEmployeeBundle(null)
      setLatestEmployeeBundle(null)
      setEmployeePanelError(null)
      return
    }

    let cancelled = false

    async function loadEmployeePanels() {
      const employeeUuid = selectedEmployeeUuid
      if (!employeeUuid) {
        return
      }

      setCurrentEmployeePlan(null)
      setLatestEmployeePlan(null)
      setCurrentEmployeeBundle(null)
      setLatestEmployeeBundle(null)
      setEmployeePanelLoading(true)
      setEmployeePanelError(null)

      try {
        const [
          currentPlanResponse,
          latestPlanResponse,
          currentBundleResponse,
          latestBundleResponse,
        ] = await Promise.all([
          requestOptional(getPrototypeCurrentIndividualPlan(workspace.slug, employeeUuid, planningContextOptions)),
          requestOptional(getPrototypeLatestIndividualPlan(workspace.slug, employeeUuid, planningContextOptions)),
          requestOptional(getPrototypeCurrentIndividualDownloads(workspace.slug, employeeUuid, planningContextOptions)),
          requestOptional(getPrototypeLatestIndividualDownloads(workspace.slug, employeeUuid, planningContextOptions)),
        ])

        if (cancelled) {
          return
        }

        setCurrentEmployeePlan(currentPlanResponse)
        setLatestEmployeePlan(latestPlanResponse)
        setCurrentEmployeeBundle(currentBundleResponse)
        setLatestEmployeeBundle(latestBundleResponse)
      } catch (requestError) {
        if (!cancelled) {
          setEmployeePanelError(getApiErrorMessage(requestError, 'Failed to load the selected employee plan.'))
        }
      } finally {
        if (!cancelled) {
          setEmployeePanelLoading(false)
        }
      }
    }

    void loadEmployeePanels()

    return () => {
      cancelled = true
    }
  }, [planningContextOptions, selectedEmployeeUuid, workspace.slug])

  const sameTeamLineage = isSamePlanLineage(currentSummary, latestSummary)
  const hasCurrentPlans = hasPlanSummary(currentSummary)
  const hasLatestPlans = hasPlanSummary(latestSummary)
  const showModeToggle = hasCurrentPlans && hasLatestPlans && !sameTeamLineage

  const activeSummary = selectedMode === 'current' ? currentSummary : latestSummary
  const activeTeamPlan = selectedMode === 'current' ? currentTeamPlan : latestTeamPlan
  const activeTeamActions = selectedMode === 'current' ? currentTeamActions : latestTeamActions
  const selectedPlanFromList =
    activeIndividuals.find((run) => run.employee_uuid === selectedEmployeeUuid) ?? null
  const activeEmployeePlan =
    (selectedMode === 'current' ? currentEmployeePlan : latestEmployeePlan) ?? selectedPlanFromList
  const activeSummaryStatus = getPlanSummaryStatus(activeSummary)
  const latestSummaryStatus = getPlanSummaryStatus(latestSummary)
  const currentSummaryStatus = getPlanSummaryStatus(currentSummary)
  const activeTeamPlanPayload = readRecord(activeTeamPlan?.plan_payload)
  const activeTeamActionPayload = readRecord(activeTeamActions?.payload)
  const teamPriorityActions = readRecordArray(activeTeamPlanPayload.priority_actions)
  const teamActionSliceItems = readRecordArray(activeTeamActionPayload.priority_actions)
  const activeTeamActionCounts = activeTeamActions
    ? readRecord(activeTeamActions.payload).action_counts
    : activeSummary?.action_counts || {}
  const activeEmployeePlanPayload = readRecord(activeEmployeePlan?.plan_payload)
  const activeEmployeeDevelopmentActions = readRecordArray(activeEmployeePlanPayload.development_actions)
  const activeEmployeeRoleFit = readString(activeEmployeePlanPayload.current_role_fit)
  const activeEmployeeMobilityNote = readString(activeEmployeePlanPayload.mobility_note)
  const activeEmployeeRoadmapAlignment = readString(activeEmployeePlanPayload.roadmap_alignment)
  const activeEmployeeAspiration = readRecord(activeEmployeePlanPayload.aspiration)
  const activeEmployeeAspirationFamily = humanizeToken(
    readString(activeEmployeeAspiration.target_role_family) || 'not_captured',
  )
  const activeEmployeeAspirationNote = readString(activeEmployeeAspiration.notes)
  const activeEmployeePriorityGaps = readStringArray(activeEmployeePlanPayload.priority_gaps)
  const activeEmployeeAdjacentRoles = readStringArray(activeEmployeePlanPayload.adjacent_roles)
  const activeEmployeeStrengths = readStringArray(activeEmployeePlanPayload.strengths)
  const currentTeamSummaryDescription = getSummaryStatusDescription(currentSummary)
  const latestTeamSummaryDescription = getSummaryStatusDescription(latestSummary)

  async function handleGeneratePlans() {
    setBusyAction('generate')
    setBanner(null)

    try {
      await generatePrototypeDevelopmentPlans(
        workspace.slug,
        {
          team_title: normalizeOptionalString(teamTitleDraft),
        },
        planningContextOptions,
      )

      const [pageReload, shellReload] = await Promise.allSettled([
        loadPlansPage({ silent: true, failOnError: true }),
        refreshShell(),
      ])

      if (pageReload.status === 'rejected') {
        throw pageReload.reason
      }

      const nextLatestSummary = pageReload.value.latestSummary
      const shellRefreshFailed = shellReload.status === 'rejected'

      if (getPlanSummaryStatus(nextLatestSummary) === 'partial_failed') {
        setBanner({
          tone: 'warn',
          title: 'Latest plan batch is only partially complete.',
          messages: [
            `${nextLatestSummary.failed_individual_plan_count} PDP(s) failed and ${nextLatestSummary.missing_individual_plan_count} remain missing.`,
            shellRefreshFailed ? 'The workspace shell did not refresh cleanly, but the plans page shows the latest available data.' : 'Current and latest selectors remain available for inspection.',
          ],
        })
      } else {
        setBanner({
          tone: 'success',
          title: 'Development plans generated.',
          messages: [
            `${nextLatestSummary.completed_individual_plan_count} PDP(s) completed in the latest batch.`,
            shellRefreshFailed ? 'The workspace shell did not refresh cleanly, but the plans page shows the latest available data.' : 'The page now reflects the canonical current/latest plan state.',
          ],
        })
      }

      setTeamTitleDraft('')
    } catch (requestError) {
      setBanner({
        tone: 'error',
        title: 'Development plan generation failed.',
        messages: getApiErrorMessages(requestError),
      })
    } finally {
      setBusyAction(null)
    }
  }

  async function handleRefreshAll() {
    await Promise.allSettled([loadPlansPage(), refreshShell()])
  }

  if (loading && currentSummary === null && latestSummary === null) {
    return (
      <LoadingState
        title="Loading development plans"
        description="Fetching current and latest plan batches, readable outputs, and download bundles."
      />
    )
  }

  if (loadError && currentSummary === null && latestSummary === null) {
    return (
      <ErrorState
        title="Development plans failed to load"
        description={loadError}
        onRetry={() => void handleRefreshAll()}
      />
    )
  }

  return (
    <div className="page-stack plans-page-stack">
      <CollapsibleHero
        tag="Stage 09"
        title="Development plans, outputs, and downloads"
        className="plans-hero"
        statusSlot={<StatusChip status={currentSummaryStatus} />}
      >
        <div className="hero-copy">
          <p>
            Generate the final plan batch, inspect the readable team plan and PDPs, and download the current or latest
            exported outputs without leaving the workspace shell.
          </p>
          {plansStage?.recommended_action ? <p>{plansStage.recommended_action}</p> : null}
        </div>

        <div className="plans-hero-rail">
          <div className="route-badge">
            <span className="summary-label">Current scope</span>
            <strong>{activePlanningContext ? activePlanningContext.name : 'Legacy workspace'}</strong>
            <StatusChip status={activePlanningContext?.status || 'ready'} />
          </div>
          <div className="route-badge">
            <span className="summary-label">Published blueprint</span>
            <strong>
              {hasPublishedBlueprint
                ? formatShortId(workflow.summary.current_published_blueprint_run_uuid)
                : 'Required first'}
            </strong>
            <StatusChip status={hasPublishedBlueprint ? 'published' : 'blocked'} />
          </div>
          <div className="route-badge">
            <span className="summary-label">Latest matrix reference</span>
            <strong>
              {workflow.summary.latest_matrix_run_uuid
                ? formatShortId(workflow.summary.latest_matrix_run_uuid)
                : 'Required first'}
            </strong>
            <StatusChip status={workflow.summary.latest_matrix_status || 'blocked'} />
            <p>{humanizeToken(workflow.summary.latest_matrix_status || 'not_started')}</p>
          </div>
          <div className="route-badge">
            <span className="summary-label">Latest batch</span>
            <strong>{formatShortId(latestSummary?.team_plan_uuid)}</strong>
            <StatusChip status={latestSummaryStatus} />
          </div>
          <div className="route-badge">
            <span className="summary-label">Current batch</span>
            <strong>{formatShortId(currentSummary?.team_plan_uuid)}</strong>
            <StatusChip status={currentSummaryStatus} />
          </div>
        </div>
      </CollapsibleHero>

      {renderBanner(banner)}

      {activePlanningContext ? (
        <section className="inline-banner inline-banner-info">
          <strong>Viewing development plans for {activePlanningContext.name}.</strong>
          <p>
            Current and latest plan selectors, downloads, and matrix lineage on this page are scoped to the selected
            planning context.
          </p>
        </section>
      ) : null}

      {isArchivedContext ? (
        <section className="inline-banner inline-banner-warn">
          <strong>{activePlanningContext?.name || 'This planning context'} is archived and read-only.</strong>
          <p>Existing plan runs remain visible, but plan generation is disabled for archived contexts.</p>
        </section>
      ) : null}

      {loadError && (currentSummary !== null || latestSummary !== null) ? (
        <ErrorState
          title="Latest plan refresh failed"
          description={`${loadError} Showing the most recent successful plan data.`}
          onRetry={() => void handleRefreshAll()}
          compact
        />
      ) : null}

      {!hasPublishedBlueprint || !workflow.summary.latest_matrix_run_uuid ? (
        <section className="inline-banner inline-banner-warn">
          <strong>Complete the blueprint and matrix stages before generating plans.</strong>
          <div className="form-actions">
            <AppLink className="primary-button link-button" to={buildScopedWorkspacePath('matrix')}>
              Open matrix review
            </AppLink>
          </div>
        </section>
      ) : null}

      {sameTeamLineage || !hasCurrentPlans || !hasLatestPlans ? null : (
        <section className="inline-banner inline-banner-warn">
          <strong>Latest and current plan batches are different.</strong>
          <p>
            The newest completed batch is not the same as the currently selected stable batch. Review both before
            sharing sponsor-ready outputs.
          </p>
        </section>
      )}

      {latestSummaryStatus === 'partial_failed' ? (
        <section className="inline-banner inline-banner-warn">
          <strong>The latest batch is partially complete.</strong>
          <p>
            {latestSummary?.failed_individual_plan_count || 0} PDP(s) failed and{' '}
            {latestSummary?.missing_individual_plan_count || 0} remain missing in the newest batch.
          </p>
        </section>
      ) : null}

      <section className="board-panel">
        <div className="panel-heading">
          <span className="section-tag">Generation</span>
          <h3>Generate the latest team and individual plan batch</h3>
          <p>
            Plan generation uses the current published blueprint and completed matrix lineage. After generation, the
            page re-reads canonical current and latest selectors instead of trusting optimistic mutation state.
          </p>
        </div>

        <div className="plans-control-grid">
          <label className="field-label">
            <span>Team plan title</span>
            <input
              className="text-input"
              value={teamTitleDraft}
              onChange={(event) => setTeamTitleDraft(event.target.value)}
              placeholder={latestTeamPlan?.title || currentTeamPlan?.title || 'Final development plan'}
              disabled={busyAction !== null || isArchivedContext}
            />
          </label>

          <div className="plans-control-actions">
            <button
              className="primary-button"
              onClick={() => void handleGeneratePlans()}
              disabled={!generateAllowed || busyAction !== null || isArchivedContext}
            >
              {busyAction === 'generate' ? 'Generating plans...' : hasLatestPlans ? 'Generate new batch' : 'Generate first batch'}
            </button>
            <button className="secondary-button" onClick={() => void handleRefreshAll()} disabled={busyAction !== null}>
              Refresh data
            </button>
            <AppLink className="secondary-button link-button" to={buildScopedWorkspacePath('matrix')}>
              Open matrix review
            </AppLink>
          </div>
        </div>

        {isArchivedContext ? (
          <p className="detail-note">
            Archived contexts keep current and latest selectors readable, but new plan batches cannot be generated.
          </p>
        ) : null}

        {generationBlockers.length > 0 ? (
          <div className="blocker-stack">
            {generationBlockers.map((blocker) => (
              <article key={blocker} className="blocker-item">
                <p>{blocker}</p>
              </article>
            ))}
          </div>
        ) : null}
      </section>

      <section className="summary-grid">
        {sameTeamLineage ? (
          <>
            <article className="summary-card">
              <span className="summary-label">Plan batch summary</span>
              <div className="status-row">
                <strong>{formatShortId(currentSummary?.team_plan_uuid || latestSummary?.team_plan_uuid)}</strong>
                <StatusChip status={currentSummaryStatus || latestSummaryStatus} />
              </div>
              <p>{currentTeamSummaryDescription || latestTeamSummaryDescription}</p>
            </article>

            <article className="summary-card">
              <span className="summary-label">Action mix</span>
              <strong>{sumActionCounts(currentSummary?.action_counts || latestSummary?.action_counts || {})} action(s)</strong>
              <p>
                {formatListPreview(
                  formatActionCountPreview(currentSummary?.action_counts || latestSummary?.action_counts || {}),
                  'No action counts were returned.',
                )}
              </p>
            </article>
          </>
        ) : (
          <>
            <article className="summary-card">
              <span className="summary-label">Latest batch summary</span>
              <div className="status-row">
                <strong>{formatShortId(latestSummary?.team_plan_uuid)}</strong>
                <StatusChip status={latestSummaryStatus} />
              </div>
              <p>{latestTeamSummaryDescription}</p>
            </article>

            <article className="summary-card">
              <span className="summary-label">Current batch summary</span>
              <div className="status-row">
                <strong>{formatShortId(currentSummary?.team_plan_uuid)}</strong>
                <StatusChip status={currentSummaryStatus} />
              </div>
              <p>{currentTeamSummaryDescription}</p>
            </article>

            <article className="summary-card">
              <span className="summary-label">Latest action mix</span>
              <strong>{sumActionCounts(latestSummary?.action_counts || {})} action(s)</strong>
              <p>
                {formatListPreview(
                  formatActionCountPreview(latestSummary?.action_counts || {}),
                  'No action counts were returned.',
                )}
              </p>
            </article>

            <article className="summary-card">
              <span className="summary-label">Current action mix</span>
              <strong>{sumActionCounts(currentSummary?.action_counts || {})} action(s)</strong>
              <p>
                {formatListPreview(
                  formatActionCountPreview(currentSummary?.action_counts || {}),
                  'No action counts were returned.',
                )}
              </p>
            </article>
          </>
        )}
      </section>

      {showModeToggle ? (
        <section className="board-panel">
          <div className="panel-heading">
            <span className="section-tag">Lineage mode</span>
            <h3>Switch between current and latest plan selectors</h3>
            <p>
              Use the current selector for stable sponsor-ready outputs and the latest selector to inspect the newest
              completed batch.
            </p>
          </div>

          <div className="segment-toggle-row">
            <button
              className={selectedMode === 'current' ? 'segment-toggle-button is-active' : 'segment-toggle-button'}
              onClick={() => setSelectedMode('current')}
            >
              Current batch
            </button>
            <button
              className={selectedMode === 'latest' ? 'segment-toggle-button is-active' : 'segment-toggle-button'}
              onClick={() => setSelectedMode('latest')}
            >
              Latest batch
            </button>
          </div>
        </section>
      ) : null}

      {activeSummary && !activeSummary.team_plan_uuid ? (
        <section className="board-panel">
          <EmptyState
            title="No development plan batch is available yet"
            description={
              generateAllowed
                ? 'The workspace is ready for the first plan generation. Once completed, readable team and individual outputs will appear here.'
                : 'Development plan generation is still blocked. Resolve the upstream blockers above, then return here.'
            }
            action={
              generateAllowed && !isArchivedContext ? (
                <button className="primary-button" onClick={() => void handleGeneratePlans()} disabled={busyAction !== null}>
                  Generate first batch
                </button>
              ) : (
                <AppLink className="primary-button link-button" to={buildScopedWorkspacePath('matrix')}>
                  Return to matrix review
                </AppLink>
              )
            }
          />
        </section>
      ) : (
        <>
          <section className="board-panel">
            <div className="panel-heading">
              <span className="section-tag">Team plan</span>
              <h3>Readable team plan presentation</h3>
              <p>Review the current plan narrative in-app before sending anyone to exports or downloads.</p>
            </div>

            {activeTeamPlan ? (
              <div className="detail-stack">
                <div className="summary-grid">
                  <article className="summary-card">
                    <span className="summary-label">Plan UUID</span>
                    <div className="status-row">
                      <strong>{formatShortId(activeTeamPlan.uuid)}</strong>
                      <StatusChip status={activeSummaryStatus} />
                    </div>
                    <p>{selectedMode === 'current' ? 'Viewing the current selected batch.' : 'Viewing the latest completed batch.'}</p>
                  </article>

                  <article className="summary-card">
                    <span className="summary-label">Lineage</span>
                    <strong>
                      Blueprint {formatShortId(activeTeamPlan.blueprint_run_uuid)} · Matrix {formatShortId(activeTeamPlan.matrix_run_uuid)}
                    </strong>
                    <p>Batch {formatShortId(activeTeamPlan.generation_batch_uuid)}</p>
                  </article>

                  <article className="summary-card">
                    <span className="summary-label">Updated</span>
                    <strong>{formatDateTime(activeTeamPlan.updated_at)}</strong>
                    <p>{activeSummary?.employee_count_in_scope || 0} employee(s) in scope.</p>
                  </article>

                  <article className="summary-card">
                    <span className="summary-label">Action totals</span>
                    <strong>{sumActionCounts(readRecord(activeTeamPlan.plan_payload).action_counts as Record<string, number>) || sumActionCounts(activeSummary?.action_counts || {})}</strong>
                    <p>{formatListPreview(formatActionCountPreview(activeSummary?.action_counts || {}), 'No action counts were returned.')}</p>
                  </article>
                </div>

                <div className="info-grid">
                  <article className="info-card">
                    <h4>Executive summary</h4>
                    <p>{readString(activeTeamPlanPayload.executive_summary) || 'No executive summary was returned.'}</p>
                  </article>
                  <article className="info-card">
                    <h4>Roadmap priority note</h4>
                    <p>{readString(activeTeamPlanPayload.roadmap_priority_note) || 'No roadmap-priority note was returned.'}</p>
                  </article>
                </div>

                <div className="review-card-grid">
                  <article className="review-card">
                    <div className="review-card-head">
                      <div>
                        <span className="summary-label">Hiring recommendations</span>
                        <h4>{readStringArray(activeTeamPlanPayload.hiring_recommendations).length} item(s)</h4>
                      </div>
                      <StatusChip status={readStringArray(activeTeamPlanPayload.hiring_recommendations).length > 0 ? 'action_required' : 'completed'} />
                    </div>
                    {readStringArray(activeTeamPlanPayload.hiring_recommendations).length > 0 ? (
                      <ul className="helper-list helper-list-compact">
                        {readStringArray(activeTeamPlanPayload.hiring_recommendations).map((item) => (
                          <li key={item}>{item}</li>
                        ))}
                      </ul>
                    ) : (
                      <p>No hiring recommendations were returned.</p>
                    )}
                  </article>

                  <article className="review-card">
                    <div className="review-card-head">
                      <div>
                        <span className="summary-label">Development focus</span>
                        <h4>{readStringArray(activeTeamPlanPayload.development_focus).length} item(s)</h4>
                      </div>
                      <StatusChip status={readStringArray(activeTeamPlanPayload.development_focus).length > 0 ? 'ready' : 'completed'} />
                    </div>
                    {readStringArray(activeTeamPlanPayload.development_focus).length > 0 ? (
                      <ul className="helper-list helper-list-compact">
                        {readStringArray(activeTeamPlanPayload.development_focus).map((item) => (
                          <li key={item}>{item}</li>
                        ))}
                      </ul>
                    ) : (
                      <p>No development-focus items were returned.</p>
                    )}
                  </article>

                  <article className="review-card">
                    <div className="review-card-head">
                      <div>
                        <span className="summary-label">Single points of failure</span>
                        <h4>{readStringArray(activeTeamPlanPayload.single_points_of_failure).length} risk(s)</h4>
                      </div>
                      <StatusChip status={readStringArray(activeTeamPlanPayload.single_points_of_failure).length > 0 ? 'action_required' : 'completed'} />
                    </div>
                    {readStringArray(activeTeamPlanPayload.single_points_of_failure).length > 0 ? (
                      <ul className="helper-list helper-list-compact">
                        {readStringArray(activeTeamPlanPayload.single_points_of_failure).map((item) => (
                          <li key={item}>{item}</li>
                        ))}
                      </ul>
                    ) : (
                      <p>No single-point-of-failure signals were returned.</p>
                    )}
                  </article>
                </div>
              </div>
            ) : (
              <EmptyState
                title="The selected plan batch has no readable team plan"
                description="Refresh the page or switch the lineage mode if the batch exists but its team-plan payload is unavailable."
              />
            )}
          </section>

          <section className="board-panel">
            <div className="panel-heading">
              <span className="section-tag">Team actions</span>
              <h3>Priority actions for sponsor scanning</h3>
              <p>
                This section keeps the highest-signal actions separate from the full narrative plan.{' '}
                {sumActionCounts(activeTeamActionCounts as Record<string, number>)} action(s) are visible for the
                selected lineage.
              </p>
            </div>

            {teamActionSliceItems.length > 0 ? (
              <div className="review-card-grid">
                {teamActionSliceItems.map((item, index) => (
                  <article key={`${readString(item.action_key) || index}`} className="review-card">
                    <div className="review-card-head">
                      <div>
                        <span className="summary-label">{humanizeToken(readString(item.action_type) || 'action')}</span>
                        <h4>{readString(item.action) || 'Action text not available'}</h4>
                      </div>
                      <StatusChip status="ready" />
                    </div>
                    <div className="detail-stack">
                      <p>{readString(item.why_now) || 'No timing rationale was returned.'}</p>
                      <p>
                        Owner role: <strong>{readString(item.owner_role) || 'Not captured'}</strong>
                      </p>
                      <p>
                        Time horizon: <strong>{readString(item.time_horizon) || 'Not captured'}</strong>
                      </p>
                    </div>
                  </article>
                ))}
              </div>
            ) : teamPriorityActions.length > 0 ? (
              <div className="review-card-grid">
                {teamPriorityActions.map((item, index) => (
                  <article key={`${readString(item.action_key) || index}`} className="review-card">
                    <div className="review-card-head">
                      <div>
                        <span className="summary-label">{humanizeToken(readString(item.action_type) || 'action')}</span>
                        <h4>{readString(item.action) || 'Action text not available'}</h4>
                      </div>
                      <StatusChip status="ready" />
                    </div>
                    <div className="detail-stack">
                      <p>{readString(item.why_now) || 'No timing rationale was returned.'}</p>
                      <p>
                        Owner role: <strong>{readString(item.owner_role) || 'Not captured'}</strong>
                      </p>
                    </div>
                  </article>
                ))}
              </div>
            ) : (
              <EmptyState
                title="No team actions are available yet"
                description="Generate a plan batch or switch lineage mode to inspect the action summary."
              />
            )}
          </section>

          <section className="board-panel">
            <div className="panel-heading">
              <span className="section-tag">Individual PDPs</span>
              <h3>Review per-employee development plans</h3>
              <p>Select an employee to inspect their PDP, priority gaps, and download bundle.</p>
            </div>

            {activeIndividuals.length === 0 ? (
              <EmptyState
                title="No individual PDPs are available for this selector"
                description="This lineage does not currently expose employee-level plan runs."
              />
            ) : (
              <div className="plans-layout-grid">
                <div className="plan-sidebar">
                  <div className="review-card-grid">
                    {activeIndividuals.map((run) => {
                      const preview = buildIndividualPlanPreview(run)
                      const isSelected = preview.employeeUuid === selectedEmployeeUuid

                      return (
                        <article
                          key={`${selectedMode}:${preview.employeeUuid}`}
                          className={isSelected ? 'review-card is-selected' : 'review-card'}
                        >
                          <div className="review-card-head">
                            <div>
                              <span className="summary-label">{preview.isCurrent ? 'Current PDP' : 'Latest PDP'}</span>
                              <h4>{preview.employeeName}</h4>
                            </div>
                            <StatusChip status={preview.status} />
                          </div>

                          <div className="detail-stack">
                            <p>{preview.currentTitle}</p>
                            <p>{preview.currentRoleFit}</p>
                            <p>
                              {preview.developmentActionCount} action(s) ·{' '}
                              {formatListPreview(preview.priorityGaps, 'No priority-gap preview available.')}
                            </p>
                          </div>

                          <div className="form-actions">
                            <button className="secondary-button" onClick={() => setSelectedEmployeeUuid(preview.employeeUuid)}>
                              Open PDP
                            </button>
                          </div>
                        </article>
                      )
                    })}
                  </div>
                </div>

                <div className="plan-detail-stack">
                  {employeePanelLoading && activeEmployeePlan === null ? (
                    <LoadingState
                      title="Loading selected PDP"
                      description="Fetching the selected employee’s readable plan and downloads."
                      compact
                    />
                  ) : employeePanelError && activeEmployeePlan === null ? (
                    <ErrorState
                      title="Selected PDP failed to load"
                      description={employeePanelError}
                      compact
                    />
                  ) : activeEmployeePlan ? (
                    <div className="detail-stack">
                      <div className="plan-overview-layout">
                        <article className="review-card plan-overview-card">
                          <div className="review-card-head">
                            <div>
                              <span className="summary-label">Selected PDP</span>
                              <h4>{getPlanRunEmployeeName(activeEmployeePlan)}</h4>
                            </div>
                            <StatusChip status={activeEmployeePlan.status} />
                          </div>
                          <p className="plan-overview-title">{getPlanRunCurrentTitle(activeEmployeePlan)}</p>
                          <div className="detail-stack">
                            <p>{activeEmployeeRoleFit || 'No role-fit summary was returned for this employee.'}</p>
                            <p>{activeEmployeeMobilityNote || 'No mobility note was returned.'}</p>
                          </div>
                        </article>

                        <div className="plan-overview-meta">
                          <article className="summary-card">
                            <span className="summary-label">Plan run</span>
                            <strong>{formatShortId(activeEmployeePlan.uuid)}</strong>
                            <p>This is the selected individual PDP revision.</p>
                          </article>

                          <article className="summary-card">
                            <span className="summary-label">Source lineage</span>
                            <strong>Batch {formatShortId(activeEmployeePlan.generation_batch_uuid)}</strong>
                            <div className="plan-lineage-list">
                              <span>Matrix {formatShortId(activeEmployeePlan.matrix_run_uuid)}</span>
                            </div>
                          </article>

                          <article className="summary-card">
                            <span className="summary-label">Development actions</span>
                            <strong>{activeEmployeeDevelopmentActions.length}</strong>
                            <p>{activeEmployeeRoadmapAlignment || 'No roadmap-alignment note was returned.'}</p>
                          </article>

                          <article className="summary-card">
                            <span className="summary-label">Aspirational direction</span>
                            <strong>{activeEmployeeAspirationFamily}</strong>
                            <p>{activeEmployeeAspirationNote || 'No aspiration note was returned.'}</p>
                          </article>
                        </div>
                      </div>

                      {employeePanelError ? (
                        <ErrorState
                          title="Selected PDP refresh failed"
                          description={employeePanelError}
                          compact
                        />
                      ) : null}

                      <div className="info-grid">
                        <article className="info-card">
                          <h4>Roadmap alignment</h4>
                          <p>{activeEmployeeRoadmapAlignment || 'No roadmap-alignment note was returned.'}</p>
                        </article>
                        <article className="info-card">
                          <h4>Adjacent roles</h4>
                          <p>{formatListPreview(activeEmployeeAdjacentRoles, 'No adjacent-role notes were returned.')}</p>
                        </article>
                        <article className="info-card">
                          <h4>Strengths</h4>
                          <p>{formatListPreview(activeEmployeeStrengths, 'No strengths were returned.')}</p>
                        </article>
                      </div>

                      <div className="review-card-grid">
                        <article className="review-card">
                          <div className="review-card-head">
                            <div>
                              <span className="summary-label">Priority gaps</span>
                              <h4>{activeEmployeePriorityGaps.length} item(s)</h4>
                            </div>
                            <StatusChip status={activeEmployeePriorityGaps.length > 0 ? 'action_required' : 'completed'} />
                          </div>
                          {activeEmployeePriorityGaps.length > 0 ? (
                            <ul className="helper-list helper-list-compact">
                              {activeEmployeePriorityGaps.map((item) => (
                                <li key={item}>{item}</li>
                              ))}
                            </ul>
                          ) : (
                            <p>No priority gaps were returned.</p>
                          )}
                        </article>

                        <article className="review-card">
                          <div className="review-card-head">
                            <div>
                              <span className="summary-label">Aspiration</span>
                              <h4>{activeEmployeeAspirationFamily}</h4>
                            </div>
                            <StatusChip status="ready" />
                          </div>
                          <p>{activeEmployeeAspirationNote || 'No aspiration note was returned.'}</p>
                        </article>
                      </div>

                      <div className="review-card-grid">
                        {activeEmployeeDevelopmentActions.length > 0 ? (
                          activeEmployeeDevelopmentActions.map((item, index) => (
                            <article key={`${readString(item.action_key) || index}`} className="review-card">
                              <div className="review-card-head">
                                <div>
                                  <span className="summary-label">{humanizeToken(readString(item.action_type) || 'action')}</span>
                                  <h4>{readString(item.action) || 'Development action not available'}</h4>
                                </div>
                                <StatusChip status="ready" />
                              </div>
                              <div className="detail-stack">
                                <p>{readString(item.expected_outcome) || 'No expected outcome was returned.'}</p>
                                <p>
                                  Time horizon: <strong>{readString(item.time_horizon) || 'Not captured'}</strong>
                                </p>
                                <p>{readString(item.coach_note) || 'No coach note was returned.'}</p>
                              </div>
                            </article>
                          ))
                        ) : (
                          <EmptyState
                            title="No development actions were returned"
                            description="The selected PDP does not currently expose development-action rows."
                          />
                        )}
                      </div>
                    </div>
                  ) : (
                    <EmptyState
                      title="Select an employee PDP"
                      description="Choose an employee from the list to inspect the readable PDP and its download bundle."
                    />
                  )}
                </div>
              </div>
            )}
          </section>

          <section className="board-panel">
            <div className="panel-heading">
              <span className="section-tag">Downloads</span>
              <h3>Current and latest output bundles</h3>
              <p>Download the team plan and the selected employee’s PDP in the formats already generated by the backend.</p>
            </div>

            <div className="artifact-bundle-stack">
              {renderArtifactBundleCard('Current team bundle', currentTeamBundle, openDownloadUrl)}
              {!sameTeamLineage ? renderArtifactBundleCard('Latest team bundle', latestTeamBundle, openDownloadUrl) : null}
              {selectedEmployeeUuid
                ? renderArtifactBundleCard('Current individual bundle', currentEmployeeBundle, openDownloadUrl)
                : null}
              {selectedEmployeeUuid && !sameTeamLineage
                ? renderArtifactBundleCard('Latest individual bundle', latestEmployeeBundle, openDownloadUrl)
                : null}
            </div>

            {latestArtifactList && latestArtifactList.total > 0 ? (
              <div className="detail-stack">
                <div className="panel-heading">
                  <span className="section-tag">Latest artifacts</span>
                  <h3>Latest generated artifact recovery panel</h3>
                  <p>This secondary list stays on the latest completed batch lineage for quick recovery or spot downloads.</p>
                </div>

                <div className="source-table-shell">
                  <table className="source-table">
                    <thead>
                      <tr>
                        <th>Title</th>
                        <th>Format</th>
                        <th>Scope</th>
                        <th>Batch</th>
                        <th>Size</th>
                        <th>Download</th>
                      </tr>
                    </thead>
                    <tbody>
                      {latestArtifactList.artifacts.map((artifact) => (
                        <tr key={buildArtifactKey(artifact)}>
                          <td>
                            <div className="source-primary-cell">
                              <strong>{artifact.original_filename}</strong>
                              <p>{artifact.title}</p>
                            </div>
                          </td>
                          <td>{humanizeToken(artifact.artifact_format)}</td>
                          <td>{humanizeToken(artifact.artifact_scope)}</td>
                          <td>{formatShortId(artifact.generation_batch_uuid)}</td>
                          <td>{formatFileSize(artifact.file_size)}</td>
                          <td>
                            <button
                              className="secondary-button source-action-button"
                              onClick={() => artifact.signed_url && openDownloadUrl(artifact.signed_url)}
                              disabled={!artifact.signed_url}
                            >
                              Download
                            </button>
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </div>
            ) : null}
          </section>
        </>
      )}
    </div>
  )
}
