import { useCallback, useEffect, useMemo, useRef, useState } from 'react'

import { AppLink } from '../app/navigation'
import { useWorkspaceShell } from '../app/WorkspaceLayout'
import { CollapsibleHero } from '../shared/ui/CollapsibleHero'
import { getApiErrorMessage, getApiErrorMessages, isApiError } from '../shared/api'
import { formatDateTime, formatPercent, formatShortId } from '../shared/formatters'
import {
  canBuildMatrix,
  findMatrixCellDetail,
  getAdvisoryFlagLabel,
  getIncompletenessFlagLabel,
  getMatrixBuildBlockers,
  getMatrixCellTone,
  getPrimaryRoleMatch,
  getRoleMatchStatus,
  getRoleMatchStatusLabel,
  normalizeHeatmapColumns,
  normalizeHeatmapRows,
  normalizeMatrixCells,
  normalizeMatrixEmployeeSlice,
  readRecord,
  readRecordArray,
  readString,
  readStringArray,
  sortRoleMatchEntries,
  type MatrixCellDetail,
  type MatrixEmployeeSlice,
} from '../shared/matrixReview'
import {
  buildPrototypeEvidenceMatrix,
  getPrototypeAssessmentStatus,
  getPrototypeCVEvidenceStatus,
  getPrototypeLatestEvidenceMatrix,
  getPrototypeLatestEvidenceMatrixCells,
  getPrototypeLatestEvidenceMatrixEmployee,
  getPrototypeLatestEvidenceMatrixHeatmap,
  getPrototypeLatestEvidenceMatrixRisks,
  getPrototypeOrgContextSummary,
  listPrototypeRoleMatches,
  type PrototypeAssessmentStatusResponse,
  type PrototypeCVEvidenceStatusResponse,
  type PrototypeEmployeeRoleMatchListResponse,
  type PrototypeEvidenceMatrixRun,
  type PrototypeOrgContextSummaryResponse,
} from '../shared/prototypeApi'
import { humanizeToken } from '../shared/workflow'
import EmptyState from '../shared/ui/EmptyState'
import ErrorState from '../shared/ui/ErrorState'
import LoadingState from '../shared/ui/LoadingState'
import SlideOverPanel from '../shared/ui/SlideOverPanel'
import StatusChip from '../shared/ui/StatusChip'

type BannerTone = 'info' | 'success' | 'warn' | 'error'

type BannerState = {
  tone: BannerTone
  title: string
  messages: string[]
}

type SelectedCellContext = {
  employeeUuid: string
  employeeName: string
  columnKey: string
  columnLabel: string
}

type LoadMatrixPageOptions = {
  silent?: boolean
  failOnError?: boolean
}

