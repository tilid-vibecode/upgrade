import { useCallback, useEffect, useMemo, useState, type FormEvent } from 'react'

import { AppLink } from '../app/navigation'
import { useWorkspaceShell } from '../app/WorkspaceLayout'
import { CollapsibleHero } from '../shared/ui/CollapsibleHero'
import { getApiErrorMessage, getApiErrorMessages } from '../shared/api'
import { formatDateTime } from '../shared/formatters'
import {
  approvePrototypePendingSkill,
  buildPrototypeCVEvidence,
  clearPrototypeEmployeeNoCvAvailable,
  deletePrototypeEmployee,
  getPrototypeCVEvidenceStatus,
  getPrototypeEmployeeEvidenceDetail,
  getPrototypeOrgContextSummary,
  getPrototypeParsedSourceDetail,
  listPrototypeCVReviewItems,
  listPrototypeEmployeesWithoutCvEvidence,
  listPrototypeParsedSources,
  listPrototypeUnmatchedCvs,
  listPrototypeWorkspaceEmployees,
  markPrototypeEmployeeNoCvAvailable,
  previewPrototypeOrgCsvSource,
  rebuildPrototypeCVEvidence,
  reparsePrototypeWorkspaceSource,
  resolvePrototypeCvMatch,
  type PrototypeCVEvidenceBuildResponse,
  type PrototypeCVEvidenceStatusResponse,
  type PrototypeEmployeeCVProfile,
  type PrototypeEmployeeCvAvailability,
  type PrototypeEmployeeCoverageGap,
  type PrototypeEmployeeDeleteResponse,
  type PrototypeEmployeeEvidenceDetailResponse,
  type PrototypeEmployeeResponse,
  type PrototypeEmployeeWithoutCVEvidence,
  type PrototypeOrgContextSummaryResponse,
  type PrototypeOrgCsvPreviewResponse,
  type PrototypePendingSkillCandidate,
  type PrototypeParsedSourceDetailResponse,
  type PrototypeParsedSourceSummary,
} from '../shared/prototypeApi'
import { getSourceKindLabel, getSourceTransportLabel } from '../shared/sourceLibrary'
import { humanizeToken } from '../shared/workflow'
import EmptyState from '../shared/ui/EmptyState'
import ErrorState from '../shared/ui/ErrorState'
import LoadingState from '../shared/ui/LoadingState'
import SlideOverPanel from '../shared/ui/SlideOverPanel'
import StatusChip from '../shared/ui/StatusChip'

type ReviewMode = 'parsed_sources' | 'org_context' | 'cv_review'
type BannerTone = 'info' | 'success' | 'warn' | 'error'

type BannerState = {
  tone: BannerTone
  title: string
  messages: string[]
}

type PendingSkillDraft = {
  approvedNameEn: string
  approvedNameRu: string
  approvalNote: string
}

type InspectionState =
  | { kind: 'parsed_source'; parsedSourceUuid: string }
  | { kind: 'csv_preview'; sourceUuid: string; sourceTitle: string }
  | { kind: 'employee_evidence'; employeeUuid: string; employeeName: string }
  | { kind: 'cv_resolution'; profile: PrototypeEmployeeCVProfile }

type QueueProfileCardProps = {
  profile: PrototypeEmployeeCVProfile
  title: string
  description: string
  actionLabel: string
  onResolve: (profile: PrototypeEmployeeCVProfile) => void
  onViewEvidence: (employeeUuid: string, employeeName: string) => void
}

function readString(value: unknown) {
  return typeof value === 'string' ? value : ''
}

function readStringArray(value: unknown) {
  return Array.isArray(value) ? value.filter((item): item is string => typeof item === 'string' && item.trim().length > 0) : []
}

function readRecord(value: unknown): Record<string, unknown> | null {
  return typeof value === 'object' && value !== null && !Array.isArray(value) ? value as Record<string, unknown> : null
}

function readNumber(value: unknown) {
  return typeof value === 'number' && Number.isFinite(value) ? value : null
}

function readStringFromRecord(record: Record<string, unknown>, key: string) {
  return readString(record[key])
}

function formatScore(value: number) {
  if (!Number.isFinite(value)) {
    return '0%'
  }

  const normalized = value <= 1 ? value * 100 : value
  return `${Math.round(normalized)}%`
}

function formatLevel(value: number) {
  if (!Number.isFinite(value)) {
    return '0'
  }

  return Number.isInteger(value) ? String(value) : value.toFixed(1)
}

function buildFocusCopy(parseStatus: string, blueprintStatus: string) {
  if (parseStatus === 'blocked' || parseStatus === 'action_required' || parseStatus === 'failed' || parseStatus === 'running') {
    return 'Parsing still needs attention. Review parsed output first, fix CSV mapping issues if needed, and only then trust the workspace context.'
  }

  if (blueprintStatus === 'blocked') {
    return 'Parsing and CV review look healthy. The remaining blocker is blueprint readiness, so the next move is to attach or parse the missing blueprint inputs and then move to Blueprint.'
  }

  if (blueprintStatus === 'ready' || blueprintStatus === 'action_required' || blueprintStatus === 'running') {
    return 'Parsing and CV review look healthy. The next checkpoint is blueprint generation and publication.'
  }

  return 'The workspace has crossed the parsing checkpoint. Use this page to spot residual quality issues before moving into blueprint work.'
}

const CV_REVIEW_REASON_COPY: Record<string, { label: string; nextStep: string }> = {
  no_matched_cv_profile: {
    label: 'No matched CV profile',
    nextStep: 'Attach a CV for this employee, or confirm that no CV is available yet.',
  },
  candidate_cv_pending_review: {
    label: 'Candidate CV match needs review',
    nextStep: 'Review the candidate CV profiles below and resolve the correct employee match.',
  },
  no_cv_evidence: {
    label: 'No CV evidence rows recorded',
    nextStep: 'Inspect the linked CV profile and rebuild if the source should contain usable evidence.',
  },
  extraction_failed: {
    label: 'CV extraction failed',
    nextStep: 'Rebuild the CV profile, and if it fails again inspect the source document and warnings.',
  },
  sparse_cv: {
    label: 'CV is too sparse',
    nextStep: 'Use a fuller CV or resume with role history, achievements, and explicit skill evidence.',
  },
  empty_cv_evidence: {
    label: 'No structured evidence extracted',
    nextStep: 'Inspect the linked CV and rebuild after improving the source content if needed.',
  },
  low_confidence_match: {
    label: 'Low-confidence employee match',
    nextStep: 'Confirm the employee match before relying on downstream evidence.',
  },
  ambiguous_match: {
    label: 'Ambiguous employee match',
    nextStep: 'Choose the correct employee match before relying on downstream evidence.',
  },
  unmatched_source: {
    label: 'Unmatched CV source',
    nextStep: 'Resolve the profile-to-employee match before relying on downstream evidence.',
  },
  pending_skill_candidates: {
    label: 'Skills still need review',
    nextStep: 'Review unresolved extracted skills and add approved overrides where needed.',
  },
}

function describeCvReviewReason(reason: string) {
  return CV_REVIEW_REASON_COPY[reason] ?? {
    label: humanizeToken(reason),
    nextStep: 'Review the linked CV source and decide whether to rebuild, resolve, or replace it.',
  }
}

function dedupeText(values: string[]) {
  const seen = new Set<string>()
  const result: string[] = []
  values.forEach((value) => {
    const normalized = value.trim()
    if (!normalized) {
      return
    }
    const key = normalized.toLowerCase()
    if (seen.has(key)) {
      return
    }
    seen.add(key)
    result.push(normalized)
  })
  return result
}

function buildCoverageGapSummary(gap: PrototypeEmployeeCoverageGap) {
  if (gap.review_reasons.includes('candidate_cv_pending_review')) {
    return 'A plausible CV match exists for this employee, but it still needs manual confirmation before evidence can be attached.'
  }
  if (gap.review_reasons.includes('no_matched_cv_profile')) {
    return 'No CV is currently matched to this employee, so there is no CV-derived evidence to show yet.'
  }
  if (gap.review_reasons.includes('extraction_failed')) {
    return 'A matched CV exists for this employee, but structured extraction failed before evidence rows could be created.'
  }
  if (gap.review_reasons.includes('sparse_cv')) {
    return 'A matched CV exists, but it is too sparse to produce dependable evidence rows.'
  }
  if (gap.review_reasons.includes('pending_skill_candidates')) {
    return 'A matched CV exists, but some extracted skills still need review before evidence can be trusted.'
  }
  if (gap.cv_profile_count > 0) {
    return 'A matched CV profile exists, but it has not produced usable evidence rows yet.'
  }
  return 'This employee still lacks CV-derived evidence and needs manual follow-up.'
}

function buildCoverageGapActions(gap: PrototypeEmployeeCoverageGap) {
  return dedupeText(gap.review_reasons.map((reason) => describeCvReviewReason(reason).nextStep))
}

function buildCvBuildBanner(result: PrototypeCVEvidenceBuildResponse, rebuild: boolean): BannerState {
  const processedCount = result.processed
  const ambiguousCount = result.status_counts.ambiguous ?? 0
  const unmatchedCount = result.status_counts.unmatched ?? 0
  const warningMessages = result.results.flatMap((item) => item.warnings).slice(0, 3)

  if (ambiguousCount > 0 || unmatchedCount > 0) {
    return {
      tone: 'warn',
      title: `${rebuild ? 'Rebuilt' : 'Built'} ${processedCount} CV profile${processedCount === 1 ? '' : 's'} with review work still visible.`,
      messages: warningMessages.length > 0
        ? warningMessages
        : [
            `${ambiguousCount} ambiguous profile${ambiguousCount === 1 ? '' : 's'} and ${unmatchedCount} unmatched profile${unmatchedCount === 1 ? '' : 's'} still need operator review.`,
          ],
    }
  }

  return {
    tone: 'success',
    title: `${rebuild ? 'Rebuilt' : 'Built'} ${processedCount} CV profile${processedCount === 1 ? '' : 's'}.`,
    messages: [
      `${result.reused_count} reused and ${result.rebuilt_count} rebuilt profile${result.rebuilt_count === 1 ? '' : 's'} were reported by the backend.`,
    ],
  }
}

function buildSourceReparseBanner(sourceTitle: string, status: string, parseError: string): BannerState {
  if (status === 'failed') {
    return {
      tone: 'warn',
      title: `${sourceTitle} failed to reparse.`,
      messages: [parseError || 'The parser reported a failure for this source.'],
    }
  }

  return {
    tone: 'success',
    title: `${sourceTitle} reparsed successfully.`,
    messages: ['The parsed-source review surfaces and org-context summary were refreshed.'],
  }
}

function buildResolutionBanner(profile: PrototypeEmployeeCVProfile): BannerState {
  return {
    tone: 'success',
    title: `Resolved ${profile.source_title || 'CV profile'} to ${profile.full_name || 'the selected employee'}.`,
    messages: [
      profile.matched_by
        ? `Resolution is now recorded as matched by ${humanizeToken(profile.matched_by)}.`
        : 'The CV profile is now linked to the selected employee.',
    ],
  }
}

function buildProfileTagList(profile: PrototypeEmployeeCVProfile) {
  return [
    profile.evidence_quality ? `Quality: ${humanizeToken(profile.evidence_quality)}` : '',
    profile.profile_current_role ? `Role: ${profile.profile_current_role}` : '',
    profile.seniority ? `Seniority: ${humanizeToken(profile.seniority)}` : '',
    profile.vector_index_status ? `Vector index: ${humanizeToken(profile.vector_index_status)}` : '',
  ].filter(Boolean)
}

function getCandidateEmployeeUuid(candidate: Record<string, unknown>) {
  const direct = readStringFromRecord(candidate, 'employee_uuid')
  if (direct) {
    return direct
  }

  return readStringFromRecord(candidate, 'uuid')
}

function buildCandidateLabel(candidate: Record<string, unknown>) {
  const name =
    readStringFromRecord(candidate, 'full_name') ||
    readStringFromRecord(candidate, 'employee_name') ||
    readStringFromRecord(candidate, 'name') ||
    'Suggested employee'
  const title = readStringFromRecord(candidate, 'current_title') || readStringFromRecord(candidate, 'role')
  const confidence = readNumber(candidate.match_confidence ?? candidate.confidence)
  const suffixParts = [
    title,
    confidence !== null ? `confidence ${formatScore(confidence)}` : '',
  ].filter(Boolean)

  return suffixParts.length > 0 ? `${name} (${suffixParts.join(' · ')})` : name
}

