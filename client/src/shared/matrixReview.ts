import type { PrototypeEmployeeRoleMatchEntry } from './prototypeApi'

const ROLE_MATCH_UNCERTAIN_THRESHOLD = 0.65

export type RoleMatchReviewStatus = 'matched' | 'uncertain' | 'unmatched'

export interface MatrixHeatmapColumn {
  column_key: string
  role_name: string
  seniority: string
  role_family: string
  skill_key: string
  skill_name_en: string
  target_level: number
  average_gap: number
  average_confidence: number
  max_priority: number
  incomplete_count: number
}

export interface MatrixHeatmapCellSummary {
  column_key: string
  skill_key: string
  gap: number
  current_level: number
  target_level: number
  confidence: number
  incompleteness_flags: string[]
}

export interface MatrixHeatmapEmployeeRow {
  employee_uuid: string
  full_name: string
  current_title: string
  best_fit_role: Record<string, unknown> | null
  total_gap_score: number
  average_confidence: number
  cells: MatrixHeatmapCellSummary[]
}

export interface MatrixEmployeeSkillRow {
  role_name: string
  skill_key: string
  skill_name_en: string
  target_level: number
  current_level: number
  gap: number
  confidence: number
  priority: number
  incompleteness_flags: string[]
  advisory_flags: string[]
}

export interface MatrixEmployeeSlice {
  employee_uuid: string
  full_name: string
  current_title: string
  best_fit_role: Record<string, unknown> | null
  adjacent_roles: Array<Record<string, unknown>>
  role_match_status: string
  top_gaps: MatrixEmployeeSkillRow[]
  skills: MatrixEmployeeSkillRow[]
  total_gap_score: number
  average_confidence: number
  insufficient_evidence_flags: string[]
  advisory_flags: string[]
  critical_gap_count: number
  insufficient_evidence_count: number
}

