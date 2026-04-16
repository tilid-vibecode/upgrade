import { useCallback, useEffect, useMemo, useRef, useState } from 'react'

import { AppLink } from '../app/navigation'
import { useWorkspaceShell } from '../app/WorkspaceLayout'
import {
  buildPublicAssessmentUrl,
  copyTextToClipboard,
  isAssessmentCycleActive,
} from '../shared/assessmentFlow'
import { CollapsibleHero } from '../shared/ui/CollapsibleHero'
import { getApiErrorMessage, getApiErrorMessages, isApiError } from '../shared/api'
import { formatDateTime, formatPercent, formatShortId } from '../shared/formatters'
import {
  generatePrototypeAssessmentCycle,
  getPrototypeAssessmentStatus,
  getPrototypeLatestAssessmentCycle,
  listPrototypeRoleMatches,
  listPrototypeLatestAssessmentPacks,
  regeneratePrototypeAssessmentCycle,
  type PrototypeAssessmentCycle,
  type PrototypeAssessmentPack,
  type PrototypeEmployeeRoleMatchListResponse,
  type PrototypeAssessmentStatusResponse,
} from '../shared/prototypeApi'
import { humanizeToken } from '../shared/workflow'
import { requestGlobalConfirmation } from '../shared/ui/ConfirmationDialog'
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

type AssessmentCohortEntry = {
  employeeUuid: string
  fullName: string
  bestFitRole: string
  fitScore: number
  relatedInitiatives: string[]
}

const DEFAULT_CONTEXT_ROLE_MATCH_THRESHOLD = 0.5

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

function openUrl(url: string) {
  const popup = window.open(url, '_blank', 'noopener,noreferrer')

  if (popup === null) {
    window.location.assign(url)
  }
}

function readString(value: unknown) {
  return typeof value === 'string' ? value : ''
}

function readNumber(value: unknown) {
  return typeof value === 'number' && Number.isFinite(value) ? value : null
}