function buildPendingSkillCandidateKey(candidate: PrototypePendingSkillCandidate) {
  const values = [
    candidate.candidate_key,
    candidate.proposed_key,
    candidate.display_name_en,
    ...(candidate.original_terms || []),
    ...(candidate.aliases || []),
  ]
  for (const value of values) {
    const normalized = readString(value).trim().toLowerCase()
    if (normalized) {
      return normalized
    }
  }
  return ''
}

function buildPendingSkillDraft(candidate: PrototypePendingSkillCandidate): PendingSkillDraft {
  return {
    approvedNameEn: readString(candidate.display_name_en) || readString(candidate.original_terms?.[0]) || '',
    approvedNameRu: readString(candidate.display_name_ru),
    approvalNote: '',
  }
}

function buildCvAvailabilityLabel(availability: PrototypeEmployeeCvAvailability | null | undefined) {
  if ((availability?.status || '') === 'no_cv_available') {
    return 'No CV confirmed'
  }
  return 'CV expected'
}

function buildEmployeeEvidenceCvLabel(detail: PrototypeEmployeeEvidenceDetailResponse) {
  if ((detail.cv_availability.status || '') === 'no_cv_available') {
    return buildCvAvailabilityLabel(detail.cv_availability)
  }
  if (detail.evidence_rows.length > 0) {
    return 'CV evidence ready'
  }
  if (detail.cv_profiles.length > 0) {
    return 'CV matched'
  }
  return buildCvAvailabilityLabel(detail.cv_availability)
}

function buildMappingTargetKeys(preview: PrototypeOrgCsvPreviewResponse) {
  const keys = new Set<string>()

  Object.keys(preview.inferred_mapping).forEach((key) => keys.add(key))
  Object.keys(preview.effective_mapping).forEach((key) => keys.add(key))
  Object.keys(preview.ambiguous_targets).forEach((key) => keys.add(key))
  preview.missing_targets.forEach((key) => keys.add(key))
  Object.keys(preview.override_applied).forEach((key) => keys.add(key))

  return Array.from(keys).sort()
}

function buildMappingDraftFromPreview(preview: PrototypeOrgCsvPreviewResponse) {
  return buildMappingTargetKeys(preview).reduce<Record<string, string>>((draft, key) => {
    draft[key] = preview.override_applied[key] || preview.effective_mapping[key] || ''
    return draft
  }, {})
}

function syncMappingDraftWithPreview(
  currentDraft: Record<string, string>,
  preview: PrototypeOrgCsvPreviewResponse,
) {
  const nextDraft = buildMappingDraftFromPreview(preview)

  buildMappingTargetKeys(preview).forEach((key) => {
    if (currentDraft[key] !== undefined) {
      nextDraft[key] = currentDraft[key]
    }
  })

  return nextDraft
}

function normalizeMappingOverride(draft: Record<string, string>) {
  return Object.entries(draft).reduce<Record<string, string>>((accumulator, [target, header]) => {
    const normalizedHeader = header.trim()
    if (normalizedHeader) {
      accumulator[target] = normalizedHeader
    }
    return accumulator
  }, {})
}

function formatMetadataValue(value: unknown): string {
  if (typeof value === 'string') {
    return value
  }

  if (typeof value === 'number' || typeof value === 'boolean') {
    return String(value)
  }

  if (Array.isArray(value)) {
    return value.map((item) => formatMetadataValue(item)).join(', ')
  }

  if (value && typeof value === 'object') {
    try {
      return JSON.stringify(value, null, 2)
    } catch {
      return '[Object]'
    }
  }

  return ''
}

/** Return true when the value looks like a JSON blob (starts with { or [). */
function isJsonBlob(v: string): boolean {
  const trimmed = v.trim()
  return (trimmed.startsWith('{') && trimmed.endsWith('}')) || (trimmed.startsWith('[') && trimmed.endsWith(']'))
}

function buildMetadataEntries(metadata: Record<string, unknown>) {
  return Object.entries(metadata)
    .map(([key, value]) => ({
      key,
      label: humanizeToken(key),
      value: formatMetadataValue(value),
    }))
    .filter((item) => item.value.trim().length > 0)
}

function buildImportedOrgFieldEntries(metadata: Record<string, unknown>) {
  const provenance = readRecord(metadata.org_csv_provenance)
  const fields = provenance ? readRecord(provenance.fields) : null
  return fields ? buildMetadataEntries(fields) : []
}

function formatEmployeeId(value: string) {
  return value.trim().length > 0 ? `Employee ID ${value}` : 'No external employee ID'
}

function QueueProfileCard({
  profile,
  title,
  description,
  actionLabel,
  onResolve,
  onViewEvidence,
}: QueueProfileCardProps) {
  const candidateMatches = Array.isArray(profile.candidate_matches)
    ? profile.candidate_matches.filter((item): item is Record<string, unknown> => typeof item === 'object' && item !== null)
    : []
  const factCountEntries = Object.entries(profile.fact_counts || {}).filter(([, value]) => Number.isFinite(value))

  return (
    <article className="review-card">
      <div className="review-card-head">
        <div>
          <span className="section-tag">{title}</span>
          <h4>{profile.source_title || profile.full_name || 'CV profile'}</h4>
          <p>{description}</p>
        </div>
        <StatusChip status={profile.status || 'action_required'} />
      </div>

      <div className="review-pill-row">
        {buildProfileTagList(profile).map((item) => (
          <span key={item} className="quiet-pill">
            {item}
          </span>
        ))}
        {profile.match_confidence > 0 ? (
          <span className="quiet-pill">Match confidence {formatScore(profile.match_confidence)}</span>
        ) : null}
      </div>

      {profile.headline ? <p className="form-helper-copy">{profile.headline}</p> : null}

      {factCountEntries.length > 0 ? (
        <div className="detail-meta-grid">
          {factCountEntries.map(([key, value]) => (
            <div key={key}>
              <span className="summary-label">{humanizeToken(key)}</span>
              <strong>{value}</strong>
            </div>
          ))}
        </div>
      ) : null}

      {profile.review_reasons.length > 0 ? (
        <ul className="helper-list helper-list-compact">
          {profile.review_reasons.map((reason) => (
            <li key={reason}>{describeCvReviewReason(reason).label}</li>
          ))}
        </ul>
      ) : null}

      {profile.warnings.length > 0 ? (
        <ul className="helper-list helper-list-compact">
          {profile.warnings.map((warning) => (
            <li key={warning}>{warning}</li>
          ))}
        </ul>
      ) : null}

      {candidateMatches.length > 0 ? (
        <div className="detail-stack">
          <strong>Suggested matches</strong>
          <ul className="helper-list helper-list-compact">
            {candidateMatches.map((candidate, index) => (
              <li key={`${profile.source_uuid}:${index}`}>{buildCandidateLabel(candidate)}</li>
            ))}
          </ul>
        </div>
      ) : null}

      <div className="source-action-group">
        <button className="primary-button source-action-button" type="button" onClick={() => onResolve(profile)}>
          {actionLabel}
        </button>
        {profile.employee_uuid ? (
          <button
            className="secondary-button source-action-button"
            type="button"
            onClick={() => onViewEvidence(profile.employee_uuid || '', profile.full_name || 'Employee evidence')}
          >
            View evidence
          </button>
        ) : null}
      </div>
    </article>
  )
}

