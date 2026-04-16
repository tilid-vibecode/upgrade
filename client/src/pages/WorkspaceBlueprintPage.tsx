import { useCallback, useEffect, useMemo, useRef, useState, type FormEvent } from 'react'

import { AppLink } from '../app/navigation'
import { useWorkspaceShell } from '../app/WorkspaceLayout'
import {
  BLUEPRINT_DETAIL_TABS,
  canApproveBlueprint,
  canPublishBlueprint,
  canRefreshBlueprintFromClarifications,
  canReviewBlueprint,
  canStartBlueprintRevision,
  getBlueprintRunBadges,
  getBlueprintRunSummaryLabel,
  getClarificationOpenCount,
  getRoleCandidateInitiativeCount,
  getRoleCandidateKey,
  getRoleCandidateName,
  getRoleCandidateSkillCount,
  type BlueprintDetailTabKey,
} from '../shared/blueprintReview'
import { CollapsibleHero } from '../shared/ui/CollapsibleHero'
import { getApiErrorMessage, getApiErrorMessages, isApiError } from '../shared/api'
import { formatDateTime, formatPercent, formatShortId } from '../shared/formatters'
import {
  generatePrototypeBlueprint,
  getPrototypeBlueprintRoleDetail,
  getPrototypeBlueprintRun,
  getPrototypeCurrentBlueprintRun,
  getPrototypeLatestBlueprintRun,
  getPrototypeLatestRoadmapAnalysis,
  getPrototypeLatestRoleLibrarySnapshot,
  getPrototypeRoadmapAnalysisStatus,
  listPrototypeBlueprintRuns,
  publishPrototypeBlueprintRun,
  refreshPrototypeBlueprintFromClarifications,
  reviewPrototypeBlueprintRun,
  runPrototypeRoadmapAnalysis,
  startPrototypeBlueprintRevision,
  syncPrototypeRoleLibrary,
  approvePrototypeBlueprintRun,
  type PrototypeBlueprintRoleDetailResponse,
  type PrototypeRoadmapAnalysisRun,
  type PrototypeRoadmapAnalysisStatusResponse,
  type PrototypeRoleLibrarySnapshot,
  type PrototypeSkillBlueprintRun,
} from '../shared/prototypeApi'
import { humanizeToken } from '../shared/workflow'
import { requestGlobalConfirmation } from '../shared/ui/ConfirmationDialog'
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

function readString(value: unknown) {
  return typeof value === 'string' ? value : ''
}

function readStringArray(value: unknown) {
  return Array.isArray(value) ? value.filter((item): item is string => typeof item === 'string' && item.trim().length > 0) : []
}

function readNumber(value: unknown) {
  return typeof value === 'number' && Number.isFinite(value) ? value : null
}

function readFirstNumber(record: Record<string, unknown>, keys: string[]) {
  for (const key of keys) {
    const value = readNumber(record[key])
    if (value !== null) {
      return value
    }
  }
  return null
}

function readFirstString(record: Record<string, unknown>, keys: string[]) {
  for (const key of keys) {
    const value = readString(record[key])
    if (value) {
      return value
    }
  }
  return ''
}

function readBoolean(value: unknown) {
  return typeof value === 'boolean' ? value : false
}

function readRecord(value: unknown) {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : {}
}

function normalizeLineList(value: string) {
  return value
    .split('\n')
    .map((item) => item.trim())
    .filter(Boolean)
}

function stringifyJson(value: unknown) {
  try {
    return JSON.stringify(value, null, 2)
  } catch {
    return String(value)
  }
}

function formatCount(value: number | null | undefined) {
  return typeof value === 'number' && Number.isFinite(value) ? String(value) : '0'
}

