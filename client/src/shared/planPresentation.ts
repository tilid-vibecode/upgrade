import type {
  PrototypeDevelopmentPlanArtifact,
  PrototypeDevelopmentPlanRun,
  PrototypeDevelopmentPlanSummaryResponse,
} from './prototypeApi'
import { humanizeToken } from './workflow'

export type PlanViewMode = 'current' | 'latest'

export interface IndividualPlanPreview {
  employeeUuid: string
  employeeName: string
  currentTitle: string
  currentRoleFit: string
  priorityGaps: string[]
  developmentActionCount: number
  status: string
  isCurrent: boolean
  generationBatchUuid: string | null
}

export function readString(value: unknown) {
  return typeof value === 'string' ? value : ''
}

export function readNumber(value: unknown) {
  return typeof value === 'number' && Number.isFinite(value) ? value : 0
}

export function readRecord(value: unknown) {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : {}
}

export function readRecordArray(value: unknown) {
  return Array.isArray(value)
    ? value.filter((item): item is Record<string, unknown> => typeof item === 'object' && item !== null && !Array.isArray(item))
    : []
}

export function readStringArray(value: unknown) {
  return Array.isArray(value)
    ? value.filter((item): item is string => typeof item === 'string' && item.trim().length > 0)
    : []
}

export function getPlanSummaryStatus(summary: PrototypeDevelopmentPlanSummaryResponse | null) {
  return readString(summary?.batch_status) || readString(summary?.team_plan_status) || 'not_started'
}

export function hasPlanSummary(summary: PrototypeDevelopmentPlanSummaryResponse | null) {
  return Boolean(summary?.team_plan_uuid)
}

export function getAvailablePlanModes(
  currentSummary: PrototypeDevelopmentPlanSummaryResponse | null,
  latestSummary: PrototypeDevelopmentPlanSummaryResponse | null,
) {
  const modes: PlanViewMode[] = []

  if (hasPlanSummary(currentSummary)) {
    modes.push('current')
  }

  if (hasPlanSummary(latestSummary)) {
    modes.push('latest')
  }

  return modes
}

export function normalizePlanViewMode(
  preferredMode: PlanViewMode,
  currentSummary: PrototypeDevelopmentPlanSummaryResponse | null,
  latestSummary: PrototypeDevelopmentPlanSummaryResponse | null,
) {
  const modes = getAvailablePlanModes(currentSummary, latestSummary)
  if (modes.includes(preferredMode)) {
    return preferredMode
  }
  if (modes.includes('current')) {
    return 'current'
  }
  if (modes.includes('latest')) {
    return 'latest'
  }
  return preferredMode
}

export function canGeneratePlans(
  plansStageStatus: string,
  hasPublishedBlueprint: boolean,
  latestMatrixRunUuid: string | null,
) {
  return hasPublishedBlueprint && Boolean(latestMatrixRunUuid) && plansStageStatus !== 'blocked' && plansStageStatus !== 'running'
}

export function getPlanGenerationBlockers(
  planStageBlockers: string[],
  hasPublishedBlueprint: boolean,
  latestMatrixRunUuid: string | null,
) {
  const blockers = [...planStageBlockers]

  if (!hasPublishedBlueprint) {
    blockers.unshift('A current published blueprint is required before generating development plans.')
  }

  if (!latestMatrixRunUuid) {
    blockers.unshift('A completed evidence matrix is required before generating development plans.')
  }

  return blockers.filter((item, index, list) => list.indexOf(item) === index)
}

export function sumActionCounts(actionCounts: Record<string, number>) {
  return Object.values(actionCounts).reduce((total, value) => total + readNumber(value), 0)
}

export function formatActionCountPreview(actionCounts: Record<string, number>) {
  const entries = Object.entries(actionCounts)
    .filter(([, value]) => readNumber(value) > 0)
    .sort((left, right) => left[0].localeCompare(right[0]))

  if (entries.length === 0) {
    return []
  }

  return entries.map(([key, value]) => `${humanizeToken(key)}: ${value}`)
}

export function getPlanRunEmployeeName(run: PrototypeDevelopmentPlanRun) {
  return (
    readString(run.plan_payload.employee_name) ||
    readString(run.recommendation_payload.employee_name) ||
    'Employee'
  )
}

export function getPlanRunCurrentTitle(run: PrototypeDevelopmentPlanRun) {
  return (
    readString(run.plan_payload.current_title) ||
    readString(run.recommendation_payload.current_title) ||
    'Current title not captured'
  )
}

export function buildIndividualPlanPreview(run: PrototypeDevelopmentPlanRun): IndividualPlanPreview {
  const planPayload = readRecord(run.plan_payload)
  const developmentActions = readRecordArray(planPayload.development_actions)

  return {
    employeeUuid: run.employee_uuid || '',
    employeeName: getPlanRunEmployeeName(run),
    currentTitle: getPlanRunCurrentTitle(run),
    currentRoleFit: readString(planPayload.current_role_fit) || 'Role-fit summary not available yet.',
    priorityGaps: readStringArray(planPayload.priority_gaps).slice(0, 3),
    developmentActionCount: developmentActions.length,
    status: run.status,
    isCurrent: run.is_current,
    generationBatchUuid: run.generation_batch_uuid,
  }
}

export function buildArtifactKey(artifact: PrototypeDevelopmentPlanArtifact) {
  return `${artifact.plan_run_uuid}:${artifact.artifact_format}:${artifact.file_uuid}`
}

export function isSamePlanLineage(
  leftSummary: PrototypeDevelopmentPlanSummaryResponse | null,
  rightSummary: PrototypeDevelopmentPlanSummaryResponse | null,
) {
  return Boolean(
    leftSummary?.team_plan_uuid &&
      rightSummary?.team_plan_uuid &&
      leftSummary.team_plan_uuid === rightSummary.team_plan_uuid,
  )
}