export default function WorkspaceParsePage() {
  const {
    workspace,
    workflow,
    activePlanningContext,
    planningContextOptions,
    refreshShell,
    buildScopedWorkspacePath,
  } = useWorkspaceShell()
  const [mode, setMode] = useState<ReviewMode>('parsed_sources')
  const [orgSummary, setOrgSummary] = useState<PrototypeOrgContextSummaryResponse | null>(null)
  const [parsedSources, setParsedSources] = useState<PrototypeParsedSourceSummary[]>([])
  const [cvStatus, setCvStatus] = useState<PrototypeCVEvidenceStatusResponse | null>(null)
  const [isLoading, setIsLoading] = useState(true)
  const [loadError, setLoadError] = useState<string | null>(null)
  const [reloadToken, setReloadToken] = useState(0)
  const [banner, setBanner] = useState<BannerState | null>(null)
  const [employees, setEmployees] = useState<PrototypeEmployeeResponse[] | null>(null)
  const [isEmployeesLoading, setIsEmployeesLoading] = useState(false)
  const [employeesError, setEmployeesError] = useState<string | null>(null)
  const [unmatchedProfiles, setUnmatchedProfiles] = useState<PrototypeEmployeeCVProfile[] | null>(null)
  const [reviewProfiles, setReviewProfiles] = useState<PrototypeEmployeeCVProfile[] | null>(null)
  const [employeesWithoutEvidence, setEmployeesWithoutEvidence] = useState<PrototypeEmployeeWithoutCVEvidence[] | null>(null)
  const [isCvQueuesLoading, setIsCvQueuesLoading] = useState(false)
  const [cvQueuesError, setCvQueuesError] = useState<string | null>(null)
  const [inspection, setInspection] = useState<InspectionState | null>(null)
  const [inspectionLoading, setInspectionLoading] = useState(false)
  const [inspectionError, setInspectionError] = useState<string | null>(null)
  const [parsedSourceDetail, setParsedSourceDetail] = useState<PrototypeParsedSourceDetailResponse | null>(null)
  const [csvPreview, setCsvPreview] = useState<PrototypeOrgCsvPreviewResponse | null>(null)
  const [employeeEvidenceDetail, setEmployeeEvidenceDetail] = useState<PrototypeEmployeeEvidenceDetailResponse | null>(null)
  const [mappingDraft, setMappingDraft] = useState<Record<string, string>>({})
  const [resolutionDraft, setResolutionDraft] = useState({
    employee_uuid: '',
    operator_name: '',
    resolution_note: '',
  })
  const [pendingSkillDrafts, setPendingSkillDrafts] = useState<Record<string, PendingSkillDraft>>({})
  const [actionKey, setActionKey] = useState<string | null>(null)

  const parseStage = workflow.stages.find((stage) => stage.key === 'parse') ?? null
  const blueprintStage = workflow.stages.find((stage) => stage.key === 'blueprint') ?? null
  const parsedSourceCount = typeof parseStage?.metadata.total_parsed_sources === 'number'
    ? parseStage.metadata.total_parsed_sources
    : parsedSources.length
  const failedSourceCount = parseStage?.status === 'failed' ? parseStage.blockers.length : 0
  const parseBlockerCount = parseStage?.blockers.length ?? 0
  const parsedOrgCsvSources = parsedSources.filter((source) => source.source_kind === 'org_csv')
  const orgCsvWarningCount = parsedOrgCsvSources.filter((source) => source.warning_count > 0).length
  const unresolvedCvCount = cvStatus?.unresolved_source_count ?? 0
  const canBuildCvProfiles = (cvStatus?.parsed_cv_sources ?? 0) > 0 && (orgSummary?.employee_count ?? 0) > 0
  const canRebuildCvProfiles = (cvStatus?.processed_profile_count ?? 0) > 0
  const blueprintNeedsRoadmapAnalysis = (blueprintStage?.blockers ?? []).some((blocker) =>
    blocker.toLowerCase().includes('roadmap analysis'),
  )
  const selectedParsedSource = inspection?.kind === 'parsed_source' ? inspection.parsedSourceUuid : null
  const selectedCsvSourceUuid = inspection?.kind === 'csv_preview' ? inspection.sourceUuid : null
  const activeEmployeeEvidenceUuid = inspection?.kind === 'employee_evidence' ? inspection.employeeUuid : null
  const activeResolutionProfile = inspection?.kind === 'cv_resolution' ? inspection.profile : null
  const employeeMetadataEntries = employeeEvidenceDetail ? buildMetadataEntries(employeeEvidenceDetail.metadata || {}) : []
  const importedOrgFieldEntries = employeeEvidenceDetail ? buildImportedOrgFieldEntries(employeeEvidenceDetail.metadata || {}) : []

  const sortedEmployees = useMemo(
    () => (employees ?? []).slice().sort((left, right) => left.full_name.localeCompare(right.full_name)),
    [employees],
  )

  useEffect(() => {
    let cancelled = false

    async function loadOverview() {
      setIsLoading(true)
      setLoadError(null)

      try {
        const [orgSummaryResponse, parsedSourcesResponse, cvStatusResponse] = await Promise.all([
          getPrototypeOrgContextSummary(workspace.slug, planningContextOptions),
          listPrototypeParsedSources(workspace.slug),
          getPrototypeCVEvidenceStatus(workspace.slug),
        ])

        if (cancelled) {
          return
        }

        setOrgSummary(orgSummaryResponse)
        setParsedSources(parsedSourcesResponse.parsed_sources)
        setCvStatus(cvStatusResponse)
      } catch (requestError) {
        if (!cancelled) {
          setLoadError(getApiErrorMessage(requestError, 'Failed to load the parse review workspace.'))
        }
      } finally {
        if (!cancelled) {
          setIsLoading(false)
        }
      }
    }

    void loadOverview()

    return () => {
      cancelled = true
    }
  }, [planningContextOptions, reloadToken, workspace.slug])

  useEffect(() => {
    let cancelled = false

    async function loadInspection() {
      if (inspection === null) {
        setInspectionError(null)
        setParsedSourceDetail(null)
        setCsvPreview(null)
        setEmployeeEvidenceDetail(null)
        return
      }

      if (inspection.kind === 'cv_resolution') {
        setInspectionError(null)
        setParsedSourceDetail(null)
        setCsvPreview(null)
        setEmployeeEvidenceDetail(null)
        return
      }

      setInspectionLoading(true)
      setInspectionError(null)
      setParsedSourceDetail(null)
      setCsvPreview(null)
      setEmployeeEvidenceDetail(null)

      try {
        if (inspection.kind === 'parsed_source') {
          const response = await getPrototypeParsedSourceDetail(workspace.slug, inspection.parsedSourceUuid)
          if (cancelled) {
            return
          }

          setParsedSourceDetail(response)
        }

        if (inspection.kind === 'csv_preview') {
          const response = await previewPrototypeOrgCsvSource(workspace.slug, inspection.sourceUuid)
          if (cancelled) {
            return
          }

          setCsvPreview(response)
          setMappingDraft(buildMappingDraftFromPreview(response))
        }

        if (inspection.kind === 'employee_evidence') {
          const response = await getPrototypeEmployeeEvidenceDetail(workspace.slug, inspection.employeeUuid)
          if (cancelled) {
            return
          }

          setEmployeeEvidenceDetail(response)
        }
      } catch (requestError) {
        if (!cancelled) {
          setInspectionError(getApiErrorMessage(requestError, 'Failed to load the selected inspection detail.'))
        }
      } finally {
        if (!cancelled) {
          setInspectionLoading(false)
        }
      }
    }

    void loadInspection()

    return () => {
      cancelled = true
    }
  }, [inspection, workspace.slug])

  const loadEmployees = useCallback(async () => {
    setIsEmployeesLoading(true)
    setEmployeesError(null)

    try {
      const response = await listPrototypeWorkspaceEmployees(workspace.slug)
      setEmployees(response.employees)
    } catch (requestError) {
      setEmployeesError(getApiErrorMessage(requestError, 'Failed to load imported employees.'))
    } finally {
      setIsEmployeesLoading(false)
    }
  }, [workspace.slug])

  const loadCvQueues = useCallback(async () => {
    setIsCvQueuesLoading(true)
    setCvQueuesError(null)

    try {
      const [unmatchedResponse, reviewResponse, withoutEvidenceResponse] = await Promise.all([
        listPrototypeUnmatchedCvs(workspace.slug),
        listPrototypeCVReviewItems(workspace.slug),
        listPrototypeEmployeesWithoutCvEvidence(workspace.slug),
      ])

      setUnmatchedProfiles(unmatchedResponse.items)
      setReviewProfiles(reviewResponse.items)
      setEmployeesWithoutEvidence(withoutEvidenceResponse.items)
    } catch (requestError) {
      setCvQueuesError(getApiErrorMessage(requestError, 'Failed to load CV review queues.'))
    } finally {
      setIsCvQueuesLoading(false)
    }
  }, [workspace.slug])

  useEffect(() => {
    if (mode === 'org_context' && employees === null && !isEmployeesLoading && (orgSummary?.employee_count ?? 0) > 0) {
      void loadEmployees()
    }
  }, [employees, isEmployeesLoading, loadEmployees, mode, orgSummary?.employee_count])

  useEffect(() => {
    if (mode === 'cv_review' && unmatchedProfiles === null && !isCvQueuesLoading) {
      void loadCvQueues()
    }
  }, [isCvQueuesLoading, loadCvQueues, mode, unmatchedProfiles])

  useEffect(() => {
    if (inspection?.kind === 'cv_resolution' && employees === null && !isEmployeesLoading && (orgSummary?.employee_count ?? 0) > 0) {
      void loadEmployees()
    }
  }, [employees, inspection, isEmployeesLoading, loadEmployees, orgSummary?.employee_count])

  function reloadOverview() {
    setReloadToken((current) => current + 1)
  }

  function refreshAfterMutation() {
    reloadOverview()
    refreshShell()

    if (employees !== null) {
      void loadEmployees()
    }

    if (unmatchedProfiles !== null || reviewProfiles !== null || employeesWithoutEvidence !== null) {
      void loadCvQueues()
    }
  }

  function refreshActiveEmployeeEvidence() {
    if (inspection?.kind !== 'employee_evidence') {
      return
    }

    setInspection({
      kind: 'employee_evidence',
      employeeUuid: inspection.employeeUuid,
      employeeName: inspection.employeeName,
    })
  }

  function pendingSkillDraftKey(
    profile: PrototypeEmployeeCVProfile,
    candidate: PrototypePendingSkillCandidate,
    fallbackKey = 'pending-skill',
  ) {
    return `${profile.source_uuid}:${buildPendingSkillCandidateKey(candidate) || fallbackKey}`
  }

  function readPendingSkillDraft(
    profile: PrototypeEmployeeCVProfile,
    candidate: PrototypePendingSkillCandidate,
    fallbackKey = 'pending-skill',
  ) {
    const key = pendingSkillDraftKey(profile, candidate, fallbackKey)
    return pendingSkillDrafts[key] || buildPendingSkillDraft(candidate)
  }

  function updatePendingSkillDraft(
    profile: PrototypeEmployeeCVProfile,
    candidate: PrototypePendingSkillCandidate,
    patch: Partial<PendingSkillDraft>,
    fallbackKey = 'pending-skill',
  ) {
    const key = pendingSkillDraftKey(profile, candidate, fallbackKey)
    setPendingSkillDrafts((current) => ({
      ...current,
      [key]: {
        ...(current[key] || buildPendingSkillDraft(candidate)),
        ...patch,
      },
    }))
  }

  function openEmployeeEvidence(employeeUuid: string, employeeName: string) {
    setInspection({
      kind: 'employee_evidence',
      employeeUuid,
      employeeName,
    })
  }

  function openCvResolution(profile: PrototypeEmployeeCVProfile) {
    const suggestedCandidateUuid = (Array.isArray(profile.candidate_matches)
      ? profile.candidate_matches
          .filter((item): item is Record<string, unknown> => typeof item === 'object' && item !== null)
          .map((item) => getCandidateEmployeeUuid(item))
          .find(Boolean)
      : '') || ''

    setResolutionDraft({
      employee_uuid: profile.employee_uuid || suggestedCandidateUuid,
      operator_name: '',
      resolution_note: '',
    })
    setInspection({ kind: 'cv_resolution', profile })
  }

  async function handleBuildCvEvidence(rebuild: boolean) {
    setActionKey(rebuild ? 'cv-rebuild' : 'cv-build')
    setBanner(null)

    try {
      const response = rebuild
        ? await rebuildPrototypeCVEvidence(workspace.slug, { source_uuids: [] })
        : await buildPrototypeCVEvidence(workspace.slug, { source_uuids: [] })

      setBanner(buildCvBuildBanner(response, rebuild))
      refreshAfterMutation()
      refreshActiveEmployeeEvidence()
    } catch (requestError) {
      setBanner({
        tone: 'error',
        title: rebuild ? 'CV rebuild failed.' : 'CV build failed.',
        messages: getApiErrorMessages(requestError),
      })
    } finally {
      setActionKey(null)
    }
  }

  async function handleRebuildEmployeeCvProfiles() {
    if (employeeEvidenceDetail === null || employeeEvidenceDetail.cv_profiles.length === 0) {
      return
    }

    const sourceUuids = Array.from(
      new Set(
        employeeEvidenceDetail.cv_profiles
          .map((profile) => profile.source_uuid)
          .filter((sourceUuid): sourceUuid is string => sourceUuid.trim().length > 0),
      ),
    )
    if (sourceUuids.length === 0) {
      return
    }

    const actionScope = sourceUuids.length === 1 ? sourceUuids[0] : employeeEvidenceDetail.employee_uuid
    setActionKey(`cv-rebuild-linked:${actionScope}`)
    setBanner(null)

    try {
      const response = await rebuildPrototypeCVEvidence(workspace.slug, { source_uuids: sourceUuids })
      setBanner(buildCvBuildBanner(response, true))
      refreshAfterMutation()
      refreshActiveEmployeeEvidence()
    } catch (requestError) {
      setBanner({
        tone: 'error',
        title: 'Linked CV rebuild failed.',
        messages: getApiErrorMessages(requestError),
      })
    } finally {
      setActionKey(null)
    }
  }

  async function handleParsedSourceReparse(
    parsedSource: PrototypeParsedSourceSummary,
    mappingOverride?: Record<string, string>,
  ) {
    setActionKey(`reparse:${parsedSource.source_uuid}`)
    setBanner(null)

    try {
      const response = await reparsePrototypeWorkspaceSource(workspace.slug, parsedSource.source_uuid, mappingOverride ? {
        mapping_override: mappingOverride,
      } : undefined)

      setBanner(buildSourceReparseBanner(parsedSource.source_title, response.status, response.parse_error))
      refreshAfterMutation()

      if (inspection?.kind === 'parsed_source') {
        const nextParsedSourceUuid = response.parsed_source?.uuid || parsedSource.uuid
        setInspection({ kind: 'parsed_source', parsedSourceUuid: nextParsedSourceUuid })
      }

      if (inspection?.kind === 'csv_preview' && inspection.sourceUuid === parsedSource.source_uuid) {
        setInspection({
          kind: 'csv_preview',
          sourceUuid: parsedSource.source_uuid,
          sourceTitle: parsedSource.source_title,
        })
      }
    } catch (requestError) {
      setBanner({
        tone: 'error',
        title: `Failed to reparse ${parsedSource.source_title}.`,
        messages: getApiErrorMessages(requestError),
      })
    } finally {
      setActionKey(null)
    }
  }

  async function handleCsvPreviewRefresh(event?: FormEvent) {
    event?.preventDefault()

    if (inspection?.kind !== 'csv_preview') {
      return
    }

    setActionKey(`preview:${inspection.sourceUuid}`)
    setInspectionError(null)

    try {
      const response = await previewPrototypeOrgCsvSource(workspace.slug, inspection.sourceUuid, {
        mapping_override: normalizeMappingOverride(mappingDraft),
        sample_row_count: 5,
      })

      setCsvPreview(response)
      setMappingDraft((currentDraft) => syncMappingDraftWithPreview(currentDraft, response))
    } catch (requestError) {
      setInspectionError(getApiErrorMessage(requestError, 'Failed to refresh the CSV preview.'))
    } finally {
      setActionKey(null)
    }
  }

  async function handleResolutionSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()

    if (activeResolutionProfile === null || !resolutionDraft.employee_uuid) {
      setBanner({
        tone: 'warn',
        title: 'Select an employee before resolving the match.',
        messages: ['The backend resolution endpoint needs an employee target for this action.'],
      })
      return
    }

    setActionKey(`resolve:${activeResolutionProfile.source_uuid}`)
    setBanner(null)

    try {
      const response = await resolvePrototypeCvMatch(workspace.slug, activeResolutionProfile.source_uuid, {
        employee_uuid: resolutionDraft.employee_uuid,
        operator_name: resolutionDraft.operator_name.trim() || undefined,
        resolution_note: resolutionDraft.resolution_note.trim() || undefined,
      })

      setInspection(null)
      setBanner(buildResolutionBanner(response))
      refreshAfterMutation()
    } catch (requestError) {
      setBanner({
        tone: 'error',
        title: 'Failed to resolve the CV match.',
        messages: getApiErrorMessages(requestError),
      })
    } finally {
      setActionKey(null)
    }
  }

  async function handleApprovePendingSkill(
    profile: PrototypeEmployeeCVProfile,
    candidate: PrototypePendingSkillCandidate,
  ) {
    const candidateKey = buildPendingSkillCandidateKey(candidate)
    const draft = readPendingSkillDraft(profile, candidate)
    if (!candidateKey) {
      setBanner({
        tone: 'warn',
        title: 'This pending skill cannot be approved yet.',
        messages: ['The candidate is missing a stable key, so the backend cannot identify it safely.'],
      })
      return
    }
    if (!draft.approvedNameEn.trim()) {
      setBanner({
        tone: 'warn',
        title: 'Approved skill name is required.',
        messages: ['Set the skill name you want to approve before saving the override.'],
      })
      return
    }

    setActionKey(`approve-skill:${profile.source_uuid}:${candidateKey}`)
    setBanner(null)

    try {
      const response = await approvePrototypePendingSkill(workspace.slug, profile.source_uuid, {
        candidate_key: candidateKey,
        approved_name_en: draft.approvedNameEn.trim(),
        approved_name_ru: draft.approvedNameRu.trim(),
        alias_terms: dedupeText([...(candidate.aliases || []), ...(candidate.original_terms || [])]),
        approval_note: draft.approvalNote.trim(),
      })

      setBanner({
        tone: 'success',
        title: `Approved skill override for ${draft.approvedNameEn.trim()}.`,
        messages: ['The override was saved and the related CV profile was refreshed.'],
      })
      refreshAfterMutation()

      if (inspection?.kind === 'cv_resolution' && inspection.profile.source_uuid === profile.source_uuid) {
        setInspection({ kind: 'cv_resolution', profile: response })
      } else {
        refreshActiveEmployeeEvidence()
      }
    } catch (requestError) {
      setBanner({
        tone: 'error',
        title: 'Failed to approve the pending skill.',
        messages: getApiErrorMessages(requestError),
      })
    } finally {
      setActionKey(null)
    }
  }

  async function handleMarkNoCvAvailable() {
    if (employeeEvidenceDetail === null) {
      return
    }

    setActionKey(`mark-no-cv:${employeeEvidenceDetail.employee_uuid}`)
    setBanner(null)

    try {
      await markPrototypeEmployeeNoCvAvailable(workspace.slug, employeeEvidenceDetail.employee_uuid)
      setBanner({
        tone: 'success',
        title: `Marked ${employeeEvidenceDetail.full_name} as having no CV available.`,
        messages: ['This employee will no longer appear in the action-required CV coverage gap list.'],
      })
      refreshAfterMutation()
      refreshActiveEmployeeEvidence()
    } catch (requestError) {
      setBanner({
        tone: 'error',
        title: 'Failed to mark this employee as having no CV.',
        messages: getApiErrorMessages(requestError),
      })
    } finally {
      setActionKey(null)
    }
  }

  async function handleClearNoCvAvailable() {
    if (employeeEvidenceDetail === null) {
      return
    }

    setActionKey(`clear-no-cv:${employeeEvidenceDetail.employee_uuid}`)
    setBanner(null)

    try {
      await clearPrototypeEmployeeNoCvAvailable(workspace.slug, employeeEvidenceDetail.employee_uuid)
      setBanner({
        tone: 'success',
        title: `Cleared the no-CV confirmation for ${employeeEvidenceDetail.full_name}.`,
        messages: ['This employee will appear in the CV coverage gap list again until a CV is matched or evidence is created.'],
      })
      refreshAfterMutation()
      refreshActiveEmployeeEvidence()
    } catch (requestError) {
      setBanner({
        tone: 'error',
        title: 'Failed to clear the no-CV confirmation.',
        messages: getApiErrorMessages(requestError),
      })
    } finally {
      setActionKey(null)
    }
  }

  async function handleDeleteEmployee() {
    if (employeeEvidenceDetail === null) {
      return
    }

    const confirmed = window.confirm(
      `Delete ${employeeEvidenceDetail.full_name}? This removes the employee record and detaches any matched CV profiles.`,
    )
    if (!confirmed) {
      return
    }

    setActionKey(`delete-employee:${employeeEvidenceDetail.employee_uuid}`)
    setBanner(null)

    try {
      const response: PrototypeEmployeeDeleteResponse = await deletePrototypeEmployee(
        workspace.slug,
        employeeEvidenceDetail.employee_uuid,
      )
      setInspection(null)
      setBanner({
        tone: 'success',
        title: `Deleted ${response.full_name}.`,
        messages: [
          response.detached_cv_profile_count > 0
            ? `${response.detached_cv_profile_count} linked CV profile(s) were detached and moved back into review.`
            : 'The employee record was removed from the workspace.',
        ],
      })
      refreshAfterMutation()
    } catch (requestError) {
      setBanner({
        tone: 'error',
        title: 'Failed to delete this employee.',
        messages: getApiErrorMessages(requestError),
      })
    } finally {
      setActionKey(null)
    }
  }

  function renderPendingSkillCandidates(
    profile: PrototypeEmployeeCVProfile,
    sectionLabel = 'Pending skill review',
  ) {
    if (!Array.isArray(profile.pending_skill_candidates) || profile.pending_skill_candidates.length === 0) {
      return null
    }

    return (
      <section className="detail-stack">
        <strong>{sectionLabel}</strong>
        <div className="review-card-grid">
          {profile.pending_skill_candidates.map((candidate, index) => {
            const stableCandidateKey = buildPendingSkillCandidateKey(candidate)
            const localCandidateKey = stableCandidateKey || `pending-${index}`
            const draft = readPendingSkillDraft(profile, candidate, localCandidateKey)
            const busyKey = `approve-skill:${profile.source_uuid}:${stableCandidateKey}`
            return (
              <article key={`${profile.source_uuid}:${localCandidateKey}`} className="review-card">
                <div className="review-card-head">
                  <div>
                    <span className="section-tag">Pending skill</span>
                    <h4>{candidate.display_name_en || candidate.original_terms?.[0] || 'Unnamed skill candidate'}</h4>
                    <p>{candidate.category ? humanizeToken(candidate.category) : 'Unresolved extracted skill'}</p>
                  </div>
                  {typeof candidate.confidence_score === 'number' ? (
                    <span className="quiet-pill">Confidence {formatScore(candidate.confidence_score)}</span>
                  ) : null}
                </div>

                {(candidate.original_terms || []).length > 0 ? (
                  <div className="detail-stack">
                    <span className="summary-label">Original terms</span>
                    <ul className="helper-list helper-list-compact">
                      {dedupeText(candidate.original_terms || []).map((term) => (
                        <li key={`${localCandidateKey}:term:${term}`}>{term}</li>
                      ))}
                    </ul>
                  </div>
                ) : null}

                {(candidate.aliases || []).length > 0 ? (
                  <div className="detail-stack">
                    <span className="summary-label">Aliases</span>
                    <p className="form-helper-copy">{dedupeText(candidate.aliases || []).join(', ')}</p>
                  </div>
                ) : null}

                {(candidate.evidence_texts || []).length > 0 ? (
                  <div className="detail-stack">
                    <span className="summary-label">Evidence snippets</span>
                    <ul className="helper-list helper-list-compact">
                      {dedupeText(candidate.evidence_texts || []).slice(0, 3).map((snippet) => (
                        <li key={`${localCandidateKey}:snippet:${snippet}`}>{snippet}</li>
                      ))}
                    </ul>
                  </div>
                ) : null}

                <div className="profile-form-grid">
                  <label className="field-label">
                    <span>Approve as</span>
                    <input
                      className="text-input"
                      type="text"
                      value={draft.approvedNameEn}
                      onChange={(event) => updatePendingSkillDraft(profile, candidate, { approvedNameEn: event.target.value }, localCandidateKey)}
                      placeholder="Approved skill name"
                    />
                  </label>

                  <label className="field-label">
                    <span>Russian label</span>
                    <input
                      className="text-input"
                      type="text"
                      value={draft.approvedNameRu}
                      onChange={(event) => updatePendingSkillDraft(profile, candidate, { approvedNameRu: event.target.value }, localCandidateKey)}
                      placeholder="Optional"
                    />
                  </label>

                  <label className="field-label field-span-full">
                    <span>Approval note</span>
                    <textarea
                      className="textarea-input textarea-input-compact"
                      value={draft.approvalNote}
                      onChange={(event) => updatePendingSkillDraft(profile, candidate, { approvalNote: event.target.value }, localCandidateKey)}
                      placeholder="Optional note about why this override is approved."
                    />
                  </label>
                </div>

                <div className="form-actions">
                  <button
                    className="primary-button"
                    type="button"
                    disabled={!stableCandidateKey || actionKey === busyKey}
                    onClick={() => void handleApprovePendingSkill(profile, candidate)}
                  >
                    {!stableCandidateKey ? 'Missing candidate key' : actionKey === busyKey ? 'Approving...' : 'Approve override'}
                  </button>
                </div>
              </article>
            )
          })}
        </div>
      </section>
    )
  }

  if (isLoading && orgSummary === null && cvStatus === null) {
    return (
      <LoadingState
        title="Loading parse review"
        description="Fetching parsed sources, org-context counts, and CV evidence status for this workspace."
      />
    )
  }

  if (orgSummary === null || cvStatus === null) {
    return (
      <ErrorState
        title="Parse review failed to load"
        description={loadError || 'The stage 05 route resolved, but the parse review payload could not be loaded.'}
        onRetry={reloadOverview}
      />
    )
  }

  const mappingTargetKeys = csvPreview ? buildMappingTargetKeys(csvPreview) : []

  return (
    <div className="page-stack">
      <CollapsibleHero
        tag="Stage 05"
        title="Parse review and org-context verification"
        statusSlot={<StatusChip status={parseStage?.status || 'not_started'} />}
      >
        <div className="hero-copy">
          <p>{buildFocusCopy(parseStage?.status || 'not_started', blueprintStage?.status || 'not_started')}</p>
          <div className="hero-actions">
            <AppLink className="secondary-button link-button" to={buildScopedWorkspacePath('sources')}>
              Back to Sources
            </AppLink>
            <AppLink className="secondary-button link-button" to={buildScopedWorkspacePath('blueprint')}>
              Go to Blueprint
            </AppLink>
          </div>
        </div>

        <div className="page-stack">
          <div className="route-badge">
            <span className="summary-label">Parse stage</span>
            <strong>{parseStage?.label || 'Parsing and normalization'}</strong>
            <StatusChip status={parseStage?.status || 'not_started'} />
          </div>
          <div className="route-badge">
            <span className="summary-label">Blueprint stage</span>
            <strong>{blueprintStage?.label || 'Blueprint generation'}</strong>
            <StatusChip status={blueprintStage?.status || 'not_started'} />
          </div>
        </div>
      </CollapsibleHero>

      {activePlanningContext ? (
        <section className="inline-banner inline-banner-info">
          <strong>Parsing and CV evidence stay workspace-wide.</strong>
          <span>
            The active context {activePlanningContext.name} only affects scoped role-match awareness here. CSV parsing, CV evidence build, review queues, and employee evidence remain shared across planning contexts.
          </span>
        </section>
      ) : null}

      {banner ? (
        <div className={`inline-banner inline-banner-${banner.tone}`}>
          <strong>{banner.title}</strong>
          {banner.messages.length > 0 ? (
            <ul className="inline-detail-list">
              {banner.messages.map((message) => (
                <li key={message}>{message}</li>
              ))}
            </ul>
          ) : null}
        </div>
      ) : null}

      {loadError ? (
        <ErrorState
          compact
          title="Latest reload failed"
          description={`${loadError} Showing the most recent successful parse review payload.`}
          onRetry={reloadOverview}
        />
      ) : null}

      {parseStage && parseStage.blockers.length > 0 ? (
        <div className={`inline-banner inline-banner-${parseStage.status === 'failed' ? 'warn' : 'info'}`}>
          <strong>{parseStage.recommended_action || 'Parsing still needs attention.'}</strong>
          <ul className="inline-detail-list">
            {parseStage.blockers.slice(0, 5).map((blocker) => (
              <li key={blocker}>{blocker}</li>
            ))}
          </ul>
        </div>
      ) : null}

      {parseStage?.status === 'completed' && blueprintStage && blueprintStage.blockers.length > 0 ? (
        <div className="inline-banner inline-banner-info">
          <strong>{blueprintStage.recommended_action || 'Blueprint preparation still needs attention.'}</strong>
          <ul className="inline-detail-list">
            {blueprintStage.blockers.slice(0, 5).map((blocker) => (
              <li key={blocker}>{blocker}</li>
            ))}
          </ul>
        </div>
      ) : null}

      {blueprintNeedsRoadmapAnalysis ? (
        <div className="inline-banner inline-banner-info">
          <strong>Roadmap analysis is triggered from the Blueprint route.</strong>
          <ul className="inline-detail-list">
            <li>Stage 05 prepares the parsed roadmap and strategy inputs, but the actual scoped roadmap-analysis run lives on Blueprint.</li>
            <li>Use `Manage scoped sources` if the right roadmap or strategy files are not included for this planning context yet.</li>
          </ul>
          <div className="form-actions">
            <AppLink className="primary-button link-button" to={buildScopedWorkspacePath('blueprint')}>
              Open Blueprint to run analysis
            </AppLink>
            <AppLink className="secondary-button link-button" to={buildScopedWorkspacePath('contexts')}>
              Manage scoped sources
            </AppLink>
          </div>
        </div>
      ) : null}

      <section className="summary-grid">
        <article className="summary-card">
          <span className="summary-label">Parsed sources</span>
          <strong>{parsedSourceCount}</strong>
          <p>{parseBlockerCount} parse blocker(s) visible in workflow status.</p>
        </article>
        <article className="summary-card">
          <span className="summary-label">Failed sources</span>
          <strong>{failedSourceCount}</strong>
          <p>{parseStage?.status === 'failed' ? 'These failures are currently blocking the parse checkpoint.' : 'No failed parsed-source blockers are visible right now.'}</p>
        </article>
        <article className="summary-card">
          <span className="summary-label">CSV mapping attention</span>
          <strong>{orgCsvWarningCount}</strong>
          <p>{parsedOrgCsvSources.length} parsed org spreadsheet source(s) are available for preview.</p>
        </article>
        <article className="summary-card">
          <span className="summary-label">Employees imported</span>
          <strong>{orgSummary.employee_count}</strong>
          <p>{orgSummary.org_unit_count} org unit(s), {orgSummary.project_count} project(s), and {orgSummary.reporting_line_count} reporting line(s).</p>
        </article>
        <article className="summary-card">
          <span className="summary-label">CV profiles</span>
          <strong>{cvStatus.processed_profile_count}</strong>
          <p>{cvStatus.parsed_cv_sources} parsed CV source(s) and {cvStatus.vector_indexed_source_count} indexed profile(s).</p>
        </article>
        <article className="summary-card">
          <span className="summary-label">Review queue</span>
          <strong>{unresolvedCvCount}</strong>
          <p>{cvStatus.employees_without_cv_evidence_count} employee(s) still lack CV evidence.</p>
        </article>
      </section>

      <section className="board-panel">
        <div className="panel-heading">
          <span className="section-tag">Review surfaces</span>
          <h3>Stay on one route while you verify the workspace</h3>
          <p>Switch between parsed output, org-context results, and CV review queues without leaving the stage 05 shell.</p>
        </div>

        <div className="segment-toggle-row" role="tablist" aria-label="Parse review modes">
          {[
            { key: 'parsed_sources' as const, label: 'Parsed sources' },
            { key: 'org_context' as const, label: 'Org context' },
            { key: 'cv_review' as const, label: 'CV review' },
          ].map((item) => (
            <button
              key={item.key}
              className={mode === item.key ? 'segment-toggle-button is-active' : 'segment-toggle-button'}
              type="button"
              onClick={() => setMode(item.key)}
            >
              {item.label}
            </button>
          ))}
        </div>

        {mode === 'parsed_sources' ? (
          <div className="detail-stack">
            <div className="sources-toolbar">
              <div>
                <strong>Parsed-source library</strong>
                <p className="form-helper-copy">
                  Inspect parser output here. If the source itself needs correction, jump back to the source library instead of editing it on this page.
                </p>
              </div>
              <div className="hero-actions">
                <AppLink className="secondary-button link-button" to={buildScopedWorkspacePath('sources')}>
                  Manage sources
                </AppLink>
              </div>
            </div>

            {parsedSources.length > 0 ? (
              <div className="source-table-shell">
                <table className="source-table">
                  <thead>
                    <tr>
                      <th>Source</th>
                      <th>Parser</th>
                      <th>Volume</th>
                      <th>Status</th>
                      <th>Updated</th>
                      <th>Actions</th>
                    </tr>
                  </thead>
                  <tbody>
                    {parsedSources.map((parsedSource) => (
                      <tr
                        key={parsedSource.uuid}
                        className={selectedParsedSource === parsedSource.uuid || selectedCsvSourceUuid === parsedSource.source_uuid ? 'is-active' : undefined}
                      >
                        <td>
                          <div className="source-primary-cell">
                            <strong>{parsedSource.source_title || 'Untitled source'}</strong>
                            <p>{getSourceKindLabel(parsedSource.source_kind)}</p>
                            <div className="review-pill-row">
                              <span className="quiet-pill">Vector {humanizeToken(parsedSource.vector_index_status || 'pending')}</span>
                              {parsedSource.language_code ? <span className="quiet-pill">{parsedSource.language_code}</span> : null}
                            </div>
                          </div>
                        </td>
                        <td>
                          <div className="source-secondary-cell">
                            <p>{parsedSource.parser_name || 'Unknown parser'}</p>
                            <p>{parsedSource.parser_version || 'No parser version'}</p>
                          </div>
                        </td>
                        <td>
                          <div className="source-secondary-cell">
                            <p>{parsedSource.page_count ?? 0} page(s)</p>
                            <p>{parsedSource.chunk_count} chunk(s)</p>
                            <p>{parsedSource.word_count} word(s)</p>
                          </div>
                        </td>
                        <td>
                          <div className="source-secondary-cell">
                            <StatusChip status={parsedSource.source_status || 'parsed'} />
                            <p>{parsedSource.warning_count} warning(s)</p>
                            {parsedSource.parse_error ? <p className="source-error-copy">{parsedSource.parse_error}</p> : null}
                          </div>
                        </td>
                        <td>
                          <p className="form-helper-copy">{formatDateTime(parsedSource.updated_at)}</p>
                        </td>
                        <td>
                          <div className="source-action-group">
                            <button
                              className="secondary-button source-action-button"
                              type="button"
                              onClick={() => setInspection({ kind: 'parsed_source', parsedSourceUuid: parsedSource.uuid })}
                            >
                              Inspect
                            </button>
                            {parsedSource.source_kind === 'org_csv' ? (
                              <button
                                className="secondary-button source-action-button"
                                type="button"
                                onClick={() =>
                                  setInspection({
                                    kind: 'csv_preview',
                                    sourceUuid: parsedSource.source_uuid,
                                    sourceTitle: parsedSource.source_title,
                                  })
                                }
                              >
                                Preview CSV
                              </button>
                            ) : null}
                            <button
                              className="secondary-button source-action-button"
                              type="button"
                              disabled={actionKey === `reparse:${parsedSource.source_uuid}`}
                              onClick={() => void handleParsedSourceReparse(parsedSource)}
                            >
                              {actionKey === `reparse:${parsedSource.source_uuid}` ? 'Reparsing...' : 'Reparse'}
                            </button>
                            <AppLink
                              className="secondary-button link-button source-action-button"
                              to={buildScopedWorkspacePath('sources')}
                            >
                              Open in Sources
                            </AppLink>
                          </div>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            ) : (
              <EmptyState
                title="No parsed sources yet"
                description="The parser has not produced reviewable sources for this workspace yet. Return to the sources page to run parsing or fix failed inputs."
                action={
                  <AppLink className="primary-button link-button" to={buildScopedWorkspacePath('sources')}>
                    Open Sources
                  </AppLink>
                }
              />
            )}
          </div>
        ) : null}

        {mode === 'org_context' ? (
          <div className="detail-stack">
            <div className="summary-grid">
              <article className="summary-card">
                <span className="summary-label">Employees</span>
                <strong>{orgSummary.employee_count}</strong>
                <p>Imported employee records currently available in org context.</p>
              </article>
              <article className="summary-card">
                <span className="summary-label">Org units</span>
                <strong>{orgSummary.org_unit_count}</strong>
                <p>Units extracted from parsed source material.</p>
              </article>
              <article className="summary-card">
                <span className="summary-label">Projects</span>
                <strong>{orgSummary.project_count}</strong>
                <p>Projects or initiatives currently visible in org context.</p>
              </article>
              <article className="summary-card">
                <span className="summary-label">Skill evidence</span>
                <strong>{orgSummary.skill_evidence_count}</strong>
                <p>Employee evidence rows stored for this workspace.</p>
              </article>
            </div>

            {employeesError ? (
              <ErrorState compact title="Employee list failed to load" description={employeesError} onRetry={() => void loadEmployees()} />
            ) : null}

            {orgSummary.employee_count === 0 ? (
              <EmptyState
                title="No imported employees yet"
                description="The org-context summary does not show any employee records yet. Fix the org CSV mapping or reparse the relevant sources before moving on."
                action={
                  <AppLink className="primary-button link-button" to={buildScopedWorkspacePath('sources')}>
                    Back to Sources
                  </AppLink>
                }
              />
            ) : employees === null && isEmployeesLoading ? (
              <LoadingState
                title="Loading imported employees"
                description="Fetching the current employee list from org context."
                compact
              />
            ) : employees && employees.length > 0 ? (
              <div className="source-table-shell">
                <table className="source-table">
                  <thead>
                    <tr>
                      <th>Employee</th>
                      <th>Role</th>
                      <th>Org record</th>
                      <th>Actions</th>
                    </tr>
                  </thead>
                  <tbody>
                    {sortedEmployees.map((employee) => (
                      <tr key={employee.uuid} className={activeEmployeeEvidenceUuid === employee.uuid ? 'is-active' : undefined}>
                        <td>
                          <div className="source-primary-cell">
                            <strong>{employee.full_name}</strong>
                            <p>{employee.email || 'No email recorded'}</p>
                            {(employee.cv_availability?.status || '') === 'no_cv_available' ? (
                              <div className="review-pill-row">
                                <span className="quiet-pill">{buildCvAvailabilityLabel(employee.cv_availability)}</span>
                              </div>
                            ) : null}
                          </div>
                        </td>
                        <td>
                          <p className="form-helper-copy">{employee.current_title || 'No title recorded'}</p>
                        </td>
                        <td>
                          <div className="source-secondary-cell">
                            <p>{formatEmployeeId(employee.external_employee_id || '')}</p>
                            <p>{Object.keys(employee.metadata || {}).length} stored metadata field(s)</p>
                          </div>
                        </td>
                        <td>
                          <div className="source-action-group">
                            <button
                              className="secondary-button source-action-button"
                              type="button"
                              onClick={() => openEmployeeEvidence(employee.uuid, employee.full_name)}
                            >
                              View evidence
                            </button>
                          </div>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            ) : (
              <EmptyState
                title="Employee list is empty"
                description="The summary reports employee records, but the list endpoint returned nothing. Refresh the page or re-run the org import flow."
                action={
                  <button className="secondary-button" type="button" onClick={() => void loadEmployees()}>
                    Reload employees
                  </button>
                }
              />
            )}
          </div>
        ) : null}

        {mode === 'cv_review' ? (
          <div className="detail-stack">
            <div className="sources-toolbar">
              <div>
                <strong>CV profile build and review</strong>
                <p className="form-helper-copy">
                  Stage 05 owns CV profile quality and identity resolution. Role-match analytics stay out of this page until the later matrix stage.
                </p>
              </div>
              <div className="hero-actions">
                <button
                  className="primary-button"
                  type="button"
                  disabled={!canBuildCvProfiles || actionKey === 'cv-build'}
                  onClick={() => void handleBuildCvEvidence(false)}
                >
                  {actionKey === 'cv-build' ? 'Building...' : 'Build CV profiles'}
                </button>
                <button
                  className="secondary-button"
                  type="button"
                  disabled={!canRebuildCvProfiles || actionKey === 'cv-rebuild'}
                  onClick={() => void handleBuildCvEvidence(true)}
                >
                  {actionKey === 'cv-rebuild' ? 'Rebuilding...' : 'Rebuild CV profiles'}
                </button>
              </div>
            </div>

            <div className="summary-grid">
              <article className="summary-card">
                <span className="summary-label">Parsed CV sources</span>
                <strong>{cvStatus.parsed_cv_sources}</strong>
                <p>{cvStatus.total_cv_sources} total CV source(s) are attached.</p>
              </article>
              <article className="summary-card">
                <span className="summary-label">Matched profiles</span>
                <strong>{cvStatus.matched_count}</strong>
                <p>{cvStatus.strong_profile_count} strong and {cvStatus.usable_profile_count} usable profile(s).</p>
              </article>
              <article className="summary-card">
                <span className="summary-label">Unresolved sources</span>
                <strong>{cvStatus.unresolved_source_count}</strong>
                <p>{cvStatus.ambiguous_count} ambiguous and {cvStatus.unmatched_count} unmatched profile(s).</p>
              </article>
              <article className="summary-card">
                <span className="summary-label">Coverage gaps</span>
                <strong>{cvStatus.employees_without_cv_evidence_count}</strong>
                <p>{cvStatus.employees_with_cv_evidence_count} employee(s) already have CV evidence.</p>
              </article>
            </div>

            {cvQueuesError ? (
              <ErrorState compact title="CV review queues failed to load" description={cvQueuesError} onRetry={() => void loadCvQueues()} />
            ) : null}

            {isCvQueuesLoading && unmatchedProfiles === null ? (
              <LoadingState
                title="Loading CV review queues"
                description="Fetching unmatched profiles, review items, and employee coverage gaps."
                compact
              />
            ) : null}

            {unmatchedProfiles !== null && reviewProfiles !== null && employeesWithoutEvidence !== null ? (
              <>
                <section className="detail-stack">
                  <div className="panel-heading">
                    <span className="section-tag">Unmatched CVs</span>
                    <h3>Profiles that still need identity resolution</h3>
                    <p>These profiles did not land on a single employee match yet.</p>
                  </div>
                  {unmatchedProfiles.length > 0 ? (
                    <div className="review-card-grid">
                      {unmatchedProfiles.map((profile) => (
                        <QueueProfileCard
                          key={profile.source_uuid}
                          profile={profile}
                          title="Unmatched"
                          description="Resolve this CV to an employee before relying on downstream evidence counts."
                          actionLabel="Resolve match"
                          onResolve={openCvResolution}
                          onViewEvidence={openEmployeeEvidence}
                        />
                      ))}
                    </div>
                  ) : (
                    <EmptyState
                      title="No unmatched CVs"
                      description="Every processed CV profile currently resolves to an employee or lives in another review queue."
                    />
                  )}
                </section>

                <section className="detail-stack">
                  <div className="panel-heading">
                    <span className="section-tag">Review items</span>
                    <h3>Low-confidence or sparse profiles</h3>
                    <p>These profiles need operator judgement before the workspace moves confidently into blueprint work.</p>
                  </div>
                  {reviewProfiles.length > 0 ? (
                    <div className="review-card-grid">
                      {reviewProfiles.map((profile) => (
                        <QueueProfileCard
                          key={profile.source_uuid}
                          profile={profile}
                          title="Needs review"
                          description="Review the profile quality and resolve identity if needed."
                          actionLabel="Review and resolve"
                          onResolve={openCvResolution}
                          onViewEvidence={openEmployeeEvidence}
                        />
                      ))}
                    </div>
                  ) : (
                    <EmptyState
                      title="No extra review items"
                      description="The CV review queue is currently empty beyond the unmatched profiles above."
                    />
                  )}
                </section>

                <section className="detail-stack">
                  <div className="panel-heading">
                    <span className="section-tag">Coverage gaps</span>
                    <h3>Employees without CV evidence</h3>
                    <p>Use this list to see who still lacks CV-derived evidence after the current build cycle.</p>
                  </div>
                  {employeesWithoutEvidence.length > 0 ? (
                    <div className="review-card-grid">
                      {employeesWithoutEvidence.map((item) => (
                        <article key={item.employee_uuid} className="review-card">
                          <div className="review-card-head">
                            <div>
                              <span className="section-tag">Missing evidence</span>
                              <h4>{item.full_name}</h4>
                              <p>{item.current_title || 'No current title recorded'}</p>
                            </div>
                            <StatusChip status={item.latest_profile_status || 'action_required'} />
                          </div>

                          <div className="review-pill-row">
                            <span className="quiet-pill">{item.cv_profile_count} profile(s)</span>
                            <span className="quiet-pill">{item.cv_evidence_row_count} evidence row(s)</span>
                          </div>

                          {item.review_reasons.length > 0 ? (
                            <ul className="helper-list helper-list-compact">
                              {item.review_reasons.map((reason) => (
                                <li key={reason}>{describeCvReviewReason(reason).label}</li>
                              ))}
                            </ul>
                          ) : item.review_reason ? (
                            <p className="form-helper-copy">{describeCvReviewReason(item.review_reason).label}</p>
                          ) : null}

                          {item.warnings.length > 0 ? (
                            <ul className="helper-list helper-list-compact">
                              {item.warnings.map((warning) => (
                                <li key={warning}>{warning}</li>
                              ))}
                            </ul>
                          ) : null}

                          <div className="source-action-group">
                            <button
                              className="secondary-button source-action-button"
                              type="button"
                              onClick={() => openEmployeeEvidence(item.employee_uuid, item.full_name)}
                            >
                              View evidence
                            </button>
                          </div>
                        </article>
                      ))}
                    </div>
                  ) : (
                    <EmptyState
                      title="No employee coverage gaps"
                      description="Every imported employee currently has at least one CV evidence row recorded."
                    />
                  )}
                </section>
              </>
            ) : null}
          </div>
        ) : null}
      </section>

      <SlideOverPanel
        title={
          inspection?.kind === 'parsed_source' ? 'Parsed source detail'
            : inspection?.kind === 'csv_preview' ? 'CSV preview: ' + (inspection as { sourceTitle: string }).sourceTitle
            : inspection?.kind === 'employee_evidence' ? (inspection as { employeeName: string }).employeeName + ' evidence'
            : inspection?.kind === 'cv_resolution' ? 'Resolve ' + ((inspection as { profile: { source_title?: string } }).profile.source_title || 'CV')
            : 'Inspection'
        }
        open={inspection !== null}
        onClose={() => setInspection(null)}
        wide
      >

          {inspectionError ? (
            <ErrorState compact title="Inspection failed to load" description={inspectionError} />
          ) : null}

          {inspectionLoading ? (
            <LoadingState
              title="Loading inspection detail"
              description="Fetching the selected detail view for this stage."
              compact
            />
          ) : null}

          {parsedSourceDetail ? (
            <div className="detail-stack">
              <div className="detail-meta-grid">
                <div>
                  <span className="summary-label">Source kind</span>
                  <strong>{getSourceKindLabel(parsedSourceDetail.parsed_source.source_kind)}</strong>
                </div>
                <div>
                  <span className="summary-label">Transport</span>
                  <strong>{getSourceTransportLabel(parsedSourceDetail.source.transport)}</strong>
                </div>
                <div>
                  <span className="summary-label">Parser</span>
                  <strong>{parsedSourceDetail.parsed_source.parser_name || 'Unknown parser'}</strong>
                </div>
                <div>
                  <span className="summary-label">Updated</span>
                  <strong>{formatDateTime(parsedSourceDetail.parsed_source.updated_at)}</strong>
                </div>
              </div>

              <div className="review-card-grid">
                <article className="review-card">
                  <div className="review-card-head">
                    <div>
                      <span className="section-tag">Original source</span>
                      <h4>{parsedSourceDetail.source.title || 'Untitled source'}</h4>
                      <p>{getSourceKindLabel(parsedSourceDetail.source.source_kind)}</p>
                    </div>
                    <StatusChip status={parsedSourceDetail.source.status || 'parsed'} />
                  </div>
                  <ul className="helper-list helper-list-compact">
                    <li>Transport: {getSourceTransportLabel(parsedSourceDetail.source.transport)}</li>
                    <li>Language: {parsedSourceDetail.source.language_code || 'Not set'}</li>
                    <li>Media: {parsedSourceDetail.source.media_filename || 'No media filename recorded'}</li>
                    <li>URL: {parsedSourceDetail.source.external_url || 'No external URL'}</li>
                    <li>Notes: {parsedSourceDetail.source.notes || 'No notes recorded'}</li>
                  </ul>
                  <div className="source-action-group">
                    <AppLink className="secondary-button link-button source-action-button" to={buildScopedWorkspacePath('sources')}>
                      Open in Sources
                    </AppLink>
                  </div>
                </article>

                <article className="review-card">
                  <div className="review-card-head">
                    <div>
                      <span className="section-tag">Parse metadata</span>
                      <h4>Warnings and parser context</h4>
                      <p>Backend metadata returned for this parsed-source record.</p>
                    </div>
                  </div>

                  {readStringArray(parsedSourceDetail.parsed_source.metadata.warnings).length > 0 ? (
                    <ul className="helper-list helper-list-compact">
                      {readStringArray(parsedSourceDetail.parsed_source.metadata.warnings).map((warning) => (
                        <li key={warning}>{warning}</li>
                      ))}
                    </ul>
                  ) : (
                    <p className="form-helper-copy">No warning entries were returned inside parse metadata.</p>
                  )}

                  {buildMetadataEntries(parsedSourceDetail.parsed_source.metadata).filter((item) => item.key !== 'warnings').length > 0 ? (
                    <div className="detail-kv-list">
                      {buildMetadataEntries(parsedSourceDetail.parsed_source.metadata)
                        .filter((item) => item.key !== 'warnings')
                        .map((item) => (
                          <div key={item.key} className="detail-kv-item">
                            <span className="summary-label">{item.label}</span>
                            {isJsonBlob(item.value) ? (
                              <pre className="code-block code-block-compact">{item.value}</pre>
                            ) : (
                              <strong>{item.value}</strong>
                            )}
                          </div>
                        ))}
                    </div>
                  ) : null}
                </article>
              </div>

              <div className="review-pill-row">
                <span className="quiet-pill">{parsedSourceDetail.parsed_source.chunk_count} chunk(s)</span>
                <span className="quiet-pill">{parsedSourceDetail.parsed_source.word_count} word(s)</span>
                <span className="quiet-pill">{parsedSourceDetail.parsed_source.warning_count} warning(s)</span>
                <span className="quiet-pill">Vector {humanizeToken(parsedSourceDetail.parsed_source.vector_index_status || 'pending')}</span>
              </div>

              {parsedSourceDetail.source.parse_error ? (
                <div className="inline-banner inline-banner-warn">
                  <strong>Latest parse error</strong>
                  <span>{parsedSourceDetail.source.parse_error}</span>
                </div>
              ) : null}

              <div className="detail-stack">
                <strong>Extracted text</strong>
                <pre className="code-block">{parsedSourceDetail.extracted_text || 'No extracted text returned by the backend.'}</pre>
              </div>

              <div className="detail-stack">
                <strong>Chunks</strong>
                {parsedSourceDetail.chunks.length > 0 ? (
                  <div className="review-card-grid">
                    {parsedSourceDetail.chunks.map((chunk) => (
                      <article key={`${parsedSourceDetail.parsed_source.uuid}:${chunk.chunk_index}`} className="review-card">
                        <div className="review-card-head">
                          <div>
                            <span className="section-tag">Chunk {chunk.chunk_index}</span>
                            <h4>{chunk.char_count} character(s)</h4>
                          </div>
                        </div>
                        {buildMetadataEntries(chunk.metadata).length > 0 ? (
                          <div className="detail-kv-list">
                            {buildMetadataEntries(chunk.metadata).map((item) => (
                              <div key={`${chunk.chunk_index}:${item.key}`} className="detail-kv-item">
                                <span className="summary-label">{item.label}</span>
                                {isJsonBlob(item.value) ? (
                                  <pre className="code-block code-block-compact">{item.value}</pre>
                                ) : (
                                  <strong>{item.value}</strong>
                                )}
                              </div>
                            ))}
                          </div>
                        ) : null}
                        <pre className="code-block code-block-compact">{chunk.text}</pre>
                      </article>
                    ))}
                  </div>
                ) : (
                  <p className="form-helper-copy">No chunk rows were returned for this parsed source.</p>
                )}
              </div>
            </div>
          ) : null}

          {csvPreview ? (
            <div className="detail-stack">
              <div className="detail-meta-grid">
                <div>
                  <span className="summary-label">Rows detected</span>
                  <strong>{csvPreview.row_count}</strong>
                </div>
                <div>
                  <span className="summary-label">Delimiter</span>
                  <strong>{csvPreview.delimiter || ','}</strong>
                </div>
                <div>
                  <span className="summary-label">Can parse</span>
                  <strong>{csvPreview.can_parse ? 'Yes' : 'Not yet'}</strong>
                </div>
                <div>
                  <span className="summary-label">Warnings</span>
                  <strong>{csvPreview.warnings.length}</strong>
                </div>
              </div>

              <div className="review-card-grid">
                <article className="review-card">
                  <div className="review-card-head">
                    <div>
                      <span className="section-tag">Inferred mapping</span>
                      <h4>What the parser guessed</h4>
                      <p>This is the mapping the backend inferred before operator overrides.</p>
                    </div>
                  </div>
                  {Object.keys(csvPreview.inferred_mapping).length > 0 ? (
                    <ul className="helper-list helper-list-compact">
                      {Object.entries(csvPreview.inferred_mapping).map(([targetKey, header]) => (
                        <li key={`inferred:${targetKey}`}>{humanizeToken(targetKey)}: {header}</li>
                      ))}
                    </ul>
                  ) : (
                    <p className="form-helper-copy">No inferred mapping entries were returned.</p>
                  )}
                </article>

                <article className="review-card">
                  <div className="review-card-head">
                    <div>
                      <span className="section-tag">Effective mapping</span>
                      <h4>What will actually be used</h4>
                      <p>This includes the backend's effective mapping after overrides are applied.</p>
                    </div>
                  </div>
                  {Object.keys(csvPreview.effective_mapping).length > 0 ? (
                    <ul className="helper-list helper-list-compact">
                      {Object.entries(csvPreview.effective_mapping).map(([targetKey, header]) => (
                        <li key={`effective:${targetKey}`}>{humanizeToken(targetKey)}: {header}</li>
                      ))}
                    </ul>
                  ) : (
                    <p className="form-helper-copy">No effective mapping entries were returned.</p>
                  )}
                </article>

                <article className="review-card">
                  <div className="review-card-head">
                    <div>
                      <span className="section-tag">Overrides</span>
                      <h4>Operator overrides in effect</h4>
                      <p>These are the explicit source-specific overrides currently reflected in the preview.</p>
                    </div>
                  </div>
                  {Object.keys(csvPreview.override_applied).length > 0 ? (
                    <ul className="helper-list helper-list-compact">
                      {Object.entries(csvPreview.override_applied).map(([targetKey, header]) => (
                        <li key={`override:${targetKey}`}>{humanizeToken(targetKey)}: {header}</li>
                      ))}
                    </ul>
                  ) : (
                    <p className="form-helper-copy">No operator overrides are applied yet.</p>
                  )}
                </article>
              </div>

              {csvPreview.warnings.length > 0 ? (
                <div className="inline-banner inline-banner-warn">
                  <strong>Preview warnings</strong>
                  <ul className="inline-detail-list">
                    {csvPreview.warnings.map((warning) => (
                      <li key={warning}>{warning}</li>
                    ))}
                  </ul>
                </div>
              ) : null}

              <form className="detail-stack" onSubmit={(event) => void handleCsvPreviewRefresh(event)}>
                <div className="panel-heading">
                  <span className="section-tag">Mapping override</span>
                  <h3>Adjust how CSV columns map into org context</h3>
                  <p>The override is source-specific. Refresh the preview first if you want to confirm the effective mapping before reparsing.</p>
                </div>

                {mappingTargetKeys.length > 0 ? (
                  <div className="profile-form-grid">
                    {mappingTargetKeys.map((targetKey) => (
                      <label key={targetKey} className="field-label">
                        <span>{humanizeToken(targetKey)}</span>
                        <select
                          className="select-input"
                          value={mappingDraft[targetKey] || ''}
                          onChange={(event) =>
                            setMappingDraft((currentDraft) => ({
                              ...currentDraft,
                              [targetKey]: event.target.value,
                            }))
                          }
                        >
                          <option value="">Leave empty</option>
                          {csvPreview.headers.map((header) => (
                            <option key={`${targetKey}:${header}`} value={header}>
                              {header}
                            </option>
                          ))}
                        </select>
                      </label>
                    ))}
                  </div>
                ) : (
                  <p className="form-helper-copy">No mappable target fields were returned by the preview endpoint.</p>
                )}

                {Object.keys(csvPreview.ambiguous_targets).length > 0 ? (
                  <div className="detail-stack">
                    <strong>Ambiguous targets</strong>
                    <ul className="helper-list helper-list-compact">
                      {Object.entries(csvPreview.ambiguous_targets).map(([targetKey, candidates]) => (
                        <li key={targetKey}>
                          {humanizeToken(targetKey)}: {readStringArray(candidates).join(', ') || 'No candidates returned'}
                        </li>
                      ))}
                    </ul>
                  </div>
                ) : null}

                {csvPreview.missing_targets.length > 0 ? (
                  <div className="detail-stack">
                    <strong>Missing targets</strong>
                    <ul className="helper-list helper-list-compact">
                      {csvPreview.missing_targets.map((targetKey) => (
                        <li key={targetKey}>{humanizeToken(targetKey)}</li>
                      ))}
                    </ul>
                  </div>
                ) : null}

                <div className="form-actions">
                  <button
                    className="secondary-button"
                    type="submit"
                    disabled={actionKey === `preview:${csvPreview.source_uuid}` || actionKey === `reparse:${csvPreview.source_uuid}`}
                  >
                    {actionKey === `preview:${csvPreview.source_uuid}` ? 'Refreshing...' : 'Refresh preview'}
                  </button>
                  <button
                    className="primary-button"
                    type="button"
                    disabled={actionKey === `preview:${csvPreview.source_uuid}` || actionKey === `reparse:${csvPreview.source_uuid}`}
                    onClick={() => {
                      const parsedSource = parsedSources.find((item) => item.source_uuid === csvPreview.source_uuid)
                      if (parsedSource) {
                        void handleParsedSourceReparse(parsedSource, normalizeMappingOverride(mappingDraft))
                      }
                    }}
                  >
                    {actionKey === `reparse:${csvPreview.source_uuid}` ? 'Reparsing...' : 'Reparse with mapping'}
                  </button>
                </div>
              </form>

              <div className="detail-stack">
                <strong>Sample rows</strong>
                {csvPreview.sample_rows.length > 0 ? (
                  <div className="source-table-shell">
                    <table className="source-table">
                      <thead>
                        <tr>
                          {csvPreview.headers.map((header) => (
                            <th key={header}>{header}</th>
                          ))}
                        </tr>
                      </thead>
                      <tbody>
                        {csvPreview.sample_rows.map((row, rowIndex) => (
                          <tr key={`${csvPreview.source_uuid}:sample:${rowIndex}`}>
                            {csvPreview.headers.map((header) => (
                              <td key={`${rowIndex}:${header}`}>
                                <span className="form-helper-copy">{String(row[header] ?? '')}</span>
                              </td>
                            ))}
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                ) : (
                  <p className="form-helper-copy">No sample rows were returned by the preview endpoint.</p>
                )}
              </div>
            </div>
          ) : null}

          {employeeEvidenceDetail ? (
            <div className="detail-stack">
              <div className="detail-meta-grid">
                <div>
                  <span className="summary-label">Employee</span>
                  <strong>{employeeEvidenceDetail.full_name}</strong>
                </div>
                <div>
                  <span className="summary-label">Current title</span>
                  <strong>{employeeEvidenceDetail.current_title || 'No title recorded'}</strong>
                </div>
                <div>
                  <span className="summary-label">Employee ID</span>
                  <strong>{employeeEvidenceDetail.external_employee_id || 'Not recorded'}</strong>
                </div>
                <div>
                  <span className="summary-label">Metadata fields</span>
                  <strong>{Object.keys(employeeEvidenceDetail.metadata || {}).length}</strong>
                </div>
                <div>
                  <span className="summary-label">CV profiles</span>
                  <strong>{employeeEvidenceDetail.cv_profiles.length}</strong>
                </div>
                <div>
                  <span className="summary-label">Evidence rows</span>
                  <strong>{employeeEvidenceDetail.evidence_rows.length}</strong>
                </div>
              </div>

              <section className="detail-stack">
                <strong>Imported org record</strong>
                <p className="form-helper-copy">
                  This is the employee identity data imported from the org-context source. It is separate from CV-linked evidence rows.
                </p>
                <div className="detail-kv-list">
                  <div className="detail-kv-item">
                    <span className="summary-label">Employee ID</span>
                    <strong>{employeeEvidenceDetail.external_employee_id || 'No external employee ID recorded.'}</strong>
                  </div>
                </div>
                {importedOrgFieldEntries.length > 0 ? (
                  <div className="detail-stack">
                    <span className="summary-label">Imported row fields</span>
                    <div className="detail-kv-list">
                      {importedOrgFieldEntries.map((item) => (
                        <div key={`org-field:${item.key}`} className="detail-kv-item">
                          <span className="summary-label">{item.label}</span>
                          {isJsonBlob(item.value) ? (
                            <pre className="code-block code-block-compact">{item.value}</pre>
                          ) : (
                            <strong>{item.value}</strong>
                          )}
                        </div>
                      ))}
                    </div>
                  </div>
                ) : null}
                {employeeMetadataEntries.length > 0 ? (
                  <div className="detail-stack">
                    <span className="summary-label">Stored metadata</span>
                    <div className="detail-kv-list">
                      {employeeMetadataEntries.map((item) => (
                        <div key={`employee-metadata:${item.key}`} className="detail-kv-item">
                          <span className="summary-label">{item.label}</span>
                          {isJsonBlob(item.value) ? (
                            <pre className="code-block code-block-compact">{item.value}</pre>
                          ) : (
                            <strong>{item.value}</strong>
                          )}
                        </div>
                      ))}
                    </div>
                  </div>
                ) : (
                  <p className="form-helper-copy">No stored metadata fields are recorded for this employee.</p>
                )}
              </section>

              <section className="detail-stack">
                <strong>CV availability</strong>
                <p className="form-helper-copy">
                  {(employeeEvidenceDetail.cv_availability.status || '') === 'no_cv_available'
                    ? 'No CV is currently expected for this employee. They will stay out of the action-required CV coverage gap list, but later assessments can still build questions from their role and the blueprint requirements.'
                    : employeeEvidenceDetail.cv_profiles.length > 0 || employeeEvidenceDetail.evidence_rows.length > 0
                      ? 'This employee already has a matched CV profile, so there is nothing else to attach manually here. If the card still shows Action required, rebuild the linked profile or review any pending skill items below.'
                      : 'This employee is still expected to have CV coverage unless you explicitly confirm that no CV is available yet.'}
                </p>
                <div className="review-pill-row">
                  <span className="quiet-pill">{buildEmployeeEvidenceCvLabel(employeeEvidenceDetail)}</span>
                  {employeeEvidenceDetail.cv_availability.confirmed_by ? (
                    <span className="quiet-pill">Confirmed by {employeeEvidenceDetail.cv_availability.confirmed_by}</span>
                  ) : null}
                  {employeeEvidenceDetail.cv_availability.confirmed_at ? (
                    <span className="quiet-pill">{formatDateTime(employeeEvidenceDetail.cv_availability.confirmed_at)}</span>
                  ) : null}
                </div>
                {employeeEvidenceDetail.cv_availability.note ? (
                  <p className="form-helper-copy">{employeeEvidenceDetail.cv_availability.note}</p>
                ) : null}
                <div className="source-action-group">
                  {(employeeEvidenceDetail.cv_availability.status || '') === 'no_cv_available' ? (
                    <button
                      className="secondary-button source-action-button"
                      type="button"
                      disabled={actionKey === `clear-no-cv:${employeeEvidenceDetail.employee_uuid}`}
                      onClick={() => void handleClearNoCvAvailable()}
                    >
                      {actionKey === `clear-no-cv:${employeeEvidenceDetail.employee_uuid}` ? 'Clearing...' : 'Clear no-CV mark'}
                    </button>
                  ) : employeeEvidenceDetail.cv_profiles.length === 0 && employeeEvidenceDetail.evidence_rows.length === 0 ? (
                    <button
                      className="secondary-button source-action-button"
                      type="button"
                      disabled={actionKey === `mark-no-cv:${employeeEvidenceDetail.employee_uuid}`}
                      onClick={() => void handleMarkNoCvAvailable()}
                    >
                      {actionKey === `mark-no-cv:${employeeEvidenceDetail.employee_uuid}` ? 'Saving...' : 'Mark as no CV available'}
                    </button>
                  ) : null}
                  <button
                    className="danger-button source-action-button"
                    type="button"
                    disabled={actionKey === `delete-employee:${employeeEvidenceDetail.employee_uuid}`}
                    onClick={() => void handleDeleteEmployee()}
                  >
                    {actionKey === `delete-employee:${employeeEvidenceDetail.employee_uuid}` ? 'Deleting...' : 'Delete employee'}
                  </button>
                </div>
              </section>

              {employeeEvidenceDetail.coverage_gap ? (
                <section className="detail-stack">
                  <strong>Action required</strong>
                  <p className="form-helper-copy">{buildCoverageGapSummary(employeeEvidenceDetail.coverage_gap)}</p>
                  {employeeEvidenceDetail.coverage_gap.review_reasons.length > 0 ? (
                    <ul className="helper-list helper-list-compact">
                      {employeeEvidenceDetail.coverage_gap.review_reasons.map((reason) => (
                        <li key={reason}>{describeCvReviewReason(reason).label}</li>
                      ))}
                    </ul>
                  ) : null}
                  {buildCoverageGapActions(employeeEvidenceDetail.coverage_gap).length > 0 ? (
                    <div className="detail-stack">
                      <span className="summary-label">Recommended next step</span>
                      <ul className="helper-list helper-list-compact">
                        {buildCoverageGapActions(employeeEvidenceDetail.coverage_gap).map((action) => (
                          <li key={action}>{action}</li>
                        ))}
                      </ul>
                    </div>
                  ) : null}
                  {employeeEvidenceDetail.cv_profiles.length > 0 && employeeEvidenceDetail.evidence_rows.length === 0 ? (
                    <div className="source-action-group">
                      <button
                        className="secondary-button source-action-button"
                        type="button"
                        disabled={
                          actionKey
                            === `cv-rebuild-linked:${employeeEvidenceDetail.cv_profiles.length === 1
                              ? employeeEvidenceDetail.cv_profiles[0].source_uuid
                              : employeeEvidenceDetail.employee_uuid}`
                        }
                        onClick={() => void handleRebuildEmployeeCvProfiles()}
                      >
                        {actionKey
                          === `cv-rebuild-linked:${employeeEvidenceDetail.cv_profiles.length === 1
                            ? employeeEvidenceDetail.cv_profiles[0].source_uuid
                            : employeeEvidenceDetail.employee_uuid}`
                          ? 'Rebuilding...'
                          : employeeEvidenceDetail.cv_profiles.length === 1
                            ? 'Rebuild linked CV profile'
                            : 'Rebuild linked CV profiles'}
                      </button>
                    </div>
                  ) : null}
                  {employeeEvidenceDetail.coverage_gap.warnings.length > 0 ? (
                    <div className="detail-stack">
                      <span className="summary-label">Warnings</span>
                      <ul className="helper-list helper-list-compact">
                        {employeeEvidenceDetail.coverage_gap.warnings.map((warning) => (
                          <li key={warning}>{warning}</li>
                        ))}
                      </ul>
                    </div>
                  ) : null}
                </section>
              ) : null}

              <section className="detail-stack">
                <strong>Linked CV profiles</strong>
                {employeeEvidenceDetail.cv_profiles.length > 0 ? (
                  <div className="review-card-grid">
                    {employeeEvidenceDetail.cv_profiles.map((profile) => (
                      <article key={profile.source_uuid} className="review-card">
                        <div className="review-card-head">
                          <div>
                            <span className="section-tag">Matched profile</span>
                            <h4>{profile.source_title || profile.full_name || 'CV profile'}</h4>
                            <p>{profile.profile_current_role || profile.headline || 'No headline available.'}</p>
                          </div>
                          <StatusChip status={profile.status || 'matched'} />
                        </div>
                        {profile.review_reasons.length > 0 ? (
                          <ul className="helper-list helper-list-compact">
                            {profile.review_reasons.map((reason) => (
                              <li key={reason}>{describeCvReviewReason(reason).label}</li>
                            ))}
                          </ul>
                        ) : null}
                        {renderPendingSkillCandidates(profile, 'Pending skills')}
                      </article>
                    ))}
                  </div>
                ) : (
                  <p className="form-helper-copy">
                    {employeeEvidenceDetail.coverage_gap?.review_reasons.includes('no_matched_cv_profile')
                      ? 'No matched CV profile is currently linked to this employee. Attach a CV or resolve a pending candidate profile if one belongs to them.'
                      : 'No matched CV profiles are currently linked to this employee.'}
                  </p>
                )}
              </section>

              {employeeEvidenceDetail.candidate_cv_profiles.length > 0 ? (
                <section className="detail-stack">
                  <strong>Candidate CV profiles</strong>
                  <div className="review-card-grid">
                    {employeeEvidenceDetail.candidate_cv_profiles.map((profile) => (
                      <article key={profile.source_uuid} className="review-card">
                        <div className="review-card-head">
                          <div>
                            <span className="section-tag">Candidate profile</span>
                            <h4>{profile.source_title || 'CV profile'}</h4>
                            <p>{profile.headline || profile.profile_current_role || 'Pending profile review.'}</p>
                          </div>
                          <StatusChip status={profile.status || 'action_required'} />
                        </div>
                        <div className="source-action-group">
                          <button className="secondary-button source-action-button" type="button" onClick={() => openCvResolution(profile)}>
                            Resolve match
                          </button>
                        </div>
                        {renderPendingSkillCandidates(profile, 'Pending skills')}
                      </article>
                    ))}
                  </div>
                </section>
              ) : null}

              <section className="detail-stack">
                <strong>Evidence rows</strong>
                {employeeEvidenceDetail.evidence_rows.length > 0 ? (
                  <div className="source-table-shell">
                    <table className="source-table">
                      <thead>
                        <tr>
                          <th>Skill</th>
                          <th>Current level</th>
                          <th>Confidence</th>
                          <th>Weight</th>
                          <th>Evidence</th>
                        </tr>
                      </thead>
                      <tbody>
                        {employeeEvidenceDetail.evidence_rows.map((row) => (
                          <tr key={`${employeeEvidenceDetail.employee_uuid}:${row.skill_uuid}:${row.source_uuid || 'none'}`}>
                            <td>
                              <div className="source-primary-cell">
                                <strong>{row.skill_name_en || humanizeToken(row.skill_key)}</strong>
                                <p>{row.skill_name_ru || row.skill_key}</p>
                              </div>
                            </td>
                            <td>{formatLevel(row.current_level)}</td>
                            <td>{formatScore(row.confidence)}</td>
                            <td>{formatLevel(row.weight)}</td>
                            <td>
                              <span className="form-helper-copy">{row.evidence_text || 'No evidence text recorded.'}</span>
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                ) : (
                  <p className="form-helper-copy">No structured evidence rows are recorded for this employee yet.</p>
                )}
              </section>
            </div>
          ) : null}

          {activeResolutionProfile ? (
            <form className="detail-stack" onSubmit={handleResolutionSubmit}>
              <article className="review-card">
                <div className="review-card-head">
                  <div>
                    <span className="section-tag">Profile under review</span>
                    <h4>{activeResolutionProfile.source_title || 'CV profile'}</h4>
                    <p>{activeResolutionProfile.headline || activeResolutionProfile.profile_current_role || 'No profile headline returned.'}</p>
                  </div>
                  <StatusChip status={activeResolutionProfile.status || 'action_required'} />
                </div>
                <div className="review-pill-row">
                  {buildProfileTagList(activeResolutionProfile).map((item) => (
                    <span key={item} className="quiet-pill">
                      {item}
                    </span>
                  ))}
                </div>
                {activeResolutionProfile.review_reasons.length > 0 ? (
                  <ul className="helper-list helper-list-compact">
                    {activeResolutionProfile.review_reasons.map((reason) => (
                      <li key={reason}>{describeCvReviewReason(reason).label}</li>
                    ))}
                  </ul>
                ) : null}
              </article>

              {employeesError ? (
                <ErrorState compact title="Employee list unavailable" description={employeesError} onRetry={() => void loadEmployees()} />
              ) : null}

              <div className="profile-form-grid">
                <label className="field-label">
                  <span>Employee</span>
                  <select
                    className="select-input"
                    value={resolutionDraft.employee_uuid}
                    onChange={(event) =>
                      setResolutionDraft((currentDraft) => ({
                        ...currentDraft,
                        employee_uuid: event.target.value,
                      }))
                    }
                  >
                    <option value="">Select an employee</option>
                    {sortedEmployees.map((employee) => (
                      <option key={employee.uuid} value={employee.uuid}>
                        {employee.full_name}
                        {employee.current_title ? ` · ${employee.current_title}` : ''}
                        {employee.email ? ` · ${employee.email}` : ''}
                      </option>
                    ))}
                  </select>
                </label>

                <label className="field-label">
                  <span>Operator name</span>
                  <input
                    className="text-input"
                    type="text"
                    value={resolutionDraft.operator_name}
                    onChange={(event) =>
                      setResolutionDraft((currentDraft) => ({
                        ...currentDraft,
                        operator_name: event.target.value,
                      }))
                    }
                    placeholder="Optional"
                  />
                </label>

                <label className="field-label field-span-full">
                  <span>Resolution note</span>
                  <textarea
                    className="textarea-input textarea-input-compact"
                    value={resolutionDraft.resolution_note}
                    onChange={(event) =>
                      setResolutionDraft((currentDraft) => ({
                        ...currentDraft,
                        resolution_note: event.target.value,
                      }))
                    }
                    placeholder="Optional context about why this match was chosen."
                  />
                </label>
              </div>

              {Array.isArray(activeResolutionProfile.candidate_matches) && activeResolutionProfile.candidate_matches.length > 0 ? (
                <div className="detail-stack">
                  <strong>Backend candidate matches</strong>
                  <ul className="helper-list helper-list-compact">
                    {activeResolutionProfile.candidate_matches
                      .filter((item): item is Record<string, unknown> => typeof item === 'object' && item !== null)
                      .map((candidate, index) => (
                        <li key={`${activeResolutionProfile.source_uuid}:candidate:${index}`}>{buildCandidateLabel(candidate)}</li>
                      ))}
                  </ul>
                </div>
              ) : null}

              {renderPendingSkillCandidates(activeResolutionProfile, 'Pending skills to approve')}

              <div className="form-actions">
                <button
                  className="primary-button"
                  type="submit"
                  disabled={!resolutionDraft.employee_uuid || actionKey === `resolve:${activeResolutionProfile.source_uuid}`}
                >
                  {actionKey === `resolve:${activeResolutionProfile.source_uuid}` ? 'Resolving...' : 'Resolve match'}
                </button>
              </div>
            </form>
          ) : null}
      </SlideOverPanel>
    </div>
  )
}