type LoadMatrixPageResult = {
  latestMatrix: PrototypeEvidenceMatrixRun | null
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

function mapRoleMatchStatusToChip(status: ReturnType<typeof getRoleMatchStatus>) {
  if (status === 'matched') {
    return 'completed'
  }

  if (status === 'uncertain') {
    return 'action_required'
  }

  return 'blocked'
}

function formatFitScore(value: number) {
  return formatPercent(value <= 1 ? value : value / 100)
}

function formatMatrixBuildSupportingCopy(
  cvStatus: PrototypeCVEvidenceStatusResponse | null,
  orgSummary: PrototypeOrgContextSummaryResponse | null,
) {
  const employeeCount = orgSummary?.employee_count ?? 0
  const evidenceRows = orgSummary?.skill_evidence_count ?? cvStatus?.skill_evidence_count ?? 0
  return `${employeeCount} workspace employee(s) and ${evidenceRows} evidence row(s) are currently visible to the shared evidence base.`
}

function formatListPreview(items: string[], fallback: string) {
  if (items.length === 0) {
    return fallback
  }

  return items.join(', ')
}

function formatTopGap(item: Record<string, unknown>) {
  const roleName = readString(item.role_name) || 'Role'
  const skillName = readString(item.skill_name_en) || readString(item.skill_key) || 'Skill'
  return `${roleName} / ${skillName}: gap ${Number(item.average_gap || 0).toFixed(1)}`
}

function formatConcentrationRisk(item: Record<string, unknown>) {
  const roleName = readString(item.role_name) || 'Role'
  const skillName = readString(item.skill_name_en) || readString(item.skill_key) || 'Skill'
  const readyCount = Number(item.ready_employee_count || 0)
  return `${roleName} / ${skillName}: ${readyCount} ready employee(s)`
}

function formatNearFitCandidate(item: Record<string, unknown>) {
  const fullName = readString(item.full_name) || 'Employee'
  const roleName = readString(item.role_name) || 'Role'
  return `${fullName}: near-fit for ${roleName}`
}

function formatUncoveredRole(item: Record<string, unknown>) {
  const roleName = readString(item.role_name) || 'Role'
  const reason = readString(item.reason)
  return reason ? `${roleName}: ${reason}` : roleName
}

function formatEvidenceSourceMixItem(item: Record<string, unknown>) {
  const sourceKind = humanizeToken(readString(item.source_kind) || 'unknown')
  const level = Number(item.current_level || 0).toFixed(1)
  const rowCount = Number(item.row_count || 0)
  const totalWeight = Number(item.total_weight || 0).toFixed(2)
  return `${sourceKind}: level ${level}, weight ${totalWeight}, ${rowCount} row(s)`
}

function formatEvidenceRowItem(item: Record<string, unknown>) {
  const sourceKind = humanizeToken(readString(item.source_kind) || 'unknown')
  const excerpt = readString(item.evidence_text)
  return excerpt ? `${sourceKind}: ${excerpt}` : sourceKind
}

function formatProvenanceItem(item: Record<string, unknown>) {
  const docType = humanizeToken(readString(item.doc_type) || readString(item.retrieval_lane) || 'evidence')
  const excerpt = readString(item.excerpt)
  return excerpt ? `${docType}: ${excerpt}` : docType
}

function getSliceMismatchMessage(sliceLabel: string) {
  return `${sliceLabel} refreshed for a different matrix run. Refresh the page to inspect the newest matrix cleanly.`
}

function hasRoleCoverageIssue(employeeSlice: MatrixEmployeeSlice) {
  return (
    employeeSlice.best_fit_role === null ||
    employeeSlice.role_match_status === 'unmatched' ||
    employeeSlice.role_match_status === 'uncertain' ||
    employeeSlice.advisory_flags.includes('role_match_uncertain')
  )
}

function getEmployeeSliceActionReasons(employeeSlice: MatrixEmployeeSlice) {
  const reasons: string[] = []
  const flags = new Set(employeeSlice.insufficient_evidence_flags)

  if (employeeSlice.best_fit_role === null || employeeSlice.role_match_status === 'unmatched') {
    reasons.push('No primary role match was produced, so this employee does not yet have a trustworthy role baseline for the matrix.')
  } else if (employeeSlice.role_match_status === 'uncertain' || employeeSlice.advisory_flags.includes('role_match_uncertain')) {
    reasons.push('The current role match is tentative, so the gap view should be treated as directional rather than final.')
  }

  if (flags.has('no_evidence')) {
    reasons.push('Some required skills still have no direct evidence at all.')
  }
  if (flags.has('single_source_only')) {
    reasons.push('Some skill scores rely on only one evidence source.')
  }
  if (flags.has('self_report_only')) {
    reasons.push('Some skill scores rely only on self-assessment answers.')
  }
  if (flags.has('cv_only')) {
    reasons.push('Some skill scores rely only on CV evidence.')
  }
  if (flags.has('indirect_evidence_only')) {
    reasons.push('Some skill scores are inferred from indirect ESCO support instead of exact direct skill evidence.')
  }
  if (flags.has('low_confidence')) {
    reasons.push('Some skill rows are low-confidence and should not drive strong planning decisions yet.')
  }
  if (flags.has('thin_evidence')) {
    reasons.push('Some skill rows are supported by very little evidence.')
  }

  return reasons
}

function getEmployeeSliceRecommendedSteps(employeeSlice: MatrixEmployeeSlice) {
  const steps: string[] = []
  const flags = new Set(employeeSlice.insufficient_evidence_flags)

  if (hasRoleCoverageIssue(employeeSlice)) {
    steps.push('Review the employee role match and blueprint role coverage before trusting the gap profile.')
  }

  if (
    flags.has('no_evidence') ||
    flags.has('single_source_only') ||
    flags.has('self_report_only') ||
    flags.has('cv_only') ||
    flags.has('indirect_evidence_only') ||
    flags.has('low_confidence') ||
    flags.has('thin_evidence')
  ) {
    steps.push('Review parse evidence for this employee and strengthen the evidence mix before using this row for plans.')
  }

  if (flags.has('self_report_only') || flags.has('cv_only') || flags.has('single_source_only')) {
    steps.push('Add at least one second evidence source so the matrix is not driven by a single lane.')
  }

  return steps
}

export default function WorkspaceMatrixPage() {
  const {
    workspace,
    workflow,
    activePlanningContext,
    planningContextOptions,
    refreshShell,
    buildScopedWorkspacePath,
  } = useWorkspaceShell()
  const matrixStage = workflow.stages.find((stage) => stage.key === 'matrix') ?? null
  const isArchivedContext = activePlanningContext?.status === 'archived'
  const [orgSummary, setOrgSummary] = useState<PrototypeOrgContextSummaryResponse | null>(null)
  const [cvStatus, setCvStatus] = useState<PrototypeCVEvidenceStatusResponse | null>(null)
  const [assessmentStatus, setAssessmentStatus] = useState<PrototypeAssessmentStatusResponse | null>(null)
  const [roleMatchResponse, setRoleMatchResponse] = useState<PrototypeEmployeeRoleMatchListResponse | null>(null)
  const [latestMatrix, setLatestMatrix] = useState<PrototypeEvidenceMatrixRun | null>(null)
  const [loading, setLoading] = useState(true)
  const [loadError, setLoadError] = useState<string | null>(null)
  const [banner, setBanner] = useState<BannerState | null>(null)
  const [busyAction, setBusyAction] = useState<string | null>(null)
  const [matrixTitleDraft, setMatrixTitleDraft] = useState('')
  const [heatmapPayload, setHeatmapPayload] = useState<Record<string, unknown> | null>(null)
  const [riskPayload, setRiskPayload] = useState<Record<string, unknown> | null>(null)
  const [selectedEmployeeUuid, setSelectedEmployeeUuid] = useState<string | null>(null)
  const [employeeReloadToken, setEmployeeReloadToken] = useState(0)
  const [employeeSlice, setEmployeeSlice] = useState<MatrixEmployeeSlice | null>(null)
  const [employeeLoading, setEmployeeLoading] = useState(false)
  const [employeeError, setEmployeeError] = useState<string | null>(null)
  const [selectedCellContext, setSelectedCellContext] = useState<SelectedCellContext | null>(null)
  const [latestCells, setLatestCells] = useState<MatrixCellDetail[] | null>(null)
  const [cellsLoading, setCellsLoading] = useState(false)
  const [cellsError, setCellsError] = useState<string | null>(null)
  const matrixLoadRequestIdRef = useRef(0)
  const latestMatrixRunUuidRef = useRef<string | null>(null)

  const hasPublishedBlueprint = Boolean(workflow.summary.current_published_blueprint_run_uuid)
  const latestMatrixAttemptStatus = workflow.summary.latest_matrix_status || matrixStage?.status || 'not_started'
  const matrixBuildBlockers = getMatrixBuildBlockers(matrixStage?.blockers || [], hasPublishedBlueprint)
  const buildAllowed = canBuildMatrix(latestMatrixAttemptStatus, hasPublishedBlueprint)
  const latestCompletedMatrixStatus = latestMatrix?.status || 'not_started'
  const employeeSliceActionReasons = useMemo(
    () => (employeeSlice ? getEmployeeSliceActionReasons(employeeSlice) : []),
    [employeeSlice],
  )
  const employeeSliceRecommendedSteps = useMemo(
    () => (employeeSlice ? getEmployeeSliceRecommendedSteps(employeeSlice) : []),
    [employeeSlice],
  )
  const employeeSliceNeedsAction = Boolean(
    employeeSlice &&
      (employeeSlice.insufficient_evidence_flags.length > 0 ||
        employeeSlice.advisory_flags.length > 0 ||
        employeeSlice.best_fit_role === null ||
        employeeSlice.role_match_status === 'unmatched' ||
        employeeSlice.role_match_status === 'uncertain'),
  )

  const roleMatchEntries = useMemo(
    () => sortRoleMatchEntries(roleMatchResponse?.employees || []),
    [roleMatchResponse],
  )

  const roleMatchCounts = useMemo(() => {
    return roleMatchEntries.reduce(
      (accumulator, entry) => {
        accumulator[getRoleMatchStatus(entry)] += 1
        return accumulator
      },
      {
        matched: 0,
        uncertain: 0,
        unmatched: 0,
      },
    )
  }, [roleMatchEntries])

  const heatmapColumns = useMemo(
    () => normalizeHeatmapColumns(heatmapPayload || {}),
    [heatmapPayload],
  )
  const heatmapRows = useMemo(
    () => normalizeHeatmapRows(heatmapPayload || {}),
    [heatmapPayload],
  )

  const riskTopPriorityGaps = useMemo(
    () => readRecordArray((riskPayload || {}).top_priority_gaps),
    [riskPayload],
  )
  const concentrationRisks = useMemo(
    () => readRecordArray((riskPayload || {}).concentration_risks),
    [riskPayload],
  )
  const nearFitCandidates = useMemo(
    () => readRecordArray((riskPayload || {}).near_fit_candidates),
    [riskPayload],
  )
  const uncoveredRoles = useMemo(
    () => readRecordArray((riskPayload || {}).uncovered_roles),
    [riskPayload],
  )
  const insufficientEvidenceEmployees = useMemo(
    () => readRecordArray((latestMatrix?.incompleteness_payload || {}).employees_with_insufficient_evidence),
    [latestMatrix],
  )
  const matrixEmployeeUuidSet = useMemo(
    () =>
      new Set(
        readRecordArray((latestMatrix?.matrix_payload || {}).employees)
          .map((item) => readString(item.employee_uuid))
          .filter((employeeUuid): employeeUuid is string => Boolean(employeeUuid)),
      ),
    [latestMatrix],
  )

  const selectedCellDetail = useMemo(
    () =>
      selectedCellContext && latestCells
        ? findMatrixCellDetail(latestCells, selectedCellContext.employeeUuid, selectedCellContext.columnKey)
        : null,
    [latestCells, selectedCellContext],
  )

  const loadMatrixPage = useCallback(
    async ({ silent = false, failOnError = false }: LoadMatrixPageOptions = {}): Promise<LoadMatrixPageResult> => {
      const requestId = matrixLoadRequestIdRef.current + 1
      matrixLoadRequestIdRef.current = requestId

      if (!silent) {
        setLoading(true)
      }
      setLoadError(null)

      try {
        const [
          orgSummaryResponse,
          cvStatusResponse,
          assessmentStatusResponse,
          roleMatchListResponse,
          latestMatrixResponse,
        ] = await Promise.all([
          getPrototypeOrgContextSummary(workspace.slug, planningContextOptions),
          getPrototypeCVEvidenceStatus(workspace.slug),
          getPrototypeAssessmentStatus(workspace.slug, planningContextOptions),
          listPrototypeRoleMatches(workspace.slug, planningContextOptions),
          requestOptional(getPrototypeLatestEvidenceMatrix(workspace.slug, planningContextOptions)),
        ])

        let nextHeatmapPayload: Record<string, unknown> | null = null
        let nextRiskPayload: Record<string, unknown> | null = null

        if (latestMatrixResponse) {
          const [heatmapResponse, riskResponse] = await Promise.all([
            requestOptional(getPrototypeLatestEvidenceMatrixHeatmap(workspace.slug, planningContextOptions)),
            requestOptional(getPrototypeLatestEvidenceMatrixRisks(workspace.slug, planningContextOptions)),
          ])

          if (heatmapResponse && heatmapResponse.run_uuid !== latestMatrixResponse.uuid) {
            throw new Error(getSliceMismatchMessage('The latest heatmap'))
          }

          if (riskResponse && riskResponse.run_uuid !== latestMatrixResponse.uuid) {
            throw new Error(getSliceMismatchMessage('The latest risk summary'))
          }

          nextHeatmapPayload = readRecord(heatmapResponse?.payload)
          nextRiskPayload = readRecord(riskResponse?.payload)
        }

        if (matrixLoadRequestIdRef.current !== requestId) {
          return { latestMatrix: latestMatrixResponse }
        }

        const nextMatrixRunUuid = latestMatrixResponse?.uuid ?? null
        const matrixRunChanged = latestMatrixRunUuidRef.current !== nextMatrixRunUuid
        latestMatrixRunUuidRef.current = nextMatrixRunUuid

        if (!latestMatrixResponse || matrixRunChanged) {
          setLatestCells(null)
          setCellsError(null)
          setSelectedEmployeeUuid(null)
          setEmployeeSlice(null)
          setEmployeeError(null)
          setSelectedCellContext(null)
        }

        setOrgSummary(orgSummaryResponse)
        setCvStatus(cvStatusResponse)
        setAssessmentStatus(assessmentStatusResponse)
        setRoleMatchResponse(roleMatchListResponse)
        setLatestMatrix(latestMatrixResponse)
        setHeatmapPayload(nextHeatmapPayload)
        setRiskPayload(nextRiskPayload)
        return { latestMatrix: latestMatrixResponse }
      } catch (requestError) {
        if (matrixLoadRequestIdRef.current === requestId) {
          setLoadError(getApiErrorMessage(requestError, 'Failed to load matrix review data.'))
        }
        if (failOnError) {
          throw requestError
        }
        return { latestMatrix: null }
      } finally {
        if (!silent && matrixLoadRequestIdRef.current === requestId) {
          setLoading(false)
        }
      }
    },
    [planningContextOptions, workspace.slug],
  )

  const loadLatestCells = useCallback(async () => {
    if (cellsLoading || !latestMatrix) {
      return
    }

    const expectedRunUuid = latestMatrix.uuid
    setCellsLoading(true)
    setCellsError(null)

    try {
      const response = await getPrototypeLatestEvidenceMatrixCells(workspace.slug, planningContextOptions)
      if (latestMatrixRunUuidRef.current !== expectedRunUuid) {
        return
      }
      if (response.run_uuid !== expectedRunUuid) {
        setLatestCells(null)
        setCellsError(getSliceMismatchMessage('Detailed cell data'))
        return
      }
      setLatestCells(normalizeMatrixCells(readRecord(response.payload)))
    } catch (requestError) {
      setCellsError(getApiErrorMessage(requestError, 'Failed to load detailed matrix cells.'))
    } finally {
      setCellsLoading(false)
    }
  }, [cellsLoading, latestMatrix, planningContextOptions, workspace.slug])

  useEffect(() => {
    void loadMatrixPage()
  }, [loadMatrixPage])

  useEffect(() => {
    if (!selectedEmployeeUuid || !latestMatrix) {
      setEmployeeSlice(null)
      setEmployeeError(null)
      return
    }

    let cancelled = false
    const selectedEmployeeId = selectedEmployeeUuid
    const expectedRunUuid = latestMatrix.uuid

    async function loadEmployeeSlice() {
      setEmployeeLoading(true)
      setEmployeeError(null)

      try {
        const response = await getPrototypeLatestEvidenceMatrixEmployee(workspace.slug, selectedEmployeeId, planningContextOptions)
        if (cancelled) {
          return
        }
        if (response.run_uuid !== expectedRunUuid) {
          setEmployeeSlice(null)
          setEmployeeError(getSliceMismatchMessage('Employee slice data'))
          return
        }
        setEmployeeSlice(normalizeMatrixEmployeeSlice(readRecord(response.payload)))
      } catch (requestError) {
        if (!cancelled) {
          setEmployeeError(getApiErrorMessage(requestError, 'Failed to load the employee matrix slice.'))
        }
      } finally {
        if (!cancelled) {
          setEmployeeLoading(false)
        }
      }
    }

    void loadEmployeeSlice()

    return () => {
      cancelled = true
    }
  }, [employeeReloadToken, latestMatrix, planningContextOptions, selectedEmployeeUuid, workspace.slug])

  useEffect(() => {
    if (!selectedCellContext || !latestMatrix || latestCells !== null) {
      return
    }

    void loadLatestCells()
  }, [latestCells, latestMatrix, loadLatestCells, selectedCellContext])

  useEffect(() => {
    setLatestCells(null)
    setCellsError(null)
  }, [latestMatrix?.uuid])

  async function handleBuildMatrix() {
    setBusyAction('build')
    setBanner(null)

    try {
      const run = await buildPrototypeEvidenceMatrix(workspace.slug, {
        title: normalizeOptionalString(matrixTitleDraft),
      }, planningContextOptions)

      if (run.status !== 'completed') {
        await Promise.allSettled([loadMatrixPage({ silent: true }), refreshShell()])
        setBanner({
          tone: 'error',
          title: 'Matrix build failed.',
          messages: [
            `${run.title} returned status ${humanizeToken(run.status)} instead of a completed matrix run.`,
            'The page continues to show the latest completed matrix, if one exists.',
          ],
        })
        return
      }

      const [{ latestMatrix: refreshedMatrix }] = await Promise.all([
        loadMatrixPage({ silent: true, failOnError: true }),
        refreshShell(),
      ])

      if (refreshedMatrix?.uuid !== run.uuid) {
        throw new Error(
          'The matrix build completed, but the latest matrix view did not advance to the new run yet. Refresh again in a moment.',
        )
      }

      setBanner({
        tone: 'success',
        title: 'Evidence matrix built.',
        messages: [
          `${run.title} completed with matrix version ${run.matrix_version}.`,
          'The page now reflects the latest completed matrix for the current published blueprint.',
        ],
      })
      setMatrixTitleDraft('')
    } catch (requestError) {
      setBanner({
        tone: 'error',
        title: 'Matrix build failed.',
        messages: getApiErrorMessages(requestError),
      })
    } finally {
      setBusyAction(null)
    }
  }

  async function handleRefreshAll() {
    await Promise.allSettled([loadMatrixPage(), refreshShell()])
  }

  if (loading && orgSummary === null) {
    return (
      <LoadingState
        title="Loading matrix review"
        description="Fetching role matches, evidence readiness, and the latest completed matrix slices."
      />
    )
  }

  if (loadError && orgSummary === null) {
    return (
      <ErrorState
        title="Matrix review failed to load"
        description={loadError}
        onRetry={() => void handleRefreshAll()}
      />
    )
  }

  return (
    <div className="page-stack matrix-page-stack">
      <CollapsibleHero
        tag="Stage 08"
        title="Evidence matrix and role-match review"
        className="matrix-hero"
        statusSlot={<StatusChip status={latestMatrixAttemptStatus} />}
      >
        <div className="hero-copy">
          <p>
            Review whether the workspace evidence is trustworthy enough for matrix decisions, inspect role matches, and
            drill into the latest completed matrix without leaving the shell.
          </p>
          {matrixStage?.recommended_action ? <p>{matrixStage.recommended_action}</p> : null}
        </div>

        <div className="matrix-hero-rail">
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
            <span className="summary-label">Current assessment cycle</span>
            <strong>{humanizeToken(assessmentStatus?.current_cycle_status || 'not_started')}</strong>
            <p>{formatShortId(assessmentStatus?.current_cycle_uuid)}</p>
          </div>
          <div className="route-badge">
            <span className="summary-label">Latest assessment attempt</span>
            <strong>{humanizeToken(assessmentStatus?.latest_attempt_status || workflow.summary.latest_assessment_status || 'not_started')}</strong>
            <p>{formatShortId(assessmentStatus?.latest_attempt_uuid)}</p>
          </div>
          <div className="route-badge">
            <span className="summary-label">Latest matrix attempt</span>
            <strong>{humanizeToken(latestMatrixAttemptStatus)}</strong>
            <p>{latestMatrix ? `Viewing completed run ${formatShortId(latestMatrix.uuid)}` : 'No completed matrix yet'}</p>
            <StatusChip status={latestMatrixAttemptStatus} />
          </div>
        </div>
      </CollapsibleHero>

      {activePlanningContext ? (
        <section className="inline-banner inline-banner-info">
          <strong>Scoped interpretation, shared evidence</strong>
          <span>
            Employee CV and assessment evidence stay org-wide, but role-match interpretation, matrix lineage, and downstream planning here are scoped to {activePlanningContext.name}.
          </span>
        </section>
      ) : null}

      {isArchivedContext ? (
        <section className="inline-banner inline-banner-warn">
          <strong>Archived contexts are read-only for matrix generation.</strong>
          <span>You can inspect the latest scoped matrix, but building a new matrix is disabled while this context is archived.</span>
        </section>
      ) : null}

      {renderBanner(banner)}

      {loadError && orgSummary !== null ? (
        <ErrorState
          title="Latest matrix refresh failed"
          description={`${loadError} Showing the most recent successful matrix data.`}
          onRetry={() => void handleRefreshAll()}
          compact
        />
      ) : null}

      <section className="summary-grid">
        <article className="summary-card">
          <span className="summary-label">CV evidence</span>
          <div className="status-row">
            <strong>{cvStatus?.matched_count || 0} matched</strong>
            <StatusChip status={(cvStatus?.unresolved_source_count || 0) > 0 ? 'action_required' : 'completed'} />
          </div>
          <p>
            {cvStatus?.unmatched_count || 0} unmatched and {cvStatus?.ambiguous_count || 0} ambiguous CV profile(s).
          </p>
        </article>

        <article className="summary-card">
          <span className="summary-label">Workspace org data</span>
          <strong>{orgSummary?.employee_count || 0} employee(s)</strong>
          <p>
            {orgSummary?.role_match_count || 0} effective role match(es) and {orgSummary?.skill_evidence_count || 0} workspace-wide evidence row(s).
          </p>
        </article>

        <article className="summary-card">
          <span className="summary-label">Role matches</span>
          <strong>{roleMatchCounts.matched} matched</strong>
          <p>
            {roleMatchCounts.uncertain} uncertain and {roleMatchCounts.unmatched} unmatched employee(s).
          </p>
        </article>

        <article className="summary-card">
          <span className="summary-label">Matrix readiness</span>
          <div className="status-row">
            <strong>{matrixBuildBlockers.length > 0 ? `${matrixBuildBlockers.length} blocker(s)` : 'Ready to build'}</strong>
            <StatusChip status={matrixBuildBlockers.length > 0 ? 'blocked' : 'ready'} />
          </div>
          <p>{formatMatrixBuildSupportingCopy(cvStatus, orgSummary)}</p>
        </article>
      </section>

      <section className="board-panel">
        <div className="panel-heading">
          <span className="section-tag">Build controls</span>
          <h3>Build or refresh the latest effective matrix</h3>
          <p>
            Matrix build stays tied to the current published blueprint and the backend’s default assessment-cycle
            selection. Use Parse when the evidence itself needs repair.
          </p>
        </div>

        <div className="matrix-control-grid">
          <label className="field-label">
            <span>Matrix title</span>
            <input
              className="text-input"
              value={matrixTitleDraft}
              onChange={(event) => setMatrixTitleDraft(event.target.value)}
              placeholder={latestMatrix?.title || 'Second-layer evidence matrix'}
              disabled={busyAction !== null || isArchivedContext}
            />
          </label>

          <div className="matrix-control-actions">
            <button
              className="primary-button"
              onClick={() => void handleBuildMatrix()}
              disabled={!buildAllowed || busyAction !== null || isArchivedContext}
            >
              {busyAction === 'build' ? 'Building matrix...' : 'Build matrix'}
            </button>
            <button className="secondary-button" onClick={() => void handleRefreshAll()} disabled={busyAction !== null}>
              Refresh data
            </button>
            <AppLink className="secondary-button link-button" to={buildScopedWorkspacePath('parse')}>
              Review parse & evidence
            </AppLink>
          </div>
        </div>

        {matrixBuildBlockers.length > 0 ? (
          <div className="blocker-stack">
            {matrixBuildBlockers.map((blocker) => (
              <article key={blocker} className="blocker-item">
                <p>{blocker}</p>
              </article>
            ))}
          </div>
        ) : null}
      </section>

      <section className="board-panel">
        <div className="panel-heading">
          <span className="section-tag">Role matches</span>
          <h3>Review the latest effective role fit per employee</h3>
          <p>
            Unmatched and uncertain employees are prioritized first because they reduce confidence in later matrix
            interpretation. These role matches follow the backend’s effective-blueprint contract, which resolves to the
            published blueprint when one exists for the workspace.
          </p>
        </div>

        {roleMatchEntries.length === 0 ? (
          <EmptyState
            title="No role matches are visible yet"
            description="Generate and publish the blueprint first, then return here to review how employees map to the current effective role set."
          />
        ) : (
          <div className="review-card-grid">
            {roleMatchEntries.map((entry) => {
              const roleMatchStatus = getRoleMatchStatus(entry)
              const primaryMatch = getPrimaryRoleMatch(entry)
              const adjacentMatches = entry.matches.slice(1, 3)
              const isInCurrentMatrixCohort = latestMatrix ? matrixEmployeeUuidSet.has(entry.employee_uuid) : false

              return (
                <article key={entry.employee_uuid} className="review-card">
                  <div className="review-card-head">
                    <div>
                      <span className="summary-label">{getRoleMatchStatusLabel(roleMatchStatus)}</span>
                      <h4>{entry.full_name}</h4>
                    </div>
                    <StatusChip status={mapRoleMatchStatusToChip(roleMatchStatus)} />
                  </div>

                  {primaryMatch ? (
                    <div className="detail-stack">
                      <p>
                        Best fit: <strong>{primaryMatch.role_name}</strong> ({formatFitScore(primaryMatch.fit_score)})
                      </p>
                      <p>{primaryMatch.reason || 'No role-match rationale was provided.'}</p>
                      <p>
                        Initiatives: {formatListPreview(primaryMatch.related_initiatives || [], 'No linked initiatives')}
                      </p>
                    </div>
                  ) : (
                    <p>No current role match was produced for this employee.</p>
                  )}

                  {adjacentMatches.length > 0 ? (
                    <ul className="helper-list helper-list-compact">
                      {adjacentMatches.map((match) => (
                        <li key={`${entry.employee_uuid}:${match.role_name}`}>
                          {match.role_name} ({formatFitScore(match.fit_score)})
                        </li>
                      ))}
                    </ul>
                  ) : null}

                  {latestMatrix && !isInCurrentMatrixCohort ? (
                    <p>This employee is outside the assessment cohort used for the latest matrix run, so no employee slice is available yet.</p>
                  ) : null}

                  <div className="form-actions">
                    <button
                      className="secondary-button"
                      onClick={() => setSelectedEmployeeUuid(entry.employee_uuid)}
                      disabled={!latestMatrix || !isInCurrentMatrixCohort}
                    >
                      {!latestMatrix
                        ? 'Build matrix first'
                        : isInCurrentMatrixCohort
                          ? 'Open employee slice'
                          : 'Not in matrix cohort'}
                    </button>
                  </div>
                </article>
              )
            })}
          </div>
        )}
      </section>

      {latestMatrix === null ? (
        <section className="board-panel">
          <EmptyState
            title={latestMatrixAttemptStatus === 'failed' ? 'The latest matrix attempt failed' : 'No completed matrix is available yet'}
            description={
              latestMatrixAttemptStatus === 'failed'
                ? 'The most recent matrix attempt did not produce a completed run. Refresh to confirm the latest state, then repair evidence issues or try another build.'
                : buildAllowed
                  ? 'The page is ready for the first matrix build. Once completed, the latest matrix summary, heatmap, and employee slices will appear here.'
                  : 'Matrix build is still blocked. Resolve the evidence or blueprint issues above, then return here to build the first matrix.'
            }
            action={
              buildAllowed ? (
                <button className="primary-button" onClick={() => void handleBuildMatrix()} disabled={busyAction !== null || isArchivedContext}>
                  {latestMatrixAttemptStatus === 'failed' ? 'Retry matrix build' : 'Build first matrix'}
                </button>
              ) : (
                <AppLink className="primary-button link-button" to={buildScopedWorkspacePath('parse')}>
                  Review evidence quality
                </AppLink>
              )
            }
          />
        </section>
      ) : (
        <>
          <section className="board-panel">
            <div className="panel-heading">
              <span className="section-tag">Latest matrix summary</span>
              <h3>Inspect the current effective matrix before moving into planning</h3>
              <p>The matrix below is the latest completed run for the current published blueprint lineage.</p>
            </div>

            <div className="summary-grid">
              <article className="summary-card">
                <span className="summary-label">Run UUID</span>
                <div className="status-row">
                  <strong>{formatShortId(latestMatrix.uuid)}</strong>
                  <StatusChip status={latestCompletedMatrixStatus} />
                </div>
                <p>Updated {formatDateTime(latestMatrix.updated_at)}</p>
              </article>

              <article className="summary-card">
                <span className="summary-label">Assessment cycle used</span>
                <strong>
                  {formatShortId(
                    readString(latestMatrix.input_snapshot.selected_assessment_cycle_uuid) ||
                      assessmentStatus?.current_cycle_uuid ||
                      workflow.summary.latest_assessment_cycle_uuid,
                  )}
                </strong>
                <p>The backend defaults this to the current blueprint-linked cycle when none is provided explicitly.</p>
              </article>

              <article className="summary-card">
                <span className="summary-label">Employees covered</span>
                <strong>{heatmapRows.length}</strong>
                <p>{heatmapColumns.length} heatmap column(s) currently selected for the summary view.</p>
              </article>

              <article className="summary-card">
                <span className="summary-label">Insufficient evidence</span>
                <strong>{Number((latestMatrix.incompleteness_payload || {}).employees_with_insufficient_evidence_count || 0)}</strong>
                <p>Employees whose matrix rows still carry incompleteness or evidence-confidence limitations.</p>
              </article>
            </div>

            <div className="info-grid">
              <article className="info-card">
                <h4>Team summary</h4>
                <p>{readString((latestMatrix.summary_payload || {}).team_summary) || 'No team summary text was returned.'}</p>
              </article>
              <article className="info-card">
                <h4>Summary flags</h4>
                <p>
                  {formatListPreview(
                    readStringArray((latestMatrix.summary_payload || {}).incompleteness_flags),
                    'No summary-level incompleteness flags were returned.',
                  )}
                </p>
              </article>
            </div>
          </section>

          <section className="board-panel">
            <div className="panel-heading">
              <span className="section-tag">Team signals</span>
              <h3>Top gaps, risks, and coverage limits</h3>
              <p>These sections surface the team-wide issues that matter most before plan generation.</p>
            </div>

            <div className="review-card-grid">
              <article className="review-card">
                <div className="review-card-head">
                  <div>
                    <span className="summary-label">Top priority gaps</span>
                    <h4>{riskTopPriorityGaps.length} gap signal(s)</h4>
                  </div>
                  <StatusChip status={riskTopPriorityGaps.length > 0 ? 'action_required' : 'completed'} />
                </div>
                {riskTopPriorityGaps.length > 0 ? (
                  <ul className="helper-list helper-list-compact">
                    {riskTopPriorityGaps.slice(0, 5).map((item) => (
                      <li key={`${readString(item.role_name)}:${readString(item.skill_key)}`}>{formatTopGap(item)}</li>
                    ))}
                  </ul>
                ) : (
                  <p>No top-priority gaps were returned.</p>
                )}
              </article>

              <article className="review-card">
                <div className="review-card-head">
                  <div>
                    <span className="summary-label">Concentration risks</span>
                    <h4>{concentrationRisks.length} risk(s)</h4>
                  </div>
                  <StatusChip status={concentrationRisks.length > 0 ? 'action_required' : 'completed'} />
                </div>
                {concentrationRisks.length > 0 ? (
                  <ul className="helper-list helper-list-compact">
                    {concentrationRisks.slice(0, 5).map((item) => (
                      <li key={`${readString(item.role_name)}:${readString(item.skill_key)}`}>{formatConcentrationRisk(item)}</li>
                    ))}
                  </ul>
                ) : (
                  <p>No concentration risks were returned.</p>
                )}
              </article>

              <article className="review-card">
                <div className="review-card-head">
                  <div>
                    <span className="summary-label">Near-fit candidates</span>
                    <h4>{nearFitCandidates.length} candidate(s)</h4>
                  </div>
                  <StatusChip status={nearFitCandidates.length > 0 ? 'ready' : 'not_started'} />
                </div>
                {nearFitCandidates.length > 0 ? (
                  <ul className="helper-list helper-list-compact">
                    {nearFitCandidates.slice(0, 5).map((item) => (
                      <li key={`${readString(item.full_name)}:${readString(item.role_name)}`}>{formatNearFitCandidate(item)}</li>
                    ))}
                  </ul>
                ) : (
                  <p>No near-fit internal mobility signals were returned.</p>
                )}
              </article>

              <article className="review-card">
                <div className="review-card-head">
                  <div>
                    <span className="summary-label">Incompleteness</span>
                    <h4>{Number((latestMatrix.incompleteness_payload || {}).employees_with_insufficient_evidence_count || 0)} employee(s)</h4>
                  </div>
                  <StatusChip
                    status={Number((latestMatrix.incompleteness_payload || {}).employees_with_insufficient_evidence_count || 0) > 0 ? 'action_required' : 'completed'}
                  />
                </div>
                <ul className="helper-list helper-list-compact">
                  {Object.entries(readRecord((latestMatrix.incompleteness_payload || {}).flag_counts)).slice(0, 5).map(([flag, count]) => (
                    <li key={flag}>
                      {getIncompletenessFlagLabel(flag)}: {String(count)}
                    </li>
                  ))}
                </ul>
                {uncoveredRoles.length > 0 ? (
                  <ul className="helper-list helper-list-compact">
                    {uncoveredRoles.slice(0, 3).map((item) => (
                      <li key={`${readString(item.role_name)}:${readString(item.reason)}`}>{formatUncoveredRole(item)}</li>
                    ))}
                  </ul>
                ) : null}
              </article>
            </div>
          </section>

          <section className="board-panel">
            <div className="panel-heading">
              <span className="section-tag">Insufficient evidence</span>
              <h3>Employees who still need evidence repair</h3>
              <p>Use this list to focus operator review on the people most likely to weaken matrix confidence.</p>
            </div>

            {insufficientEvidenceEmployees.length === 0 ? (
              <EmptyState
                title="No employees are currently flagged for insufficient evidence"
                description="The latest completed matrix did not return any employee-level incompleteness rows."
              />
            ) : (
              <div className="review-card-grid">
                {insufficientEvidenceEmployees.map((item) => {
                  const employeeUuid = readString(item.employee_uuid)
                  const bestFitRole = readRecord(item.best_fit_role)
                  const flags = readStringArray(item.flags)

                  return (
                    <article key={employeeUuid} className="review-card">
                      <div className="review-card-head">
                        <div>
                          <span className="summary-label">Needs evidence review</span>
                          <h4>{readString(item.full_name) || 'Employee'}</h4>
                        </div>
                        <StatusChip status="action_required" />
                      </div>

                      <div className="detail-stack">
                        <p>{readString(item.current_title) || 'Current title not captured.'}</p>
                        <p>
                          Best fit role: <strong>{readString(bestFitRole.role_name) || 'No primary role match yet'}</strong>
                        </p>
                        <p>{formatListPreview(flags.map(getIncompletenessFlagLabel), 'No employee-level flags were returned.')}</p>
                      </div>

                      <div className="form-actions">
                        <button className="secondary-button" onClick={() => setSelectedEmployeeUuid(employeeUuid)}>
                          Open employee slice
                        </button>
                      </div>
                    </article>
                  )
                })}
              </div>
            )}
          </section>

          <section className="board-panel">
            <div className="panel-heading">
              <span className="section-tag">Heatmap</span>
              <h3>Team-wide gap and confidence view</h3>
              <p>Select an employee row to inspect their slice, or select a cell to inspect the underlying evidence summary.</p>
            </div>

            {heatmapColumns.length === 0 || heatmapRows.length === 0 ? (
              <EmptyState
                title="Heatmap payload is empty"
                description="The latest matrix exists, but the heatmap slice did not return employee rows or selected skill columns."
              />
            ) : (
              <div className="source-table-shell">
                <table className="source-table matrix-heatmap-table">
                  <thead>
                    <tr>
                      <th>Employee</th>
                      {heatmapColumns.map((column) => (
                        <th key={column.column_key}>
                          <div className="matrix-heatmap-heading">
                            <strong>{column.skill_name_en || column.skill_key}</strong>
                            <span>{column.role_name || 'Role'} · target {column.target_level}</span>
                          </div>
                        </th>
                      ))}
                    </tr>
                  </thead>
                  <tbody>
                    {heatmapRows.map((row) => (
                      <tr key={row.employee_uuid}>
                        <td>
                          <button
                            className="matrix-row-button"
                            onClick={() => setSelectedEmployeeUuid(row.employee_uuid)}
                          >
                            <strong>{row.full_name}</strong>
                            <span>{row.current_title || 'Current title not captured'}</span>
                            <span>
                              {row.best_fit_role ? readString(readRecord(row.best_fit_role).role_name) || 'Matched role' : 'No role'}
                            </span>
                          </button>
                        </td>
                        {heatmapColumns.map((column) => {
                          const cell =
                            row.cells.find((item) => item.column_key === column.column_key) ??
                            {
                              column_key: column.column_key,
                              skill_key: column.skill_key,
                              gap: 0,
                              current_level: 0,
                              target_level: column.target_level,
                              confidence: 0,
                              incompleteness_flags: ['not_required'],
                            }
                          const tone = getMatrixCellTone(cell.gap, cell.confidence, cell.incompleteness_flags)

                          return (
                            <td key={`${row.employee_uuid}:${column.column_key}`}>
                              <button
                                className={`matrix-heatmap-cell ${tone}`}
                                onClick={() =>
                                  setSelectedCellContext({
                                    employeeUuid: row.employee_uuid,
                                    employeeName: row.full_name,
                                    columnKey: column.column_key,
                                    columnLabel: `${column.skill_name_en || column.skill_key} / ${column.role_name || 'Role'}`,
                                  })
                                }
                              >
                                <strong>{cell.gap.toFixed(1)}</strong>
                                <span>gap</span>
                                <span>{formatPercent(cell.confidence)}</span>
                              </button>
                            </td>
                          )
                        })}
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </section>
        </>
      )}

      <SlideOverPanel
        title={employeeSlice?.full_name || 'Employee detail'}
        open={selectedEmployeeUuid !== null}
        onClose={() => setSelectedEmployeeUuid(null)}
        wide
      >
          {employeeLoading ? (
            <LoadingState
              title="Loading employee matrix slice"
              description="Fetching the latest employee-specific matrix payload."
              compact
            />
          ) : employeeError ? (
            <ErrorState
              title="Employee slice failed to load"
              description={employeeError}
              onRetry={() => setEmployeeReloadToken((value) => value + 1)}
              compact
            />
          ) : employeeSlice ? (
            <div className="detail-stack">
              <div className="summary-grid">
                <article className="summary-card">
                  <span className="summary-label">Best fit role</span>
                  <strong>{readString((employeeSlice.best_fit_role || {}).role_name) || 'No primary role match'}</strong>
                  <p>{humanizeToken(employeeSlice.role_match_status || 'unknown')}</p>
                </article>
                <article className="summary-card">
                  <span className="summary-label">Gap score</span>
                  <strong>{employeeSlice.total_gap_score.toFixed(1)}</strong>
                  <p>{employeeSlice.critical_gap_count} critical gap(s)</p>
                </article>
                <article className="summary-card">
                  <span className="summary-label">Average confidence</span>
                  <strong>{formatPercent(employeeSlice.average_confidence)}</strong>
                  <p>{employeeSlice.insufficient_evidence_count} insufficient-evidence skill row(s)</p>
                </article>
                <article className="summary-card">
                  <span className="summary-label">Flags</span>
                  <strong>{employeeSlice.insufficient_evidence_flags.length + employeeSlice.advisory_flags.length}</strong>
                  <p>
                    {formatListPreview(
                      [
                        ...employeeSlice.insufficient_evidence_flags.map(getIncompletenessFlagLabel),
                        ...employeeSlice.advisory_flags.map(getAdvisoryFlagLabel),
                      ],
                      'No employee-level flags.',
                    )}
                  </p>
                </article>
              </div>

              {employeeSliceNeedsAction ? (
                <section className="inline-banner inline-banner-warn">
                  <strong>Action required</strong>
                  {employeeSliceActionReasons.length > 0 ? (
                    <ul className="inline-detail-list">
                      {employeeSliceActionReasons.map((reason) => (
                        <li key={reason}>{reason}</li>
                      ))}
                    </ul>
                  ) : null}
                  {employeeSliceRecommendedSteps.length > 0 ? (
                    <ul className="inline-detail-list">
                      {employeeSliceRecommendedSteps.map((step) => (
                        <li key={step}>{step}</li>
                      ))}
                    </ul>
                  ) : null}
                  <div className="form-actions">
                    <AppLink className="secondary-button link-button" to={buildScopedWorkspacePath('parse')}>
                      Review parse & evidence
                    </AppLink>
                    {hasRoleCoverageIssue(employeeSlice) ? (
                      <AppLink className="secondary-button link-button" to={buildScopedWorkspacePath('blueprint')}>
                        Review blueprint roles
                      </AppLink>
                    ) : null}
                  </div>
                </section>
              ) : null}

              <div className="info-grid">
                <article className="info-card">
                  <h4>Adjacent roles</h4>
                  <p>
                    {employeeSlice.adjacent_roles.length > 0
                      ? employeeSlice.adjacent_roles
                          .map((item) => `${readString(item.role_name)} (${formatFitScore(Number(item.fit_score || 0))})`)
                          .join(', ')
                      : 'No adjacent roles were returned.'}
                  </p>
                </article>
                <article className="info-card">
                  <h4>Top gaps</h4>
                  <p>
                    {employeeSlice.top_gaps.length > 0
                      ? employeeSlice.top_gaps
                          .slice(0, 3)
                          .map((item) => `${item.skill_name_en || item.skill_key} (${item.gap.toFixed(1)})`)
                          .join(', ')
                      : 'No top-gap rows were returned.'}
                  </p>
                </article>
              </div>

              <div className="source-table-shell">
                <table className="source-table">
                  <thead>
                    <tr>
                      <th>Skill</th>
                      <th>Current</th>
                      <th>Target</th>
                      <th>Gap</th>
                      <th>Confidence</th>
                      <th>Flags</th>
                    </tr>
                  </thead>
                  <tbody>
                    {employeeSlice.skills.map((skill) => (
                      <tr key={`${employeeSlice.employee_uuid}:${skill.skill_key}`}>
                        <td>
                          <div className="source-primary-cell">
                            <strong>{skill.skill_name_en || skill.skill_key}</strong>
                            <p>{skill.role_name || 'Role not captured'}</p>
                          </div>
                        </td>
                        <td>{skill.current_level.toFixed(1)}</td>
                        <td>{skill.target_level}</td>
                        <td>{skill.gap.toFixed(1)}</td>
                        <td>{formatPercent(skill.confidence)}</td>
                        <td>
                          <div className="source-secondary-cell">
                            <p>
                              {formatListPreview(
                                [
                                  ...skill.incompleteness_flags.map(getIncompletenessFlagLabel),
                                  ...skill.advisory_flags.map(getAdvisoryFlagLabel),
                                ],
                                'No flags',
                              )}
                            </p>
                          </div>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          ) : null}
      </SlideOverPanel>

      <SlideOverPanel
        title={selectedCellContext ? selectedCellContext.columnLabel : 'Cell detail'}
        open={selectedCellContext !== null}
        onClose={() => setSelectedCellContext(null)}
        wide
      >
          {cellsLoading ? (
            <LoadingState
              title="Loading detailed matrix cells"
              description="Fetching the latest detailed cell payload."
              compact
            />
          ) : cellsError ? (
            <ErrorState
              title="Cell detail failed to load"
              description={cellsError}
              onRetry={() => void loadLatestCells()}
              compact
            />
          ) : selectedCellDetail ? (
            <div className="detail-stack">
              <div className="summary-grid">
                <article className="summary-card">
                  <span className="summary-label">Employee</span>
                  <strong>{selectedCellDetail.employee_name}</strong>
                  <p>{selectedCellDetail.current_title || 'Current title not captured'}</p>
                </article>
                <article className="summary-card">
                  <span className="summary-label">Current vs target</span>
                  <strong>
                    {selectedCellDetail.current_level.toFixed(1)} / {selectedCellDetail.target_level}
                  </strong>
                  <p>Gap {selectedCellDetail.gap.toFixed(1)}</p>
                </article>
                <article className="summary-card">
                  <span className="summary-label">Confidence</span>
                  <strong>{formatPercent(selectedCellDetail.confidence)}</strong>
                  <p>Priority {selectedCellDetail.priority}</p>
                </article>
                <article className="summary-card">
                  <span className="summary-label">Flags</span>
                  <strong>{selectedCellDetail.incompleteness_flags.length + selectedCellDetail.advisory_flags.length}</strong>
                  <p>
                    {formatListPreview(
                      [
                        ...selectedCellDetail.incompleteness_flags.map(getIncompletenessFlagLabel),
                        ...selectedCellDetail.advisory_flags.map(getAdvisoryFlagLabel),
                      ],
                      'No cell-level flags.',
                    )}
                  </p>
                </article>
              </div>

              <div className="info-grid">
                <article className="info-card">
                  <h4>Explanation summary</h4>
                  <p>{selectedCellDetail.explanation_summary || 'No explanation summary was returned for this cell.'}</p>
                </article>
                <article className="info-card">
                  <h4>Role fit</h4>
                  <p>
                    {selectedCellDetail.role_name || 'Role not captured'} · fit {formatFitScore(selectedCellDetail.role_fit_score)}
                  </p>
                </article>
              </div>

              <div className="review-card-grid">
                <article className="review-card">
                  <div className="review-card-head">
                    <div>
                      <span className="summary-label">Evidence source mix</span>
                      <h4>{selectedCellDetail.evidence_source_mix.length} source signal(s)</h4>
                    </div>
                    <StatusChip status={selectedCellDetail.evidence_source_mix.length > 0 ? 'ready' : 'blocked'} />
                  </div>
                  {selectedCellDetail.evidence_source_mix.length > 0 ? (
                    <ul className="helper-list helper-list-compact">
                      {selectedCellDetail.evidence_source_mix.map((item, index) => (
                        <li key={`${selectedCellDetail.cell_key}:mix:${index}`}>{formatEvidenceSourceMixItem(item)}</li>
                      ))}
                    </ul>
                  ) : (
                    <p>No evidence source mix was returned.</p>
                  )}
                </article>

                <article className="review-card">
                  <div className="review-card-head">
                    <div>
                      <span className="summary-label">Evidence rows</span>
                      <h4>{selectedCellDetail.evidence_rows.length} row(s)</h4>
                    </div>
                    <StatusChip status={selectedCellDetail.evidence_rows.length > 0 ? 'ready' : 'blocked'} />
                  </div>
                  {selectedCellDetail.evidence_rows.length > 0 ? (
                    <ul className="helper-list helper-list-compact">
                      {selectedCellDetail.evidence_rows.map((item, index) => (
                        <li key={`${selectedCellDetail.cell_key}:row:${index}`}>{formatEvidenceRowItem(item)}</li>
                      ))}
                    </ul>
                  ) : (
                    <p>No direct evidence rows were returned.</p>
                  )}
                </article>

                <article className="review-card">
                  <div className="review-card-head">
                    <div>
                      <span className="summary-label">Provenance snippets</span>
                      <h4>{selectedCellDetail.provenance_snippets.length} snippet(s)</h4>
                    </div>
                    <StatusChip status={selectedCellDetail.provenance_snippets.length > 0 ? 'ready' : 'not_started'} />
                  </div>
                  {selectedCellDetail.provenance_snippets.length > 0 ? (
                    <ul className="helper-list helper-list-compact">
                      {selectedCellDetail.provenance_snippets.map((item, index) => (
                        <li key={`${selectedCellDetail.cell_key}:prov:${index}`}>{formatProvenanceItem(item)}</li>
                      ))}
                    </ul>
                  ) : (
                    <p>No provenance snippets were returned.</p>
                  )}
                </article>
              </div>
            </div>
          ) : (
            <EmptyState
              title="Cell detail is unavailable"
              description="The selected heatmap cell could not be matched to a detailed matrix cell payload."
            />
          )}
      </SlideOverPanel>
    </div>
  )
}