export interface MatrixCellDetail {
  cell_key: string
  employee_uuid: string
  employee_name: string
  current_title: string
  role_profile_uuid: string
  role_name: string
  role_family: string
  seniority: string
  role_fit_score: number
  skill_key: string
  skill_name_en: string
  skill_name_ru: string
  target_level: number
  current_level: number
  gap: number
  confidence: number
  priority: number
  evidence_source_mix: Array<Record<string, unknown>>
  evidence_rows: Array<Record<string, unknown>>
  incompleteness_flags: string[]
  advisory_flags: string[]
  provenance_snippets: Array<Record<string, unknown>>
  explanation_summary: string
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

export function buildMatrixColumnKey(roleProfileUuid: string, skillKey: string, targetLevel: number) {
  return `${roleProfileUuid}:${skillKey}:${targetLevel}`
}

export function getPrimaryRoleMatch(entry: PrototypeEmployeeRoleMatchEntry) {
  return entry.matches[0] ?? null
}

export function getAdjacentRoleMatches(entry: PrototypeEmployeeRoleMatchEntry) {
  return entry.matches.slice(1, 3)
}

export function getRoleMatchStatus(entry: PrototypeEmployeeRoleMatchEntry): RoleMatchReviewStatus {
  const primaryMatch = getPrimaryRoleMatch(entry)

  if (primaryMatch === null) {
    return 'unmatched'
  }

  if (Number(primaryMatch.fit_score) < ROLE_MATCH_UNCERTAIN_THRESHOLD) {
    return 'uncertain'
  }

  return 'matched'
}

export function sortRoleMatchEntries(entries: PrototypeEmployeeRoleMatchEntry[]) {
  const priority: Record<RoleMatchReviewStatus, number> = {
    unmatched: 0,
    uncertain: 1,
    matched: 2,
  }

  return [...entries].sort((left, right) => {
    const statusDifference = priority[getRoleMatchStatus(left)] - priority[getRoleMatchStatus(right)]
    if (statusDifference !== 0) {
      return statusDifference
    }

    return left.full_name.localeCompare(right.full_name)
  })
}

export function getMatrixBuildBlockers(matrixStageBlockers: string[], hasPublishedBlueprint: boolean) {
  const blockers = [...matrixStageBlockers]
  if (!hasPublishedBlueprint) {
    blockers.unshift('A current published blueprint is required before building the matrix.')
  }
  return blockers.filter((item, index, list) => list.indexOf(item) === index)
}

export function canBuildMatrix(matrixStageStatus: string, hasPublishedBlueprint: boolean) {
  return hasPublishedBlueprint && matrixStageStatus !== 'blocked' && matrixStageStatus !== 'running'
}

export function getRoleMatchStatusLabel(status: RoleMatchReviewStatus) {
  if (status === 'unmatched') {
    return 'No match'
  }

  if (status === 'uncertain') {
    return 'Uncertain'
  }

  return 'Matched'
}

export function getIncompletenessFlagLabel(flag: string) {
  const labels: Record<string, string> = {
    self_report_only: 'Self-report only',
    low_confidence: 'Low confidence',
    no_evidence: 'No evidence',
    low_evidence_mass: 'Low evidence mass',
    narrow_source_diversity: 'Narrow source diversity',
    not_required: 'Not required',
  }

  return labels[flag] || humanizeToken(flag)
}

export function getAdvisoryFlagLabel(flag: string) {
  const labels: Record<string, string> = {
    role_match_uncertain: 'Role match uncertain',
  }

  return labels[flag] || humanizeToken(flag)
}

export function getMatrixCellTone(
  gap: number,
  confidence: number,
  incompletenessFlags: string[],
) {
  if (incompletenessFlags.length > 0) {
    return 'is-warning'
  }

  if (gap >= 1.5) {
    return 'is-danger'
  }

  if (gap >= 0.5 || confidence < 0.55) {
    return 'is-caution'
  }

  return 'is-good'
}

export function normalizeHeatmapColumns(payload: Record<string, unknown>) {
  return readRecordArray(payload.skill_columns).map((item): MatrixHeatmapColumn => ({
    column_key: readString(item.column_key),
    role_name: readString(item.role_name),
    seniority: readString(item.seniority),
    role_family: readString(item.role_family),
    skill_key: readString(item.skill_key),
    skill_name_en: readString(item.skill_name_en),
    target_level: readNumber(item.target_level),
    average_gap: readNumber(item.average_gap),
    average_confidence: readNumber(item.average_confidence),
    max_priority: readNumber(item.max_priority),
    incomplete_count: readNumber(item.incomplete_count),
  }))
}

export function normalizeHeatmapRows(payload: Record<string, unknown>) {
  return readRecordArray(payload.employee_rows).map((item): MatrixHeatmapEmployeeRow => ({
    employee_uuid: readString(item.employee_uuid),
    full_name: readString(item.full_name),
    current_title: readString(item.current_title),
    best_fit_role: item.best_fit_role ? readRecord(item.best_fit_role) : null,
    total_gap_score: readNumber(item.total_gap_score),
    average_confidence: readNumber(item.average_confidence),
    cells: readRecordArray(item.cells).map((cell): MatrixHeatmapCellSummary => ({
      column_key: readString(cell.column_key),
      skill_key: readString(cell.skill_key),
      gap: readNumber(cell.gap),
      current_level: readNumber(cell.current_level),
      target_level: readNumber(cell.target_level),
      confidence: readNumber(cell.confidence),
      incompleteness_flags: readStringArray(cell.incompleteness_flags),
    })),
  }))
}

function normalizeEmployeeSkillRows(value: unknown) {
  return readRecordArray(value).map((item): MatrixEmployeeSkillRow => ({
    role_name: readString(item.role_name),
    skill_key: readString(item.skill_key),
    skill_name_en: readString(item.skill_name_en),
    target_level: readNumber(item.target_level),
    current_level: readNumber(item.current_level),
    gap: readNumber(item.gap),
    confidence: readNumber(item.confidence),
    priority: readNumber(item.priority),
    incompleteness_flags: readStringArray(item.incompleteness_flags),
    advisory_flags: readStringArray(item.advisory_flags),
  }))
}

export function normalizeMatrixEmployeeSlice(payload: Record<string, unknown>): MatrixEmployeeSlice {
  return {
    employee_uuid: readString(payload.employee_uuid),
    full_name: readString(payload.full_name),
    current_title: readString(payload.current_title),
    best_fit_role: payload.best_fit_role ? readRecord(payload.best_fit_role) : null,
    adjacent_roles: readRecordArray(payload.adjacent_roles),
    role_match_status: readString(payload.role_match_status),
    top_gaps: normalizeEmployeeSkillRows(payload.top_gaps),
    skills: normalizeEmployeeSkillRows(payload.skills),
    total_gap_score: readNumber(payload.total_gap_score),
    average_confidence: readNumber(payload.average_confidence),
    insufficient_evidence_flags: readStringArray(payload.insufficient_evidence_flags),
    advisory_flags: readStringArray(payload.advisory_flags),
    critical_gap_count: readNumber(payload.critical_gap_count),
    insufficient_evidence_count: readNumber(payload.insufficient_evidence_count),
  }
}

export function normalizeMatrixCells(payload: Record<string, unknown>) {
  return readRecordArray(payload.matrix_cells).map((item): MatrixCellDetail => ({
    cell_key: readString(item.cell_key),
    employee_uuid: readString(item.employee_uuid),
    employee_name: readString(item.employee_name),
    current_title: readString(item.current_title),
    role_profile_uuid: readString(item.role_profile_uuid),
    role_name: readString(item.role_name),
    role_family: readString(item.role_family),
    seniority: readString(item.seniority),
    role_fit_score: readNumber(item.role_fit_score),
    skill_key: readString(item.skill_key),
    skill_name_en: readString(item.skill_name_en),
    skill_name_ru: readString(item.skill_name_ru),
    target_level: readNumber(item.target_level),
    current_level: readNumber(item.current_level),
    gap: readNumber(item.gap),
    confidence: readNumber(item.confidence),
    priority: readNumber(item.priority),
    evidence_source_mix: readRecordArray(item.evidence_source_mix),
    evidence_rows: readRecordArray(item.evidence_rows),
    incompleteness_flags: readStringArray(item.incompleteness_flags),
    advisory_flags: readStringArray(item.advisory_flags),
    provenance_snippets: readRecordArray(item.provenance_snippets),
    explanation_summary: readString(item.explanation_summary),
  }))
}

export function findMatrixCellDetail(
  cells: MatrixCellDetail[],
  employeeUuid: string,
  columnKey: string,
) {
  return (
    cells.find(
      (cell) =>
        cell.employee_uuid === employeeUuid &&
        buildMatrixColumnKey(cell.role_profile_uuid, cell.skill_key, cell.target_level) === columnKey,
    ) ?? null
  )
}

export function humanizeToken(value: string) {
  if (!value) {
    return 'Unknown'
  }

  return value
    .replace(/[_-]+/g, ' ')
    .trim()
    .replace(/\b\w/g, (match) => match.toUpperCase())
}