function formatConfidence(value: number | null) {
  if (value === null) {
    return 'Not available'
  }

  return formatPercent(value <= 1 ? value : value / 100)
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

function JsonBlock({ value, compact = false }: { value: unknown; compact?: boolean }) {
  return <pre className={compact ? 'code-block code-block-compact' : 'code-block'}>{stringifyJson(value)}</pre>
}

function DetailMetaCard({
  label,
  value,
  supporting,
}: {
  label: string
  value: string
  supporting?: string
}) {
  return (
    <article className="info-card">
      <span className="summary-label">{label}</span>
      <strong>{value}</strong>
      {supporting ? <p>{supporting}</p> : null}
    </article>
  )
}

function requestOptional<T>(request: Promise<T>) {
  return request.catch((error: unknown) => {
    if (isApiError(error) && error.status === 404) {
      return null
    }
    throw error
  })
}

export default function WorkspaceBlueprintPage() {
  const {
    workspace,
    workflow,
    activePlanningContext,
    planningContextOptions,
    refreshShell,
    buildScopedWorkspacePath,
  } = useWorkspaceShell()
  const blueprintStage = workflow.stages.find((stage) => stage.key === 'blueprint') ?? null
  const clarificationStage = workflow.stages.find((stage) => stage.key === 'clarifications') ?? null

  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [busyAction, setBusyAction] = useState<string | null>(null)
  const [banner, setBanner] = useState<BannerState | null>(null)

  const [roleLibrary, setRoleLibrary] = useState<PrototypeRoleLibrarySnapshot | null>(null)
  const [roadmapStatus, setRoadmapStatus] = useState<PrototypeRoadmapAnalysisStatusResponse | null>(null)
  const [roadmapRun, setRoadmapRun] = useState<PrototypeRoadmapAnalysisRun | null>(null)
  const [runs, setRuns] = useState<PrototypeSkillBlueprintRun[]>([])
  const [effectiveRun, setEffectiveRun] = useState<PrototypeSkillBlueprintRun | null>(null)
  const [latestRun, setLatestRun] = useState<PrototypeSkillBlueprintRun | null>(null)

  const [selectedRunUuid, setSelectedRunUuid] = useState<string | null>(null)
  const [selectedRunDetail, setSelectedRunDetail] = useState<PrototypeSkillBlueprintRun | null>(null)
  const [selectedRunLoading, setSelectedRunLoading] = useState(false)
  const [selectedRunError, setSelectedRunError] = useState<string | null>(null)
  const [selectedTab, setSelectedTab] = useState<BlueprintDetailTabKey>('overview')

  const [selectedRoleKey, setSelectedRoleKey] = useState<string | null>(null)
  const [selectedRoleDetail, setSelectedRoleDetail] = useState<PrototypeBlueprintRoleDetailResponse | null>(null)
  const [selectedRoleLoading, setSelectedRoleLoading] = useState(false)
  const [selectedRoleError, setSelectedRoleError] = useState<string | null>(null)

  const [syncBaseUrlsDraft, setSyncBaseUrlsDraft] = useState('')
  const [syncMaxPagesDraft, setSyncMaxPagesDraft] = useState('40')
  const [operatorNameDraft, setOperatorNameDraft] = useState('')
  const [operatorNoteDraft, setOperatorNoteDraft] = useState('')
  const isArchivedContext = activePlanningContext?.status === 'archived'
  const pageLoadRequestIdRef = useRef(0)

  const loadBlueprintPage = useCallback(
    async (preferredRunUuid?: string | null) => {
      const requestId = pageLoadRequestIdRef.current + 1
      pageLoadRequestIdRef.current = requestId
      setLoading(true)
      setError(null)

      try {
        const [roleLibraryResponse, roadmapStatusResponse, roadmapRunResponse, runsResponse, effectiveResponse, latestResponse] = await Promise.all([
          requestOptional(getPrototypeLatestRoleLibrarySnapshot(workspace.slug)),
          getPrototypeRoadmapAnalysisStatus(workspace.slug, planningContextOptions),
          requestOptional(getPrototypeLatestRoadmapAnalysis(workspace.slug, planningContextOptions)),
          listPrototypeBlueprintRuns(workspace.slug, planningContextOptions),
          requestOptional(getPrototypeCurrentBlueprintRun(workspace.slug, planningContextOptions)),
          requestOptional(getPrototypeLatestBlueprintRun(workspace.slug, planningContextOptions)),
        ])

        if (pageLoadRequestIdRef.current !== requestId) {
          return
        }

        const nextRuns = runsResponse.runs
        const nextEffectiveRun = effectiveResponse
        const nextLatestRun = latestResponse
        const availableRunUuids = new Set<string>(
          [
            ...nextRuns.map((run) => run.uuid),
            nextEffectiveRun?.uuid ?? '',
            nextLatestRun?.uuid ?? '',
          ].filter(Boolean),
        )

        setRoleLibrary(roleLibraryResponse)
        setRoadmapStatus(roadmapStatusResponse)
        setRoadmapRun(roadmapRunResponse)
        setRuns(nextRuns)
        setEffectiveRun(nextEffectiveRun)
        setLatestRun(nextLatestRun)
        setSelectedRunUuid((currentValue) => {
          if (preferredRunUuid && availableRunUuids.has(preferredRunUuid)) {
            return preferredRunUuid
          }
          if (currentValue && availableRunUuids.has(currentValue)) {
            return currentValue
          }
          return nextEffectiveRun?.uuid || nextLatestRun?.uuid || nextRuns[0]?.uuid || null
        })
      } catch (loadError) {
        if (pageLoadRequestIdRef.current === requestId) {
          setError(getApiErrorMessage(loadError, 'Failed to load blueprint review data.'))
        }
      } finally {
        if (pageLoadRequestIdRef.current === requestId) {
          setLoading(false)
        }
      }
    },
    [planningContextOptions, workspace.slug],
  )

  useEffect(() => {
    loadBlueprintPage().catch(() => undefined)
  }, [loadBlueprintPage])

  useEffect(() => {
    if (!selectedRunUuid) {
      setSelectedRunDetail(null)
      setSelectedRunError(null)
      return
    }

    let cancelled = false
    const selectedRunId = selectedRunUuid
    const fallbackRun =
      runs.find((run) => run.uuid === selectedRunId) ??
      (effectiveRun?.uuid === selectedRunId ? effectiveRun : null) ??
      (latestRun?.uuid === selectedRunId ? latestRun : null)

    setSelectedRunDetail(fallbackRun ?? null)
    setSelectedRunError(null)
    setSelectedRunLoading(true)

    async function loadSelectedRunDetail() {
      try {
        const detail = await getPrototypeBlueprintRun(workspace.slug, selectedRunId)
        if (cancelled) {
          return
        }
        setSelectedRunDetail(detail)
      } catch (detailError) {
        if (cancelled) {
          return
        }
        setSelectedRunError(getApiErrorMessage(detailError, 'Failed to load the selected blueprint run.'))
      } finally {
        if (!cancelled) {
          setSelectedRunLoading(false)
        }
      }
    }

    loadSelectedRunDetail().catch(() => undefined)

    return () => {
      cancelled = true
    }
  }, [workspace.slug, selectedRunUuid, runs, effectiveRun, latestRun])

  useEffect(() => {
    setSelectedRoleKey(null)
    setSelectedRoleDetail(null)
    setSelectedRoleError(null)
  }, [selectedRunUuid])

  useEffect(() => {
    if (!selectedRunDetail || !selectedRoleKey) {
      setSelectedRoleDetail(null)
      setSelectedRoleError(null)
      return
    }

    let cancelled = false
    const roleKey = selectedRoleKey
    const selectedRunId = selectedRunDetail.uuid
    setSelectedRoleLoading(true)
    setSelectedRoleError(null)

    async function loadRoleDetail() {
      try {
        const detail = await getPrototypeBlueprintRoleDetail(workspace.slug, selectedRunId, roleKey)
        if (cancelled) {
          return
        }
        setSelectedRoleDetail(detail)
      } catch (roleError) {
        if (cancelled) {
          return
        }
        setSelectedRoleError(getApiErrorMessage(roleError, 'Failed to load role detail.'))
      } finally {
        if (!cancelled) {
          setSelectedRoleLoading(false)
        }
      }
    }

    loadRoleDetail().catch(() => undefined)

    return () => {
      cancelled = true
    }
  }, [workspace.slug, selectedRunDetail, selectedRoleKey])

  const selectedRun = selectedRunDetail
  const effectiveAndLatestDiffer =
    effectiveRun !== null && latestRun !== null && effectiveRun.uuid !== latestRun.uuid
  const selectedRunRoleCandidates = selectedRun?.role_candidates ?? []
  const selectedRunRequiredSkills = selectedRun?.required_skill_set ?? []
  const selectedRunAutomationCandidates = selectedRun?.automation_candidates ?? []
  const selectedRunOccupationMap = selectedRun?.occupation_map ?? []
  const selectedRunOpenClarificationCount = getClarificationOpenCount(selectedRun)
  const latestRunOpenClarificationCount = getClarificationOpenCount(latestRun)
  const selectedRunOpenClarifications = (selectedRun?.clarification_questions ?? []).filter(
    (item) => readString(item.status) === 'open' || readString(item.status) === 'answered' || readString(item.status) === 'rejected',
  )
  const roleLibrarySummary = readRecord(roleLibrary?.summary)
  const roleLibraryCounts = useMemo(
    () =>
      Object.entries(roleLibrary?.canonical_family_counts ?? {}).sort((left, right) => {
        return Number(right[1]) - Number(left[1])
      }),
    [roleLibrary],
  )

  const selectedRunReviewSummary = readRecord(selectedRun?.review_summary)
  const selectedRunClarificationSummary = readRecord(selectedRunReviewSummary.clarification_summary)
  const employeeMatches = selectedRun?.employee_role_matches ?? []
  const changeLog = selectedRun?.change_log ?? []
  const heroRun = latestRun ?? effectiveRun
  const latestRunExistsWithoutEffective = latestRun !== null && effectiveRun === null
  const roadmapSummary = roadmapStatus?.latest_run ?? roadmapRun
  const hasRoadmapAnalysis = Boolean(roadmapStatus?.has_analysis || roadmapSummary)
  const roadmapBusyKey = 'roadmap-analysis:run'
  const roadmapForceBusyKey = 'roadmap-analysis:force'
  const blueprintNeedsRoadmapAnalysis = (blueprintStage?.blockers ?? []).some((blocker) =>
    blocker.toLowerCase().includes('roadmap analysis'),
  )

  function requireOperatorName(actionLabel: string) {
    const normalized = operatorNameDraft.trim()
    if (normalized) {
      return normalized
    }

    setBanner({
      tone: 'warn',
      title: `${actionLabel} needs an operator name.`,
      messages: ['Enter the operator name in the release controls before running this action.'],
    })
    return null
  }

  async function runMutation(
    actionKey: string,
    request: () => Promise<PrototypeSkillBlueprintRun>,
    successBuilder: (run: PrototypeSkillBlueprintRun) => BannerState,
  ) {
    setBusyAction(actionKey)
    setBanner(null)

    try {
      const updatedRun = await request()
      await loadBlueprintPage(updatedRun.uuid)
      void refreshShell()
      setBanner(successBuilder(updatedRun))
    } catch (actionError) {
      setBanner({
        tone: 'error',
        title: 'Blueprint action failed.',
        messages: getApiErrorMessages(actionError).length > 0
          ? getApiErrorMessages(actionError)
          : ['The backend rejected the action.'],
      })
    } finally {
      setBusyAction(null)
    }
  }

  async function handleGenerateBlueprint() {
    setBusyAction('generate')
    setBanner(null)

    try {
      const generatedRun = await generatePrototypeBlueprint(workspace.slug, {
        role_library_snapshot_uuid: roleLibrary?.uuid || undefined,
      }, planningContextOptions)
      await loadBlueprintPage(generatedRun.uuid)
      void refreshShell()
      setBanner({
        tone: generatedRun.status === 'needs_clarification' ? 'warn' : 'success',
        title: `Generated ${generatedRun.title || 'a new blueprint run'}.`,
        messages: [
          roleLibrary ? `Generation used role-library snapshot ${formatShortId(roleLibrary.uuid)}.` : 'Generation ran without an explicit role-library snapshot selection.',
          generatedRun.status === 'needs_clarification'
            ? 'Open clarifications were created, so review the run and move into the clarification route next.'
            : `The run is now in ${humanizeToken(generatedRun.status)} status.`,
        ],
      })
    } catch (generateError) {
      setBanner({
        tone: 'error',
        title: 'Blueprint generation failed.',
        messages: getApiErrorMessages(generateError).length > 0
          ? getApiErrorMessages(generateError)
          : ['The backend did not accept the blueprint generation request.'],
      })
    } finally {
      setBusyAction(null)
    }
  }

  async function handleRunRoadmapAnalysis(forceRebuild = false) {
    const actionKey = forceRebuild ? roadmapForceBusyKey : roadmapBusyKey
    setBusyAction(actionKey)
    setBanner(null)

    try {
      const response = await runPrototypeRoadmapAnalysis(
        workspace.slug,
        forceRebuild ? { force_rebuild: true } : {},
        planningContextOptions,
      )
      await loadBlueprintPage(selectedRunUuid)
      void refreshShell()
      setBanner({
        tone: response.status === 'completed' ? 'success' : response.status === 'failed' ? 'error' : 'info',
        title: response.status === 'failed'
          ? 'Roadmap analysis failed for this scope.'
          : forceRebuild
            ? 'Roadmap analysis was rebuilt for this scope.'
            : hasRoadmapAnalysis
              ? 'Roadmap analysis was refreshed for this scope.'
              : 'Roadmap analysis is now available for this scope.',
        messages: [
          response.message,
          activePlanningContext
            ? `Scope: ${activePlanningContext.name}.`
            : 'Scope: legacy workspace.',
          'Inputs come from parsed roadmap and strategy sources that are active and included in roadmap analysis for this scope.',
        ],
      })
    } catch (analysisError) {
      setBanner({
        tone: 'error',
        title: 'Roadmap analysis failed.',
        messages: getApiErrorMessages(analysisError).length > 0
          ? getApiErrorMessages(analysisError)
          : ['The roadmap-analysis stage is not ready yet for this scope.'],
      })
    } finally {
      setBusyAction(null)
    }
  }

  async function handleSyncRoleLibrary(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    setBusyAction('sync-role-library')
    setBanner(null)

    try {
      const maxPages = Number.parseInt(syncMaxPagesDraft, 10)
      const snapshot = await syncPrototypeRoleLibrary(workspace.slug, {
        base_urls: normalizeLineList(syncBaseUrlsDraft),
        max_pages: Number.isFinite(maxPages) && maxPages > 0 ? maxPages : 40,
      })
      setRoleLibrary(snapshot)
      setBanner({
        tone: snapshot.status === 'failed' ? 'warn' : 'success',
        title: `Role-library sync finished with ${humanizeToken(snapshot.status)} status.`,
        messages: [
          `${snapshot.entry_count} role entries and ${snapshot.normalized_skill_count} normalized skills are visible in the latest snapshot.`,
          snapshot.error_message || 'The latest snapshot is now available to use for the next blueprint generation run.',
        ].filter(Boolean),
      })
    } catch (syncError) {
      setBanner({
        tone: 'error',
        title: 'Role-library sync failed.',
        messages: getApiErrorMessages(syncError).length > 0
          ? getApiErrorMessages(syncError)
          : ['The role-library snapshot could not be refreshed.'],
      })
    } finally {
      setBusyAction(null)
    }
  }

  const handleReviewBlueprint = async () => {
    if (!selectedRun) {
      return
    }

    const operatorName = requireOperatorName('Review')
    if (!operatorName) {
      return
    }

    await runMutation(
      'review',
      () =>
        reviewPrototypeBlueprintRun(workspace.slug, selectedRun.uuid, {
          reviewer_name: operatorName,
          review_notes: normalizeOptionalString(operatorNoteDraft),
        }),
      (updatedRun) => ({
        tone: updatedRun.status === 'needs_clarification' ? 'warn' : 'success',
        title: `Reviewed ${updatedRun.title || 'the selected blueprint run'}.`,
        messages: [
          updatedRun.status === 'needs_clarification'
            ? 'Clarifications are still open, so the run remains in clarification workflow.'
            : `The run is now ${humanizeToken(updatedRun.status)} and ready for the next release step.`,
        ],
      }),
    )
  }

  const handleApproveBlueprint = async () => {
    if (!selectedRun) {
      return
    }

    const operatorName = requireOperatorName('Approval')
    if (!operatorName) {
      return
    }

    await runMutation(
      'approve',
      () =>
        approvePrototypeBlueprintRun(workspace.slug, selectedRun.uuid, {
          approver_name: operatorName,
          approval_notes: normalizeOptionalString(operatorNoteDraft),
        }),
      (updatedRun) => ({
        tone: 'success',
        title: `Approved ${updatedRun.title || 'the selected blueprint run'}.`,
        messages: ['The run is now approved and can be published for downstream stages when you are ready.'],
      }),
    )
  }

  const handlePublishBlueprint = async () => {
    if (!selectedRun) {
      return
    }

    const operatorName = requireOperatorName('Publication')
    if (!operatorName) {
      return
    }

    if (!(await requestGlobalConfirmation({
      title: 'Publish this blueprint?',
      description: 'Publishing will make this run the effective downstream blueprint for assessments and later stages.',
      confirmLabel: 'Publish blueprint',
      cancelLabel: 'Keep reviewing',
      tone: 'warn',
    }))) {
      return
    }

    await runMutation(
      'publish',
      () =>
        publishPrototypeBlueprintRun(workspace.slug, selectedRun.uuid, {
          publisher_name: operatorName,
          publish_notes: normalizeOptionalString(operatorNoteDraft),
        }),
      (updatedRun) => ({
        tone: 'success',
        title: `Published ${updatedRun.title || 'the selected blueprint run'}.`,
        messages: ['This run is now the effective blueprint for assessments and later downstream stages.'],
      }),
    )
  }

  const handleRefreshFromClarifications = async () => {
    if (!selectedRun) {
      return
    }

    const operatorName = requireOperatorName('Clarification refresh')
    if (!operatorName) {
      return
    }

    await runMutation(
      'refresh-from-clarifications',
      () =>
        refreshPrototypeBlueprintFromClarifications(workspace.slug, selectedRun.uuid, {
          operator_name: operatorName,
          refresh_note: normalizeOptionalString(operatorNoteDraft),
          skip_employee_matching: false,
        }),
      (updatedRun) => ({
        tone: 'success',
        title: `Refreshed ${updatedRun.title || 'the selected blueprint run'} from clarifications.`,
        messages: [
          'The mutable run was rebuilt from the latest answered clarification set.',
          'Answered items can still remain open on the old run. Review the refreshed detail and confirm whether the new run now has fewer or zero open clarifications.',
        ],
      }),
    )
  }

  const handleStartRevision = async () => {
    if (!selectedRun) {
      return
    }

    const operatorName = requireOperatorName('Revision start')
    if (!operatorName) {
      return
    }

    if (!(await requestGlobalConfirmation({
      title: 'Start a new blueprint revision?',
      description: 'A new mutable revision will be created while the approved or published baseline stays unchanged.',
      confirmLabel: 'Start revision',
      cancelLabel: 'Stay on this run',
      tone: 'warn',
    }))) {
      return
    }

    await runMutation(
      'start-revision',
      () =>
        startPrototypeBlueprintRevision(workspace.slug, selectedRun.uuid, {
          operator_name: operatorName,
          revision_reason: normalizeOptionalString(operatorNoteDraft),
          skip_employee_matching: true,
        }),
      (updatedRun) => ({
        tone: 'success',
        title: `Started revision ${updatedRun.title || 'for the selected blueprint'}.`,
        messages: ['A new mutable draft is now available. The published or approved baseline remains unchanged.'],
      }),
    )
  }

  if (loading && runs.length === 0 && effectiveRun === null && latestRun === null) {
    return (
      <LoadingState
        title="Loading blueprint review"
        description="Fetching role-library state, blueprint runs, and the current effective blueprint."
      />
    )
  }

  if (error && runs.length === 0 && effectiveRun === null && latestRun === null) {
    return (
      <ErrorState
        title="Blueprint review failed to load"
        description={error}
        onRetry={() => loadBlueprintPage().catch(() => undefined)}
      />
    )
  }

  return (
    <div className="page-stack">
      <CollapsibleHero
        tag="Stage 06"
        title="Blueprint review and release"
        statusSlot={<StatusChip status={heroRun?.status || blueprintStage?.status || 'not_started'} />}
      >
        <div className="hero-copy">
          <p>
            {latestRun
              ? latestRun.is_published
                ? 'The latest blueprint run is already published and effective downstream.'
                : latestRun.status === 'needs_clarification'
                ? `A working blueprint run exists, but ${latestRunOpenClarificationCount} clarification item(s) still need operator decisions before approval can unlock.`
                : latestRun.status === 'reviewed'
                  ? 'The latest blueprint run is reviewed and waiting for approval.'
                  : latestRun.status === 'approved'
                    ? 'The latest blueprint run is approved and ready to publish downstream.'
                    : getBlueprintRunSummaryLabel(latestRun)
              : blueprintStage?.recommended_action ||
                'Generate blueprint runs, inspect effective versus latest state, and release a downstream-safe version.'}
          </p>
          <div className="hero-actions">
            {!hasRoadmapAnalysis ? (
              <button
                className="primary-button"
                onClick={() => handleRunRoadmapAnalysis().catch(() => undefined)}
                disabled={busyAction !== null || isArchivedContext}
              >
                {busyAction === roadmapBusyKey ? 'Running...' : 'Run roadmap analysis'}
              </button>
            ) : null}
            <button
              className={!hasRoadmapAnalysis ? 'secondary-button' : 'primary-button'}
              onClick={() => handleGenerateBlueprint().catch(() => undefined)}
              disabled={busyAction !== null || isArchivedContext}
            >
              {busyAction === 'generate' ? 'Generating...' : 'Generate blueprint'}
            </button>
            <AppLink className="secondary-button link-button" to={buildScopedWorkspacePath('clarifications')}>
              Open clarifications
            </AppLink>
            <AppLink className="secondary-button link-button" to={buildScopedWorkspacePath('parse')}>
              Back to parse review
            </AppLink>
          </div>
        </div>

        <div className="blueprint-hero-rail">
          <article className="route-badge">
            <span className="summary-label">Effective blueprint</span>
            <strong>{formatShortId(effectiveRun?.uuid)}</strong>
            <StatusChip status={effectiveRun?.status || 'not_started'} />
          </article>
          <article className="route-badge">
            <span className="summary-label">Latest run</span>
            <strong>{formatShortId(latestRun?.uuid)}</strong>
            <StatusChip status={latestRun?.status || 'not_started'} />
          </article>
        </div>
      </CollapsibleHero>

      {activePlanningContext ? (
        <section className="inline-banner inline-banner-info">
          <strong>Scoped blueprint flow</strong>
          <span>
            Blueprint generation, roadmap lineage, and clarifications are currently scoped to {activePlanningContext.name}.
          </span>
        </section>
      ) : null}

      {isArchivedContext ? (
        <section className="inline-banner inline-banner-warn">
          <strong>Archived contexts are read-only for blueprint progression.</strong>
          <span>Review data remains visible, but generation and publication actions are disabled while this context stays archived.</span>
        </section>
      ) : null}

      {error ? (
        <ErrorState
          title="Latest blueprint refresh failed"
          description={`${error} Showing the most recent successful stage data.`}
          onRetry={() => loadBlueprintPage(selectedRunUuid).catch(() => undefined)}
          compact
        />
      ) : null}

      {renderBanner(banner)}

      {effectiveAndLatestDiffer ? (
        <section className="inline-banner inline-banner-warn">
          <strong>The latest run is not the current effective blueprint.</strong>
          <ul className="inline-detail-list">
            <li>
              Effective downstream run: {formatShortId(effectiveRun?.uuid)} ({humanizeToken(effectiveRun?.status || 'unknown')})
            </li>
            <li>
              Latest working run: {formatShortId(latestRun?.uuid)} ({humanizeToken(latestRun?.status || 'unknown')})
            </li>
            <li>Review the latest run carefully before approval or publication so stage 07 does not consume an unintended draft.</li>
          </ul>
        </section>
      ) : null}

      {latestRunExistsWithoutEffective ? (
        <section className="inline-banner inline-banner-warn">
          <strong>A draft blueprint exists, but nothing effective is driving downstream stages yet.</strong>
          <ul className="inline-detail-list">
            <li>Latest run: {formatShortId(latestRun?.uuid)} ({humanizeToken(latestRun?.status || 'unknown')}).</li>
            <li>There is no current review-ready or published blueprint yet, so `/blueprint/current` correctly returns empty.</li>
            <li>Resolve clarifications, refresh if needed, then review, approve, and publish this run to make it effective.</li>
          </ul>
        </section>
      ) : null}

      {blueprintNeedsRoadmapAnalysis && !hasRoadmapAnalysis ? (
        <section className="inline-banner inline-banner-info">
          <strong>Roadmap analysis is the next required action for this scope.</strong>
          <ul className="inline-detail-list">
            <li>Run roadmap analysis before generating the next blueprint.</li>
            <li>It uses parsed roadmap and strategy sources that are active and included for this planning context.</li>
            <li>Use `Manage scoped sources` if the right roadmap or strategy documents are not currently in scope.</li>
          </ul>
          <div className="form-actions">
            <button
              className="primary-button"
              type="button"
              onClick={() => handleRunRoadmapAnalysis().catch(() => undefined)}
              disabled={busyAction !== null || isArchivedContext}
            >
              {busyAction === roadmapBusyKey ? 'Running...' : 'Run roadmap analysis'}
            </button>
            <AppLink className="secondary-button link-button" to={buildScopedWorkspacePath('contexts')}>
              Manage scoped sources
            </AppLink>
          </div>
        </section>
      ) : null}

      <section className="blueprint-stage-grid">
        <article className="board-panel">
          <div className="panel-heading">
            <span className="section-tag">Role library</span>
            <h3>Latest snapshot and re-sync controls</h3>
            <p>Stage 06 keeps this surface intentionally shallow: inspect the latest snapshot and refresh it when role coverage looks stale.</p>
          </div>

          {roleLibrary ? (
            <div className="detail-stack">
              <div className="detail-meta-grid">
                <DetailMetaCard
                  label="Provider"
                  value={humanizeToken(roleLibrary.provider)}
                  supporting={`Updated ${formatDateTime(roleLibrary.updated_at)}`}
                />
                <DetailMetaCard
                  label="Status"
                  value={humanizeToken(roleLibrary.status)}
                  supporting={`${roleLibrary.entry_count} entries discovered`}
                />
                <DetailMetaCard
                  label="Normalized skills"
                  value={formatCount(roleLibrary.normalized_skill_count)}
                  supporting={`${roleLibrary.alias_count} aliases mapped`}
                />
                <DetailMetaCard
                  label="Role families"
                  value={formatCount(roleLibraryCounts.length)}
                  supporting={`${roleLibrary.missing_role_families.length} missing family marker(s)`}
                />
              </div>

              {roleLibrary.error_message ? (
                <section className="inline-banner inline-banner-warn">
                  <strong>Latest snapshot reported an issue.</strong>
                  <p>{roleLibrary.error_message}</p>
                  <p>Use `Sync role library` to retry with the updated public handbook defaults if this snapshot came from an older GitLab route.</p>
                </section>
              ) : null}

              {roleLibrary.quality_flags.length > 0 ? (
                <div className="review-pill-row">
                  {roleLibrary.quality_flags.map((flag) => (
                    <span key={flag} className="quiet-pill is-blocker">
                      {humanizeToken(flag)}
                    </span>
                  ))}
                </div>
              ) : null}

              {roleLibrary.missing_role_families.length > 0 ? (
                <section className="review-card">
                  <div className="review-card-head">
                    <div>
                      <span className="section-tag">Coverage gaps</span>
                      <h4>Missing role families</h4>
                    </div>
                  </div>
                  <div className="review-pill-row">
                    {roleLibrary.missing_role_families.map((family) => (
                      <span key={family} className="quiet-pill is-blocker">
                        {humanizeToken(family)}
                      </span>
                    ))}
                  </div>
                </section>
              ) : null}

              {Object.keys(roleLibrarySummary).length > 0 ? (
                <section className="review-card">
                  <div className="review-card-head">
                    <div>
                      <span className="section-tag">Snapshot summary</span>
                      <h4>Latest aggregation payload</h4>
                    </div>
                  </div>
                  <JsonBlock value={roleLibrarySummary} compact />
                </section>
              ) : null}
            </div>
          ) : (
            <EmptyState
              title="No role-library snapshot yet"
              description="You can still try blueprint generation, but stage 06 expects operators to refresh the latest role-library snapshot when the role baseline is missing."
            />
          )}

          <form className="profile-page-form" onSubmit={handleSyncRoleLibrary}>
            <div className="profile-form-grid">
              <label className="field-label field-span-full">
                <span>Seed base URLs</span>
                <textarea
                  className="textarea-input textarea-input-compact"
                  value={syncBaseUrlsDraft}
                  onChange={(event) => setSyncBaseUrlsDraft(event.target.value)}
                  placeholder="One GitLab handbook or role-library URL per line. Leave blank to use the backend defaults."
                  disabled={busyAction !== null || isArchivedContext}
                />
              </label>
              <label className="field-label">
                <span>Max pages</span>
                <input
                  className="text-input"
                  type="number"
                  min="1"
                  max="200"
                  value={syncMaxPagesDraft}
                  onChange={(event) => setSyncMaxPagesDraft(event.target.value)}
                  disabled={busyAction !== null || isArchivedContext}
                />
              </label>
            </div>

            <div className="form-actions">
              <button className="secondary-button" type="submit" disabled={busyAction !== null || isArchivedContext}>
                {busyAction === 'sync-role-library' ? 'Syncing...' : 'Sync role library'}
              </button>
              <span className="form-helper-copy">
                Use this only when the latest snapshot looks stale or coverage flags suggest missing families.
              </span>
            </div>
          </form>
        </article>

        <article className="board-panel">
          <div className="panel-heading">
            <span className="section-tag">Run selection</span>
            <h3>Effective, latest, and historical runs</h3>
            <p>Use the run rail to switch context. The selected run drives the lower action surface and detailed inspection tabs.</p>
          </div>

          <div className="summary-grid">
            <article className="summary-card">
              <span className="summary-label">Current scope</span>
              <div className="status-row">
                <strong>{activePlanningContext?.name || 'Legacy workspace'}</strong>
                <StatusChip status={activePlanningContext?.status === 'archived' ? 'blocked' : activePlanningContext?.status === 'draft' ? 'ready' : 'completed'} />
              </div>
              <p>{activePlanningContext ? `${humanizeToken(activePlanningContext.kind)} context` : 'No planning context selected.'}</p>
            </article>
            <article className="summary-card">
              <span className="summary-label">Roadmap analysis</span>
              <div className="status-row">
                <strong>{roadmapSummary?.title || 'Not available'}</strong>
                <StatusChip status={roadmapSummary?.status || 'not_started'} />
              </div>
              <p>{hasRoadmapAnalysis ? 'The scoped roadmap analysis is available for blueprint inputs.' : 'No scoped roadmap analysis has been completed yet.'}</p>
              <div className="form-actions">
                <button
                  className={hasRoadmapAnalysis ? 'secondary-button' : 'primary-button'}
                  type="button"
                  onClick={() => handleRunRoadmapAnalysis().catch(() => undefined)}
                  disabled={busyAction !== null || isArchivedContext}
                >
                  {busyAction === roadmapBusyKey
                    ? hasRoadmapAnalysis ? 'Refreshing...' : 'Running...'
                    : hasRoadmapAnalysis ? 'Refresh roadmap analysis' : 'Run roadmap analysis'}
                </button>
                {roadmapSummary ? (
                  <button
                    className="secondary-button"
                    type="button"
                    onClick={() => handleRunRoadmapAnalysis(true).catch(() => undefined)}
                    disabled={busyAction !== null || isArchivedContext}
                  >
                    {busyAction === roadmapForceBusyKey ? 'Rebuilding...' : 'Force rebuild'}
                  </button>
                ) : null}
                <AppLink className="secondary-button link-button" to={buildScopedWorkspacePath('contexts')}>
                  Manage scoped sources
                </AppLink>
              </div>
              <p className="form-helper-copy">
                Uses parsed roadmap and strategy sources that are active and included in roadmap analysis for this scope.
              </p>
            </article>
            <article className="summary-card">
              <span className="summary-label">Effective downstream run</span>
              <div className="status-row">
                <strong>{effectiveRun?.title || 'Not available'}</strong>
                <StatusChip status={effectiveRun?.status || 'not_started'} />
              </div>
              <p>{effectiveRun ? getBlueprintRunSummaryLabel(effectiveRun) : 'No review-ready or published blueprint is available yet.'}</p>
            </article>
            <article className="summary-card">
              <span className="summary-label">Latest working run</span>
              <div className="status-row">
                <strong>{latestRun?.title || 'Not available'}</strong>
                <StatusChip status={latestRun?.status || 'not_started'} />
              </div>
              <p>{latestRun ? getBlueprintRunSummaryLabel(latestRun) : 'No blueprint run has been generated yet.'}</p>
            </article>
            <article className="summary-card">
              <span className="summary-label">Clarification stage</span>
              <div className="status-row">
                <strong>{clarificationStage?.label || 'Clarifications'}</strong>
                <StatusChip status={clarificationStage?.status || 'not_started'} />
              </div>
              <p>{clarificationStage?.recommended_action || 'Open clarifications remain the dedicated place for question resolution.'}</p>
            </article>
          </div>

          {runs.length > 0 ? (
            <div className="blueprint-run-list">
              {runs.map((run) => {
                const badges = getBlueprintRunBadges(run)
                const clarificationCount = getClarificationOpenCount(run)
                const isSelected = run.uuid === selectedRunUuid

                return (
                  <article key={run.uuid} className={isSelected ? 'review-card blueprint-run-card is-selected' : 'review-card blueprint-run-card'}>
                    <div className="review-card-head">
                      <div>
                        <span className="section-tag">Blueprint run</span>
                        <h4>{run.title || formatShortId(run.uuid)}</h4>
                      </div>
                      <StatusChip status={run.status} />
                    </div>
                    <p>{getBlueprintRunSummaryLabel(run)}</p>
                    <div className="review-pill-row">
                      {badges.map((badge) => (
                        <span key={badge} className="quiet-pill">
                          {badge}
                        </span>
                      ))}
                    </div>
                    <div className="detail-meta-grid">
                      <DetailMetaCard label="Run ID" value={formatShortId(run.uuid)} />
                      <DetailMetaCard label="Clarifications open" value={formatCount(clarificationCount)} />
                      <DetailMetaCard label="Created" value={formatDateTime(run.created_at)} />
                      <DetailMetaCard label="Updated" value={formatDateTime(run.updated_at)} />
                    </div>
                    <div className="form-actions">
                      <button
                        className="secondary-button"
                        type="button"
                        onClick={() => setSelectedRunUuid(run.uuid)}
                        disabled={busyAction !== null}
                      >
                        {isSelected ? 'Selected' : 'View run'}
                      </button>
                    </div>
                  </article>
                )
              })}
            </div>
          ) : (
            <EmptyState
              title="No blueprint runs yet"
              description="Generate the first run from the parse-reviewed workspace to unlock detailed roadmap and role inspection."
              action={
                <button className="primary-button" onClick={() => handleGenerateBlueprint().catch(() => undefined)} disabled={busyAction !== null || isArchivedContext}>
                  Generate blueprint
                </button>
              }
            />
          )}
        </article>
      </section>

      {selectedRun ? (
        <>
          <section className="board-panel">
            <div className="panel-heading">
              <span className="section-tag">Release controls</span>
              <h3>{selectedRun.title || 'Selected blueprint run'}</h3>
              <p>Review, approve, publish, or revise the selected run. Clarification answers stay on the dedicated clarification route.</p>
            </div>

            {selectedRunError ? (
              <ErrorState
                title="Selected run refresh failed"
                description={`${selectedRunError} Showing the most recent detail already loaded for this run.`}
                onRetry={() => loadBlueprintPage(selectedRun.uuid).catch(() => undefined)}
                compact
              />
            ) : null}

            <div className="profile-form-grid">
              <label className="field-label">
                <span>Operator name</span>
                <input
                  className="text-input"
                  value={operatorNameDraft}
                  onChange={(event) => setOperatorNameDraft(event.target.value)}
                  placeholder="Required for review, approval, publication, refresh, or revision"
                  disabled={busyAction !== null || isArchivedContext}
                />
              </label>
              <label className="field-label field-span-full">
                <span>Action notes</span>
                <textarea
                  className="textarea-input textarea-input-compact"
                  value={operatorNoteDraft}
                  onChange={(event) => setOperatorNoteDraft(event.target.value)}
                  placeholder="Use this for review notes, approval context, publish notes, or revision reason."
                  disabled={busyAction !== null || isArchivedContext}
                />
              </label>
            </div>

            <div className="form-actions">
              {canReviewBlueprint(selectedRun) ? (
                <button className="secondary-button" onClick={() => handleReviewBlueprint().catch(() => undefined)} disabled={busyAction !== null || isArchivedContext}>
                  {busyAction === 'review' ? 'Reviewing...' : 'Review run'}
                </button>
              ) : null}
              {canApproveBlueprint(selectedRun) ? (
                <button className="secondary-button" onClick={() => handleApproveBlueprint().catch(() => undefined)} disabled={busyAction !== null || isArchivedContext}>
                  {busyAction === 'approve' ? 'Approving...' : 'Approve run'}
                </button>
              ) : null}
              {canPublishBlueprint(selectedRun) ? (
                <button className="primary-button" onClick={() => handlePublishBlueprint().catch(() => undefined)} disabled={busyAction !== null || isArchivedContext}>
                  {busyAction === 'publish' ? 'Publishing...' : 'Publish run'}
                </button>
              ) : null}
              {canRefreshBlueprintFromClarifications(selectedRun) ? (
                <button
                  className="secondary-button"
                  onClick={() => handleRefreshFromClarifications().catch(() => undefined)}
                  disabled={busyAction !== null || isArchivedContext}
                >
                  {busyAction === 'refresh-from-clarifications' ? 'Refreshing...' : 'Refresh from clarifications'}
                </button>
              ) : null}
              {canStartBlueprintRevision(selectedRun) ? (
                <button className="secondary-button" onClick={() => handleStartRevision().catch(() => undefined)} disabled={busyAction !== null || isArchivedContext}>
                  {busyAction === 'start-revision' ? 'Creating revision...' : 'Start revision'}
                </button>
              ) : null}
              <AppLink className="secondary-button link-button" to={buildScopedWorkspacePath('clarifications')}>
                Review open clarifications
              </AppLink>
            </div>

            {selectedRunOpenClarificationCount > 0 ? (
              <section className="inline-banner inline-banner-warn">
                  <strong>Approval is hidden until clarification work is closed.</strong>
                <ul className="inline-detail-list">
                  <li>{selectedRunOpenClarificationCount} clarification item(s) are still open on this run.</li>
                  <li>`Answered` still counts as open clarification work. Use `Accepted` or `Obsolete` when you want to close a question.</li>
                  <li>Use `Refresh from clarifications` to generate a recalculated draft from the saved answers.</li>
                  <li>After the refreshed run has `0` open clarifications, `Review run` will move it to reviewed and `Approve run` will appear.</li>
                </ul>
                {selectedRunOpenClarifications.length > 0 ? (
                  <div className="helper-list helper-list-compact">
                    {selectedRunOpenClarifications.slice(0, 3).map((question, index) => (
                      <p key={`${readString(question.id) || readString(question.question) || index}`} className="source-error-copy">
                        {readString(question.question) || 'Open clarification question'}
                      </p>
                    ))}
                  </div>
                ) : null}
              </section>
            ) : null}

            {selectedRunOpenClarificationCount === 0 && selectedRun.status === 'reviewed' ? (
              <section className="inline-banner inline-banner-info">
                <strong>This run is ready for approval.</strong>
                <p>Enter the operator name above and use `Approve run` to promote this reviewed draft.</p>
              </section>
            ) : null}

            {selectedRun.status === 'approved' && !selectedRun.is_published ? (
              <section className="inline-banner inline-banner-success">
                <strong>This run is approved and ready to publish.</strong>
                <p>Publishing will make it the effective blueprint for downstream stages.</p>
              </section>
            ) : null}

            <div className="detail-meta-grid">
              <DetailMetaCard
                label="Clarification gate"
                value={formatCount(selectedRunOpenClarificationCount)}
                supporting={selectedRun.approval_blocked ? 'Approval is currently blocked by open clarification work.' : 'No approval blocker is currently reported.'}
              />
              <DetailMetaCard
                label="Generation mode"
                value={humanizeToken(selectedRun.generation_mode || 'generation')}
                supporting={selectedRun.derived_from_run_uuid ? `Derived from ${formatShortId(selectedRun.derived_from_run_uuid)}` : 'Fresh generation run'}
              />
              <DetailMetaCard
                label="Published"
                value={selectedRun.is_published ? 'Yes' : 'No'}
                supporting={selectedRun.published_at ? `Published ${formatDateTime(selectedRun.published_at)}` : 'Not yet published'}
              />
              <DetailMetaCard
                label="Review-ready"
                value={selectedRunOpenClarificationCount > 0 ? 'No' : 'Yes'}
                supporting={`Status is ${humanizeToken(selectedRun.status)}.`}
              />
            </div>
          </section>

          <section className="board-panel">
            <div className="panel-heading">
              <span className="section-tag">Selected run detail</span>
              <h3>Inspection tabs</h3>
              <p>Stay on this page while switching between roadmap, roles, skills, assessment plan, and change history.</p>
            </div>

            <div className="segment-toggle-row">
              {BLUEPRINT_DETAIL_TABS.map((tab) => (
                <button
                  key={tab.key}
                  type="button"
                  className={selectedTab === tab.key ? 'segment-toggle-button is-active' : 'segment-toggle-button'}
                  onClick={() => setSelectedTab(tab.key)}
                >
                  {tab.label}
                </button>
              ))}
            </div>

            {selectedRunLoading ? (
              <LoadingState title="Refreshing selected run" description="Fetching the latest detail payload for the selected blueprint run." compact />
            ) : null}

            {selectedTab === 'overview' ? (
              <div className="detail-stack">
                <div className="detail-meta-grid">
                  <DetailMetaCard
                    label="Title"
                    value={selectedRun.title || formatShortId(selectedRun.uuid)}
                    supporting={`Created ${formatDateTime(selectedRun.created_at)}`}
                  />
                  <DetailMetaCard
                    label="Status"
                    value={humanizeToken(selectedRun.status)}
                    supporting={`Updated ${formatDateTime(selectedRun.updated_at)}`}
                  />
                  <DetailMetaCard
                    label="Generation mode"
                    value={humanizeToken(selectedRun.generation_mode || 'generation')}
                    supporting={selectedRun.derived_from_run_uuid ? `Derived from ${formatShortId(selectedRun.derived_from_run_uuid)}` : 'Fresh generation run'}
                  />
                  <DetailMetaCard
                    label="Role-library snapshot"
                    value={formatShortId(selectedRun.role_library_snapshot_uuid)}
                    supporting="The snapshot used for this run, when present."
                  />
                  <DetailMetaCard
                    label="Clarification summary"
                    value={`${formatCount(selectedRunOpenClarificationCount)} open`}
                    supporting={selectedRun.approval_blocked ? 'Approval is blocked until open questions are resolved.' : 'No open clarification blocker is reported.'}
                  />
                  <DetailMetaCard
                    label="Publication"
                    value={selectedRun.is_published ? 'Published' : 'Not published'}
                    supporting={selectedRun.published_at ? `Published ${formatDateTime(selectedRun.published_at)}` : 'No publication timestamp recorded'}
                  />
                </div>

                <section className="review-card">
                  <div className="review-card-head">
                    <div>
                      <span className="section-tag">Review summary</span>
                      <h4>Release-readiness signals</h4>
                    </div>
                    <StatusChip status={selectedRun.status} />
                  </div>
                  <JsonBlock value={selectedRun.review_summary} compact />
                </section>

                <div className="review-card-grid">
                  <section className="review-card">
                    <div className="review-card-head">
                      <div>
                        <span className="section-tag">Publication metadata</span>
                        <h4>Review, approval, and publication trail</h4>
                      </div>
                    </div>
                    <JsonBlock
                      value={{
                        reviewed_by: selectedRun.reviewed_by,
                        review_notes: selectedRun.review_notes,
                        reviewed_at: selectedRun.reviewed_at,
                        approved_by: selectedRun.approved_by,
                        approval_notes: selectedRun.approval_notes,
                        approved_at: selectedRun.approved_at,
                        published_by: selectedRun.published_by,
                        published_notes: selectedRun.published_notes,
                        published_at: selectedRun.published_at,
                      }}
                      compact
                    />
                  </section>

                  <section className="review-card">
                    <div className="review-card-head">
                      <div>
                        <span className="section-tag">Clarification summary</span>
                        <h4>Cycle and queue metadata</h4>
                      </div>
                    </div>
                    <JsonBlock
                      value={{
                        clarification_summary: selectedRunClarificationSummary,
                        clarification_cycle_uuid: selectedRun.clarification_cycle_uuid,
                        clarification_cycle_status: selectedRun.clarification_cycle_status,
                        clarification_cycle_summary: selectedRun.clarification_cycle_summary,
                      }}
                      compact
                    />
                  </section>
                </div>
              </div>
            ) : null}

            {selectedTab === 'roadmap' ? (
              <div className="detail-stack">
                <section className="review-card">
                  <div className="review-card-head">
                    <div>
                      <span className="section-tag">Company context</span>
                      <h4>Context carried into roadmap synthesis</h4>
                    </div>
                  </div>
                  {Object.keys(selectedRun.company_context).length > 0 ? (
                    <JsonBlock value={selectedRun.company_context} compact />
                  ) : (
                    <p>No structured company context was stored on this run.</p>
                  )}
                </section>

                {selectedRun.roadmap_context.length > 0 ? (
                  <div className="review-card-grid">
                    {selectedRun.roadmap_context.map((initiative, index) => {
                      const initiativeRecord = readRecord(initiative)
                      const initiativeId = readString(initiativeRecord.initiative_id) || `initiative-${index + 1}`
                      const ambiguities = readStringArray(initiativeRecord.ambiguities)
                      return (
                        <article key={initiativeId} className="review-card">
                          <div className="review-card-head">
                            <div>
                              <span className="section-tag">Initiative</span>
                              <h4>{readString(initiativeRecord.title) || humanizeToken(initiativeId)}</h4>
                            </div>
                            <StatusChip status={ambiguities.length > 0 ? 'action_required' : 'completed'} />
                          </div>
                          <p>{readString(initiativeRecord.summary) || 'No initiative summary was stored for this item.'}</p>
                          <div className="review-pill-row">
                            {[
                              readString(initiativeRecord.time_horizon) ? `Time horizon: ${readString(initiativeRecord.time_horizon)}` : '',
                              readString(initiativeRecord.criticality) ? `Criticality: ${humanizeToken(readString(initiativeRecord.criticality))}` : '',
                              ...readStringArray(initiativeRecord.functions_required).map((item) => `Function: ${item}`),
                              ...readStringArray(initiativeRecord.tech_stack).map((item) => `Tech: ${item}`),
                            ]
                              .filter(Boolean)
                              .map((token) => (
                                <span key={token} className="quiet-pill">
                                  {token}
                                </span>
                              ))}
                          </div>
                          {ambiguities.length > 0 ? (
                            <div className="helper-list helper-list-compact">
                              {ambiguities.map((ambiguity) => (
                                <p key={ambiguity} className="source-error-copy">
                                  {ambiguity}
                                </p>
                              ))}
                            </div>
                          ) : null}
                        </article>
                      )
                    })}
                  </div>
                ) : (
                  <EmptyState title="No roadmap context on this run" description="This blueprint run does not include structured roadmap initiatives yet." />
                )}
              </div>
            ) : null}

            {selectedTab === 'roles' ? (
              <div className="detail-stack">
                {selectedRunRoleCandidates.length > 0 ? (
                  <div className="review-card-grid">
                    {selectedRunRoleCandidates.map((roleCandidate) => {
                      const roleKey = getRoleCandidateKey(roleCandidate)
                      const roleName = getRoleCandidateName(roleCandidate)
                      const roleSkills = Array.isArray(roleCandidate.skills) ? roleCandidate.skills : []
                      const roleSkillPreview = roleSkills
                        .slice(0, 4)
                        .map((skill) => readString(readRecord(skill).skill_name_en) || 'Unnamed skill')
                      const ambiguityNotes = readStringArray(roleCandidate.ambiguity_notes)
                      return (
                        <article key={roleKey} className="review-card">
                          <div className="review-card-head">
                            <div>
                              <span className="section-tag">Role candidate</span>
                              <h4>{roleName}</h4>
                            </div>
                            <StatusChip status={ambiguityNotes.length > 0 ? 'action_required' : 'ready'} />
                          </div>
                          <p>{readString(roleCandidate.rationale) || 'No rationale was recorded for this role candidate.'}</p>
                          <div className="detail-meta-grid">
                            <DetailMetaCard
                              label="Skills"
                              value={formatCount(getRoleCandidateSkillCount(roleCandidate))}
                              supporting={roleSkillPreview.length > 0 ? roleSkillPreview.join(', ') : 'No skill preview available'}
                            />
                            <DetailMetaCard
                              label="Initiatives"
                              value={formatCount(getRoleCandidateInitiativeCount(roleCandidate))}
                              supporting={readStringArray(roleCandidate.related_initiatives).join(', ') || 'No related initiatives'}
                            />
                            <DetailMetaCard
                              label="Headcount"
                              value={formatCount(readNumber(roleCandidate.headcount_needed))}
                              supporting={readBoolean(roleCandidate.likely_requires_hiring) ? 'Likely requires hiring' : 'Internal coverage may exist'}
                            />
                            <DetailMetaCard
                              label="Confidence"
                              value={formatConfidence(readNumber(roleCandidate.confidence))}
                              supporting={humanizeToken(readString(roleCandidate.seniority) || 'unknown')}
                            />
                          </div>
                          {ambiguityNotes.length > 0 ? (
                            <div className="helper-list helper-list-compact">
                              {ambiguityNotes.map((note) => (
                                <p key={note} className="source-error-copy">
                                  {note}
                                </p>
                              ))}
                            </div>
                          ) : null}
                          <div className="form-actions">
                            <button
                              className="secondary-button"
                              type="button"
                              onClick={() => setSelectedRoleKey(roleKey)}
                              disabled={busyAction !== null}
                            >
                              {selectedRoleKey === roleKey ? 'Role selected' : 'Inspect role'}
                            </button>
                          </div>
                        </article>
                      )
                    })}
                  </div>
                ) : (
                  <EmptyState title="No role candidates" description="This run does not include role candidates yet." />
                )}

                <SlideOverPanel
                  title={selectedRoleDetail ? getRoleCandidateName(selectedRoleDetail.role_candidate) : 'Role detail'}
                  open={selectedRoleKey !== null}
                  onClose={() => setSelectedRoleKey(null)}
                  wide
                >
                    {selectedRoleLoading ? (
                      <LoadingState title="Loading role detail" description="Fetching the selected role candidate payload." compact />
                    ) : null}
                    {selectedRoleError ? (
                      <ErrorState title="Role detail failed to load" description={selectedRoleError} compact />
                    ) : null}
                    {selectedRoleDetail ? (
                      <div className="detail-stack">
                        <div className="detail-meta-grid">
                          <DetailMetaCard
                            label="Role family"
                            value={humanizeToken(readString(selectedRoleDetail.role_candidate.canonical_role_family) || readString(selectedRoleDetail.role_candidate.role_family) || 'unknown')}
                          />
                          <DetailMetaCard
                            label="Seniority"
                            value={humanizeToken(readString(selectedRoleDetail.role_candidate.seniority) || 'unknown')}
                          />
                          <DetailMetaCard
                            label="Headcount"
                            value={formatCount(readNumber(selectedRoleDetail.role_candidate.headcount_needed))}
                          />
                          <DetailMetaCard
                            label="Confidence"
                            value={formatConfidence(readNumber(selectedRoleDetail.role_candidate.confidence))}
                          />
                        </div>

                        {readStringArray(selectedRoleDetail.role_candidate.responsibilities).length > 0 ? (
                          <section className="review-card">
                            <div className="review-card-head">
                              <div>
                                <span className="section-tag">Responsibilities</span>
                                <h4>Delivery expectations</h4>
                              </div>
                            </div>
                            <ul className="helper-list">
                              {readStringArray(selectedRoleDetail.role_candidate.responsibilities).map((item) => (
                                <li key={item}>{item}</li>
                              ))}
                            </ul>
                          </section>
                        ) : null}

                        <section className="review-card">
                          <div className="review-card-head">
                            <div>
                              <span className="section-tag">Raw role payload</span>
                              <h4>Complete candidate detail</h4>
                            </div>
                          </div>
                          <JsonBlock value={selectedRoleDetail.role_candidate} compact />
                        </section>
                      </div>
                    ) : null}
                </SlideOverPanel>
              </div>
            ) : null}

            {selectedTab === 'skills' ? (
              <div className="detail-stack">
                {selectedRunRequiredSkills.length > 0 ? (
                  <div className="review-card-grid">
                    {selectedRunRequiredSkills.slice(0, 18).map((skillRequirement, index) => {
                      const requirementRecord = readRecord(skillRequirement)
                      const skillName =
                        readFirstString(requirementRecord, ['skill_name_en', 'skill_name_ru']) ||
                        `Skill ${index + 1}`
                      const targetLevel = readFirstNumber(requirementRecord, ['target_level', 'max_target_level'])
                      const priority = readFirstNumber(requirementRecord, ['priority', 'max_priority'])
                      const confidence = readFirstNumber(requirementRecord, ['confidence', 'max_confidence'])
                      const requirementType = readFirstString(requirementRecord, ['requirement_type'])
                      const requiredByRoles = readStringArray(requirementRecord.required_by_roles)
                      const supportedInitiatives = readStringArray(requirementRecord.supported_initiatives)
                      const requirementSummary =
                        readFirstString(requirementRecord, ['reason']) ||
                        [
                          requiredByRoles.length > 0 ? `Required by ${requiredByRoles.join(', ')}` : '',
                          supportedInitiatives.length > 0 ? `Supports ${supportedInitiatives.join(', ')}` : '',
                        ]
                          .filter(Boolean)
                          .join(' | ')
                      return (
                        <article key={`${skillName}-${index}`} className="review-card">
                          <div className="review-card-head">
                            <div>
                              <span className="section-tag">Required skill</span>
                              <h4>{skillName}</h4>
                            </div>
                            <StatusChip status={readString(requirementRecord.criticality) || 'ready'} />
                          </div>
                          <p>{requirementSummary || 'No explicit rationale was recorded for this skill requirement.'}</p>
                          <div className="detail-meta-grid">
                            <DetailMetaCard label="Target level" value={formatCount(targetLevel)} />
                            <DetailMetaCard label="Priority" value={formatCount(priority)} />
                            <DetailMetaCard label="Confidence" value={formatConfidence(confidence)} />
                            <DetailMetaCard label="Requirement type" value={humanizeToken(requirementType || 'unknown')} />
                          </div>
                          {requiredByRoles.length > 0 || supportedInitiatives.length > 0 ? (
                            <div className="review-pill-row">
                              {requiredByRoles.map((role) => (
                                <span key={`role-${role}`} className="quiet-pill">
                                  Role: {role}
                                </span>
                              ))}
                              {supportedInitiatives.map((initiative) => (
                                <span key={`initiative-${initiative}`} className="quiet-pill">
                                  Initiative: {initiative}
                                </span>
                              ))}
                            </div>
                          ) : null}
                        </article>
                      )
                    })}
                  </div>
                ) : (
                  <EmptyState title="No required skill set" description="The selected run does not include a flattened required skill set yet." />
                )}

                <div className="review-card-grid">
                  <section className="review-card">
                    <div className="review-card-head">
                      <div>
                        <span className="section-tag">Gap summary</span>
                        <h4>Role and capability gaps</h4>
                      </div>
                    </div>
                    <JsonBlock value={selectedRun.gap_summary} compact />
                  </section>
                  <section className="review-card">
                    <div className="review-card-head">
                      <div>
                        <span className="section-tag">Redundancy summary</span>
                        <h4>Overlap and redundancy view</h4>
                      </div>
                    </div>
                    <JsonBlock value={selectedRun.redundancy_summary} compact />
                  </section>
                </div>

                {selectedRunAutomationCandidates.length > 0 ? (
                  <section className="review-card">
                    <div className="review-card-head">
                      <div>
                        <span className="section-tag">Automation candidates</span>
                        <h4>Potential automation or leverage ideas</h4>
                      </div>
                    </div>
                    <JsonBlock value={selectedRunAutomationCandidates} compact />
                  </section>
                ) : null}

                {selectedRunOccupationMap.length > 0 ? (
                  <section className="review-card">
                    <div className="review-card-head">
                      <div>
                        <span className="section-tag">Occupation mapping</span>
                        <h4>Reference role matches</h4>
                      </div>
                    </div>
                    <JsonBlock value={selectedRunOccupationMap} compact />
                  </section>
                ) : null}
              </div>
            ) : null}

            {selectedTab === 'assessment' ? (
              <div className="detail-stack">
                <div className="detail-meta-grid">
                  <DetailMetaCard
                    label="Per-employee questions"
                    value={formatCount(readNumber(selectedRun.assessment_plan.per_employee_question_count))}
                    supporting="This count will be consumed by stage 07 cycle generation."
                  />
                  <DetailMetaCard
                    label="Question themes"
                    value={formatCount(readStringArray(selectedRun.assessment_plan.question_themes).length)}
                  />
                  <DetailMetaCard
                    label="Matched employees"
                    value={formatCount(employeeMatches.length)}
                    supporting="Employee matching is informative here; stage 08 owns deeper role-match analytics."
                  />
                  <DetailMetaCard
                    label="Clarification cycle"
                    value={selectedRun.clarification_cycle_uuid ? formatShortId(selectedRun.clarification_cycle_uuid) : 'Not available'}
                    supporting={selectedRun.clarification_cycle_status ? humanizeToken(selectedRun.clarification_cycle_status) : 'No clarification cycle metadata'}
                  />
                </div>

                {readString(selectedRun.assessment_plan.global_notes) ? (
                  <section className="review-card">
                    <div className="review-card-head">
                      <div>
                        <span className="section-tag">Assessment notes</span>
                        <h4>Global questionnaire guidance</h4>
                      </div>
                    </div>
                    <p>{readString(selectedRun.assessment_plan.global_notes)}</p>
                  </section>
                ) : null}

                {readStringArray(selectedRun.assessment_plan.question_themes).length > 0 ? (
                  <section className="review-card">
                    <div className="review-card-head">
                      <div>
                        <span className="section-tag">Question themes</span>
                        <h4>Stage 07 input hints</h4>
                      </div>
                    </div>
                    <ul className="helper-list">
                      {readStringArray(selectedRun.assessment_plan.question_themes).map((theme) => (
                        <li key={theme}>{theme}</li>
                      ))}
                    </ul>
                  </section>
                ) : null}

                {employeeMatches.length > 0 ? (
                  <section className="review-card">
                    <div className="review-card-head">
                      <div>
                        <span className="section-tag">Employee matches</span>
                        <h4>Stored role-match hints</h4>
                      </div>
                    </div>
                    <JsonBlock value={employeeMatches} compact />
                  </section>
                ) : null}
              </div>
            ) : null}

            {selectedTab === 'changes' ? (
              <div className="detail-stack">
                <div className="detail-meta-grid">
                  <DetailMetaCard label="Reviewed by" value={selectedRun.reviewed_by || 'Not recorded'} supporting={selectedRun.reviewed_at ? formatDateTime(selectedRun.reviewed_at) : 'No review timestamp'} />
                  <DetailMetaCard label="Approved by" value={selectedRun.approved_by || 'Not recorded'} supporting={selectedRun.approved_at ? formatDateTime(selectedRun.approved_at) : 'No approval timestamp'} />
                  <DetailMetaCard label="Published by" value={selectedRun.published_by || 'Not recorded'} supporting={selectedRun.published_at ? formatDateTime(selectedRun.published_at) : 'No publication timestamp'} />
                  <DetailMetaCard label="Derived from" value={selectedRun.derived_from_run_uuid ? formatShortId(selectedRun.derived_from_run_uuid) : 'No ancestor'} supporting={selectedRun.generation_mode ? humanizeToken(selectedRun.generation_mode) : 'Generation'} />
                </div>

                {selectedRun.review_notes || selectedRun.approval_notes || selectedRun.published_notes ? (
                  <section className="review-card">
                    <div className="review-card-head">
                      <div>
                        <span className="section-tag">Release notes</span>
                        <h4>Stored review, approval, and publish notes</h4>
                      </div>
                    </div>
                    <JsonBlock
                      value={{
                        review_notes: selectedRun.review_notes,
                        approval_notes: selectedRun.approval_notes,
                        published_notes: selectedRun.published_notes,
                      }}
                      compact
                    />
                  </section>
                ) : null}

                {changeLog.length > 0 ? (
                  <div className="review-card-grid">
                    {changeLog.map((entry, index) => {
                      const entryRecord = readRecord(entry)
                      const changeKey = `${readString(entryRecord.event) || 'event'}-${readString(entryRecord.timestamp) || index}`
                      return (
                        <article key={changeKey} className="review-card">
                          <div className="review-card-head">
                            <div>
                              <span className="section-tag">Change event</span>
                              <h4>{humanizeToken(readString(entryRecord.event) || 'update')}</h4>
                            </div>
                            <span className="quiet-pill">{formatDateTime(readString(entryRecord.timestamp))}</span>
                          </div>
                          <p>{readString(entryRecord.actor_name) || readString(entryRecord.actor) || 'System or unnamed operator'}</p>
                          <JsonBlock value={entryRecord} compact />
                        </article>
                      )
                    })}
                  </div>
                ) : (
                  <EmptyState title="No change log entries" description="This run has not recorded any release-stage change log items yet." />
                )}
              </div>
            ) : null}
          </section>
        </>
      ) : (
        <EmptyState
          title="No blueprint selected"
          description="Generate a blueprint or select an existing historical run to inspect roadmap, roles, and release actions."
        />
      )}
    </div>
  )
}