function readRecord(value: unknown) {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : {}
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

function getPackRoleSummary(pack: PrototypeAssessmentPack) {
  const primaryRole = readRecord(readRecord(pack.selection_summary).primary_role)
  const roleName = readString(primaryRole.role_name).trim()
  if (roleName) {
    return roleName
  }

  return 'Primary role match not captured'
}

function getPackQuestionCount(pack: PrototypeAssessmentPack) {
  const targetedQuestions = pack.questionnaire_payload.targeted_questions || []
  if (targetedQuestions.length > 0) {
    return targetedQuestions.length
  }

  const summaryCount = readNumber(readRecord(pack.selection_summary).targeted_question_count)
  return summaryCount ?? 0
}

function getCurrentCycleSupportingCopy(
  currentCycle: PrototypeAssessmentCycle | null,
  status: PrototypeAssessmentStatusResponse | null,
) {
  if (currentCycle === null || status === null) {
    return 'No current usable cycle is available yet.'
  }

  return `${status.total_packs} pack(s), schema ${readString(currentCycle.configuration.schema_version) || 'stage7-v1'}, updated ${formatDateTime(currentCycle.updated_at)}.`
}

function getLatestAttemptSupportingCopy(status: PrototypeAssessmentStatusResponse | null) {
  if (status === null || !status.latest_attempt_uuid) {
    return 'No assessment generation attempt has been recorded yet.'
  }

  if (status.latest_attempt_uuid === status.current_cycle_uuid) {
    return 'The latest generation attempt is the same as the current usable cycle.'
  }

  if (status.latest_attempt_status === 'failed') {
    return 'The newest generation attempt failed. The current-cycle card shows whether an older usable cycle still exists.'
  }

  return 'The latest attempt differs from the currently usable cycle.'
}

function buildCohortEntries(roleMatches: PrototypeEmployeeRoleMatchListResponse | null) {
  if (!roleMatches) {
    return []
  }

  return roleMatches.employees
    .map((employee): AssessmentCohortEntry | null => {
      const bestMatch = employee.matches[0]
      if (!bestMatch) {
        return null
      }
      return {
        employeeUuid: employee.employee_uuid,
        fullName: employee.full_name,
        bestFitRole: bestMatch.role_name || 'Matched role',
        fitScore: bestMatch.fit_score,
        relatedInitiatives: bestMatch.related_initiatives || [],
      }
    })
    .filter((entry): entry is AssessmentCohortEntry => entry !== null)
    .sort((left, right) => {
      if (right.fitScore !== left.fitScore) {
        return right.fitScore - left.fitScore
      }
      return left.fullName.localeCompare(right.fullName)
    })
}

export default function WorkspaceAssessmentsPage() {
  const {
    workspace,
    workflow,
    activePlanningContext,
    planningContextOptions,
    refreshShell,
    buildScopedWorkspacePath,
  } = useWorkspaceShell()
  const assessmentStage = workflow.stages.find((stage) => stage.key === 'assessments') ?? null
  const [status, setStatus] = useState<PrototypeAssessmentStatusResponse | null>(null)
  const [currentCycle, setCurrentCycle] = useState<PrototypeAssessmentCycle | null>(null)
  const [packs, setPacks] = useState<PrototypeAssessmentPack[]>([])
  const [loading, setLoading] = useState(true)
  const [loadError, setLoadError] = useState<string | null>(null)
  const [banner, setBanner] = useState<BannerState | null>(null)
  const [busyAction, setBusyAction] = useState<string | null>(null)
  const [cycleTitleDraft, setCycleTitleDraft] = useState('')
  const [cohortEntries, setCohortEntries] = useState<AssessmentCohortEntry[]>([])
  const [selectedEmployeeUuids, setSelectedEmployeeUuids] = useState<string[]>([])
  const [cohortLoadError, setCohortLoadError] = useState<string | null>(null)
  const [cohortSelectionMode, setCohortSelectionMode] = useState<'auto' | 'manual'>('auto')
  const loadRequestIdRef = useRef(0)
  const cohortSelectionModeRef = useRef<'auto' | 'manual'>('auto')

  const hasPublishedBlueprint = Boolean(workflow.summary.current_published_blueprint_run_uuid)
  const isArchivedContext = activePlanningContext?.status === 'archived'

  useEffect(() => {
    setCohortSelectionMode('auto')
  }, [activePlanningContext?.uuid])

  useEffect(() => {
    cohortSelectionModeRef.current = cohortSelectionMode
  }, [cohortSelectionMode])

  const loadAssessmentPage = useCallback(
    async (silent = false) => {
      const requestId = loadRequestIdRef.current + 1
      loadRequestIdRef.current = requestId

      if (!silent) {
        setLoading(true)
        setLoadError(null)
      }

      try {
        const statusResponse = await getPrototypeAssessmentStatus(workspace.slug, planningContextOptions)
        if (loadRequestIdRef.current !== requestId) {
          return
        }

        let roleMatchesResponse: PrototypeEmployeeRoleMatchListResponse | null = null
        let nextCohortLoadError: string | null = null

        if (activePlanningContext) {
          try {
            roleMatchesResponse = await requestOptional(listPrototypeRoleMatches(workspace.slug, planningContextOptions))
          } catch (cohortError) {
            nextCohortLoadError = getApiErrorMessage(cohortError, 'The scoped employee cohort could not be loaded.')
          }
        }

        const [currentCycleResponse, packListResponse] = await Promise.all([
          statusResponse.current_cycle_uuid
            ? requestOptional(getPrototypeLatestAssessmentCycle(workspace.slug, planningContextOptions))
            : Promise.resolve(null),
          statusResponse.current_cycle_uuid
            ? requestOptional(listPrototypeLatestAssessmentPacks(workspace.slug, planningContextOptions))
            : Promise.resolve(null),
        ])

        if (loadRequestIdRef.current !== requestId) {
          return
        }

        setStatus(statusResponse)
        setCurrentCycle(currentCycleResponse)
        setPacks(packListResponse?.packs || [])
        if (activePlanningContext) {
          const nextCohortEntries = buildCohortEntries(roleMatchesResponse)
          setCohortEntries(nextCohortEntries)
          setSelectedEmployeeUuids((currentValue) => {
            const validIds = new Set(nextCohortEntries.map((entry) => entry.employeeUuid))
            const retained = currentValue.filter((employeeUuid) => validIds.has(employeeUuid))
            if (cohortSelectionModeRef.current === 'manual') {
              return retained
            }
            if (retained.length > 0) {
              return retained
            }
            return nextCohortEntries
              .filter((entry) => entry.fitScore >= DEFAULT_CONTEXT_ROLE_MATCH_THRESHOLD)
              .map((entry) => entry.employeeUuid)
          })
          setCohortLoadError(nextCohortLoadError)
        } else {
          setCohortEntries([])
          setSelectedEmployeeUuids([])
          setCohortLoadError(null)
        }
        if (!silent) {
          setLoadError(null)
        }
      } catch (requestError) {
        if (!silent) {
          setLoadError(getApiErrorMessage(requestError, 'Failed to load assessment cycle data.'))
        }
      } finally {
        if (!silent && loadRequestIdRef.current === requestId) {
          setLoading(false)
        }
      }
    },
    [activePlanningContext, planningContextOptions, workspace.slug],
  )

  useEffect(() => {
    void loadAssessmentPage()
  }, [loadAssessmentPage])

  useEffect(() => {
    if (!status || busyAction !== null || !isAssessmentCycleActive(status.current_cycle_status)) {
      return undefined
    }

      const intervalId = window.setInterval(() => {
        if (document.visibilityState === 'visible') {
          void loadAssessmentPage(true)
          void refreshShell()
        }
      }, 15000)

    return () => window.clearInterval(intervalId)
  }, [busyAction, loadAssessmentPage, refreshShell, status])

  async function handleGenerateCycle(mode: 'generate' | 'regenerate') {
    if (mode === 'regenerate') {
      const confirmed = await requestGlobalConfirmation({
        title: 'Regenerate the assessment cycle?',
        description: 'Older assessment links will be superseded after the new cycle is created.',
        confirmLabel: 'Regenerate cycle',
        cancelLabel: 'Keep current cycle',
        tone: 'warn',
      })
      if (!confirmed) {
        return
      }
    }

    if (activePlanningContext && selectedEmployeeUuids.length === 0) {
      setBanner({
        tone: 'warn',
        title: 'Select at least one employee before generating a scoped cycle.',
        messages: ['Context-scoped assessment generation requires an explicit cohort review step.'],
      })
      return
    }

    setBusyAction(mode)
    setBanner(null)

    try {
      const requestBody = {
        title: normalizeOptionalString(cycleTitleDraft),
        ...(activePlanningContext ? { selected_employee_uuids: selectedEmployeeUuids } : {}),
      }
      const cycle =
        mode === 'generate'
          ? await generatePrototypeAssessmentCycle(workspace.slug, requestBody, planningContextOptions)
          : await regeneratePrototypeAssessmentCycle(workspace.slug, requestBody, planningContextOptions)

      await loadAssessmentPage(true)
      void refreshShell()
      setBanner({
        tone: 'success',
        title: mode === 'generate' ? 'Assessment cycle generated.' : 'Assessment cycle regenerated.',
        messages: [
          `${cycle.title} is now ${humanizeToken(cycle.status).toLowerCase()}.`,
          mode === 'regenerate'
            ? 'Older pack links are superseded once the new cycle is available.'
            : 'Share the new pack links from the table below once they are ready.',
        ],
      })
      setCycleTitleDraft('')
    } catch (requestError) {
      setBanner({
        tone: 'error',
        title: mode === 'generate' ? 'Cycle generation failed.' : 'Cycle regeneration failed.',
        messages: getApiErrorMessages(requestError),
      })
    } finally {
      setBusyAction(null)
    }
  }

  async function handleCopyLink(pack: PrototypeAssessmentPack) {
    try {
      await copyTextToClipboard(buildPublicAssessmentUrl(pack.uuid))
      setBanner({
        tone: 'success',
        title: `Copied ${pack.employee_name}'s assessment link.`,
        messages: ['The public pack URL is ready to share.'],
      })
    } catch (copyError) {
      setBanner({
        tone: 'error',
        title: `Could not copy ${pack.employee_name}'s link.`,
        messages: getApiErrorMessages(copyError),
      })
    }
  }

  function handleOpenLink(pack: PrototypeAssessmentPack) {
    openUrl(buildPublicAssessmentUrl(pack.uuid))
  }

  async function handleRefreshAll() {
    await loadAssessmentPage()
    void refreshShell()
  }

  const refreshLabel = busyAction === null ? 'Refresh data' : 'Refreshing locked during mutation'
  const cohortSelectedCount = useMemo(() => selectedEmployeeUuids.length, [selectedEmployeeUuids])

  if (loading && status === null) {
    return (
      <LoadingState
        title="Loading assessments"
        description="Fetching the current cycle, latest attempt status, and current pack list."
      />
    )
  }

  if (loadError && status === null) {
    return (
      <ErrorState
        title="Assessments failed to load"
        description={loadError}
        onRetry={() => void handleRefreshAll()}
      />
    )
  }

  return (
    <div className="page-stack assessment-page-stack">
      <CollapsibleHero
        tag="Stage 07"
        title="Assessment cycle ops and public links"
        className="assessment-hero"
        statusSlot={<StatusChip status={status?.current_cycle_status || 'not_started'} />}
      >
        <div className="hero-copy">
          <p>
            Generate or regenerate the current self-assessment cycle, track pack progress, and share one direct public
            link per employee without leaving the workspace shell.
          </p>
          {assessmentStage?.recommended_action ? <p>{assessmentStage.recommended_action}</p> : null}
        </div>

        <div className="assessment-hero-rail">
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
            <span className="summary-label">Current cycle status</span>
            <strong>{humanizeToken(status?.current_cycle_status || 'not_started')}</strong>
            <StatusChip status={status?.current_cycle_status || 'not_started'} />
          </div>
        </div>
      </CollapsibleHero>

      {activePlanningContext ? (
        <section className="inline-banner inline-banner-info">
          <strong>Scoped assessment flow</strong>
          <span>
            Assessment generation, cycle status, and pack lists are scoped to {activePlanningContext.name}. The employee cohort below is based on this context’s role matches.
          </span>
        </section>
      ) : null}

      {isArchivedContext ? (
        <section className="inline-banner inline-banner-warn">
          <strong>Archived contexts are read-only for assessment generation.</strong>
          <span>Current cycle data remains visible, but generate and regenerate actions are disabled while this context is archived.</span>
        </section>
      ) : null}

      {renderBanner(banner)}

      {loadError && status !== null ? (
        <ErrorState
          title="Latest assessment refresh failed"
          description={`${loadError} Showing the most recent successful assessment data.`}
          onRetry={() => void handleRefreshAll()}
          compact
        />
      ) : null}

      {!hasPublishedBlueprint ? (
        <section className="inline-banner inline-banner-warn">
          <strong>Publish a blueprint before generating assessments.</strong>
          <div className="form-actions">
            <AppLink className="primary-button link-button" to={buildScopedWorkspacePath('blueprint')}>
              Open blueprint review
            </AppLink>
          </div>
        </section>
      ) : null}

      {assessmentStage?.blockers?.length ? (
        <section className="board-panel">
          <div className="panel-heading">
            <span className="section-tag">Stage blockers</span>
            <h3>Resolve these before the cycle can run cleanly</h3>
          </div>
          <div className="blocker-stack">
            {assessmentStage.blockers.map((blocker) => (
              <article key={blocker} className="blocker-item">
                <p>{blocker}</p>
              </article>
            ))}
          </div>
        </section>
      ) : null}

      <section className="board-panel">
        <div className="panel-heading">
          <span className="section-tag">Cycle controls</span>
          <h3>Generate, regenerate, and refresh the current cycle</h3>
          <p>
            Regenerate only when you intentionally want to supersede older employee links with a new assessment cycle.
          </p>
        </div>

        {activePlanningContext ? (
          <>
            {cohortLoadError ? (
              <section className="inline-banner inline-banner-warn">
                <strong>Cohort preview could not be loaded.</strong>
                <span>{cohortLoadError}</span>
              </section>
            ) : null}

            <section className="detail-stack">
              <div className="workspace-card-head">
                <div>
                  <span className="section-tag">Cohort review</span>
                  <h4>{cohortSelectedCount} selected employee(s)</h4>
                </div>
                <StatusChip status={cohortSelectedCount > 0 ? 'ready' : 'blocked'} />
              </div>
              <p>
                The server defaults to employees with a strong role match in this context. You can fine-tune the cohort before generation.
              </p>

              {cohortEntries.length > 0 ? (
                <div className="review-card-grid">
                  {cohortEntries.map((entry) => {
                    const isSelected = selectedEmployeeUuids.includes(entry.employeeUuid)
                    return (
                      <article key={entry.employeeUuid} className={isSelected ? 'review-card is-selected' : 'review-card'}>
                        <div className="review-card-head">
                          <div>
                            <span className="summary-label">Best fit role</span>
                            <h4>{entry.fullName}</h4>
                          </div>
                          <StatusChip status={isSelected ? 'completed' : 'ready'} />
                        </div>
                        <div className="detail-stack">
                          <p>{entry.bestFitRole}</p>
                          <p>{formatPercent(entry.fitScore <= 1 ? entry.fitScore : entry.fitScore / 100)} fit score</p>
                          <p>
                            {entry.relatedInitiatives.length > 0
                              ? `Initiatives: ${entry.relatedInitiatives.join(', ')}`
                              : 'No initiative references were attached to the best-fit role.'}
                          </p>
                        </div>
                        <div className="form-actions">
                          <label className="field-label">
                            <span>Select for cycle</span>
                            <input
                              type="checkbox"
                              checked={isSelected}
                              disabled={busyAction !== null || isArchivedContext}
                              onChange={(event) => {
                                const checked = event.target.checked
                                setCohortSelectionMode('manual')
                                setSelectedEmployeeUuids((currentValue) => (
                                  checked
                                    ? [...currentValue, entry.employeeUuid].filter((value, index, items) => items.indexOf(value) === index)
                                    : currentValue.filter((employeeUuid) => employeeUuid !== entry.employeeUuid)
                                ))
                              }}
                            />
                          </label>
                        </div>
                      </article>
                    )
                  })}
                </div>
              ) : (
                <section className="inline-banner inline-banner-warn">
                  <strong>No matched cohort is available for this context yet.</strong>
                  <span>Publish or refresh the scoped blueprint first so role matches can seed the downstream assessment cohort.</span>
                </section>
              )}
            </section>
          </>
        ) : null}

        <div className="assessment-control-grid">
          <label className="field-label">
            <span>Cycle title</span>
            <input
              className="text-input"
              value={cycleTitleDraft}
              onChange={(event) => setCycleTitleDraft(event.target.value)}
              placeholder={currentCycle?.title || 'Initial assessment cycle'}
              disabled={busyAction !== null || isArchivedContext}
            />
          </label>

          <div className="assessment-control-actions">
            <button
              className="primary-button"
              onClick={() => void handleGenerateCycle('generate')}
              disabled={!hasPublishedBlueprint || busyAction !== null || isArchivedContext || (activePlanningContext !== null && cohortSelectedCount === 0)}
            >
              {busyAction === 'generate' ? 'Generating cycle...' : 'Generate cycle'}
            </button>
            {status?.current_cycle_uuid ? (
              <button
                className="secondary-button"
                onClick={() => void handleGenerateCycle('regenerate')}
                disabled={!hasPublishedBlueprint || busyAction !== null || isArchivedContext || (activePlanningContext !== null && cohortSelectedCount === 0)}
              >
                {busyAction === 'regenerate' ? 'Regenerating cycle...' : 'Regenerate cycle'}
              </button>
            ) : null}
            <button
              className="secondary-button"
              onClick={() => void handleRefreshAll()}
              disabled={busyAction !== null}
            >
              {refreshLabel}
            </button>
          </div>
        </div>
      </section>

      <section className="summary-grid">
        <article className="summary-card">
          <span className="summary-label">Current cycle</span>
          <div className="status-row">
            <strong>{formatShortId(status?.current_cycle_uuid)}</strong>
            <StatusChip status={status?.current_cycle_status || 'not_started'} />
          </div>
          <p>{getCurrentCycleSupportingCopy(currentCycle, status)}</p>
        </article>

        <article className="summary-card">
          <span className="summary-label">Latest attempt</span>
          <div className="status-row">
            <strong>{formatShortId(status?.latest_attempt_uuid)}</strong>
            <StatusChip status={status?.latest_attempt_status || 'not_started'} />
          </div>
          <p>{getLatestAttemptSupportingCopy(status)}</p>
        </article>

        <article className="summary-card">
          <span className="summary-label">Pack completion</span>
          <strong>{formatPercent(status?.completion_rate)}</strong>
          <p>
            {status?.submitted_packs || 0} submitted, {status?.opened_packs || 0} opened, {status?.generated_packs || 0} not started.
          </p>
        </article>

        <article className="summary-card">
          <span className="summary-label">Submitted evidence coverage</span>
          <strong>{status?.employees_with_submitted_self_assessment || 0}</strong>
          <p>Employees with persisted self-assessment evidence for the current usable cycle.</p>
        </article>
      </section>

      {status?.employees_missing_packs?.length ? (
        <section className="board-panel">
          <div className="panel-heading">
            <span className="section-tag">Employees missing packs</span>
            <h3>{status.employees_missing_packs.length} employee(s) do not currently have a pack</h3>
            <p>These employees will not receive a public link until the cycle includes them.</p>
          </div>

          <div className="review-card-grid">
            {status.employees_missing_packs.map((employee) => (
              <article key={employee.employee_uuid} className="review-card">
                <div className="review-card-head">
                  <div>
                    <span className="summary-label">Missing pack</span>
                    <h4>{employee.full_name}</h4>
                  </div>
                  <StatusChip status="blocked" />
                </div>
                <p>{employee.current_title || 'Current title not captured'}</p>
              </article>
            ))}
          </div>
        </section>
      ) : null}

      {status?.current_cycle_uuid === null ? (
        <section className="board-panel">
          <EmptyState
            title="No current assessment cycle yet"
            description={
              hasPublishedBlueprint
                ? 'Generate the first cycle once you are ready to issue employee links.'
                : 'A published blueprint is required before the first cycle can be generated.'
            }
            action={
              hasPublishedBlueprint ? (
                <button
                  className="primary-button"
                  onClick={() => void handleGenerateCycle('generate')}
                  disabled={busyAction !== null || isArchivedContext || (activePlanningContext !== null && cohortSelectedCount === 0)}
                >
                  Generate first cycle
                </button>
              ) : (
                <AppLink className="primary-button link-button" to={buildScopedWorkspacePath('blueprint')}>
                  Review blueprint
                </AppLink>
              )
            }
          />
        </section>
      ) : (
        <section className="board-panel">
          <div className="panel-heading">
            <span className="section-tag">Current cycle packs</span>
            <h3>Track employee progress and share the direct links</h3>
            <p>
              The table below is scoped to the current usable cycle only. Regeneration supersedes older employee links.
            </p>
          </div>

          {currentCycle ? (
            <div className="assessment-cycle-meta-grid">
              <article className="info-card">
                <span className="summary-label">Cycle title</span>
                <strong>{currentCycle.title}</strong>
                <p>Created {formatDateTime(currentCycle.created_at)}</p>
              </article>
              <article className="info-card">
                <span className="summary-label">Blueprint run</span>
                <strong>{formatShortId(currentCycle.blueprint_run_uuid)}</strong>
                <p>Assessments depend on the currently published blueprint lineage.</p>
              </article>
              <article className="info-card">
                <span className="summary-label">Question target</span>
                <strong>{readNumber(currentCycle.configuration.question_count_target) ?? 0}</strong>
                <p>Targeted questions plus hidden-skills and aspiration prompts make up the current pack shape.</p>
              </article>
            </div>
          ) : null}

          {packs.length === 0 ? (
            <EmptyState
              title="No packs returned for the current cycle"
              description="The cycle exists, but the latest pack list endpoint returned no employee packs yet."
            />
          ) : (
            <div className="source-table-shell">
              <table className="source-table assessment-pack-table">
                <thead>
                  <tr>
                    <th>Employee</th>
                    <th>Pack status</th>
                    <th>Opened</th>
                    <th>Submitted</th>
                    <th>Question set</th>
                    <th>Share</th>
                  </tr>
                </thead>
                <tbody>
                  {packs.map((pack) => (
                    <tr key={pack.uuid}>
                      <td>
                        <div className="source-primary-cell">
                          <strong>{pack.employee_name}</strong>
                          <p>{getPackRoleSummary(pack)}</p>
                          <p>{pack.title || `Pack ${formatShortId(pack.uuid)}`}</p>
                        </div>
                      </td>
                      <td>
                        <div className="source-secondary-cell">
                          <StatusChip status={pack.status} />
                          <p>Pack UUID: {formatShortId(pack.uuid)}</p>
                        </div>
                      </td>
                      <td>{formatDateTime(pack.opened_at)}</td>
                      <td>{formatDateTime(pack.submitted_at)}</td>
                      <td>
                        <div className="source-secondary-cell">
                          <strong>{getPackQuestionCount(pack)} targeted question(s)</strong>
                          <p>{humanizeToken(pack.questionnaire_version || 'stage7-v1')}</p>
                        </div>
                      </td>
                      <td>
                        <div className="source-action-group">
                          <button
                            className="secondary-button source-action-button"
                            onClick={() => void handleCopyLink(pack)}
                          >
                            Copy link
                          </button>
                          <button
                            className="secondary-button source-action-button"
                            onClick={() => handleOpenLink(pack)}
                          >
                            Open link
                          </button>
                        </div>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </section>
      )}
    </div>
  )
}
