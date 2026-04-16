import { apiDelete, apiFetch, apiPatch, apiPost, apiUpload, registerHeaderProvider } from './api'

// ---------------------------------------------------------------------------
// Operator token store — lightweight in-memory cache keyed by workspace slug.
// The token is returned in the workspace detail response and stored here so
// all subsequent operator API calls can include it automatically.
// ---------------------------------------------------------------------------
const _operatorTokens = new Map<string, string>()

export function setOperatorToken(workspaceSlug: string, token: string) {
  _operatorTokens.set(workspaceSlug, token)
}

export function getOperatorToken(workspaceSlug: string): string | undefined {
  return _operatorTokens.get(workspaceSlug)
}

export function operatorHeaders(workspaceSlug: string): RequestInit | undefined {
  const token = _operatorTokens.get(workspaceSlug)
  if (!token) return undefined
  return { headers: { 'X-Operator-Token': token } }
}

// Auto-inject operator token for workspace-scoped prototype API calls.
registerHeaderProvider((path) => {
  const match = path.match(/\/prototype\/workspaces\/([^/]+)/)
  if (!match) return undefined
  const slug = decodeURIComponent(match[1])
  const token = _operatorTokens.get(slug)
  if (!token) return undefined
  return { 'X-Operator-Token': token }
})


export interface PrototypeWorkspaceSummary {
  uuid: string
  name: string
  slug: string
  notes: string
  status: string
  created_at: string
  updated_at: string
}

export interface PrototypeWorkspaceCompanyProfile {
  company_name: string
  website_url: string
  company_description: string
  main_products: string[]
  primary_market_geography: string
  locations: string[]
  target_customers: string[]
  current_tech_stack: string[]
  planned_tech_stack: string[]
  rough_employee_count: number | null
  pilot_scope_notes: string
  notable_constraints_or_growth_plans: string
}

export interface PrototypeWorkspacePilotScope {
  scope_mode: string
  departments_in_scope: string[]
  roles_in_scope: string[]
  products_in_scope: string[]
  employee_count_in_scope: number | null
  stakeholder_contact: string
  analyst_notes: string
}

export interface PrototypeWorkspaceSourceChecklist {
  existing_matrix_available: boolean | null
  sales_growth_plan_available: boolean | null
  architecture_overview_available: boolean | null
  product_notes_available: boolean | null
  hr_notes_available: boolean | null
  notes: string
}

export interface PrototypeWorkspaceDetail extends PrototypeWorkspaceSummary {
  metadata_schema_version: string
  company_profile: PrototypeWorkspaceCompanyProfile
  pilot_scope: PrototypeWorkspacePilotScope
  source_checklist: PrototypeWorkspaceSourceChecklist
  operator_notes: string
  operator_token: string
}

export interface PrototypeWorkspaceProfileUpdateRequest {
  company_profile?: Partial<PrototypeWorkspaceCompanyProfile>
  pilot_scope?: Partial<PrototypeWorkspacePilotScope>
  source_checklist?: Partial<PrototypeWorkspaceSourceChecklist>
  operator_notes?: string
  notes?: string
}

export interface PrototypeWorkspaceSectionCompleteness {
  completed_fields: number
  total_fields: number
  completion_ratio: number
  missing_required_fields: string[]
  missing_recommended_fields: string[]
  is_complete: boolean
}

export interface PrototypeWorkspaceSourceRequirement {
  key: string
  label: string
  required: boolean
  required_for_parse: boolean
  required_for_roadmap_analysis: boolean
  required_for_blueprint: boolean
  required_for_evidence: boolean
  source_kinds: string[]
  required_min_count: number
  attached_count: number
  parsed_count: number
  is_satisfied: boolean
  is_parsed_ready: boolean
  notes: string[]
}

export interface PrototypeWorkspaceReadinessFlags {
  ready_for_parse: boolean
  ready_for_roadmap_analysis: boolean
  ready_for_blueprint: boolean
  ready_for_evidence: boolean
  ready_for_assessments: boolean
  ready_for_matrix: boolean
  ready_for_plans: boolean
}

export interface PrototypeWorkspaceBlueprintState {
  review_ready: boolean
  published: boolean
}

export interface PrototypeWorkspaceStageBlockers {
  context: string[]
  sources: string[]
  parse: string[]
  roadmap_analysis: string[]
  blueprint: string[]
  clarifications: string[]
  evidence: string[]
  assessments: string[]
  matrix: string[]
  plans: string[]
}

export interface PrototypeWorkspaceReadinessResponse {
  workspace: PrototypeWorkspaceDetail
  company_profile_completeness: PrototypeWorkspaceSectionCompleteness
  pilot_scope_completeness: PrototypeWorkspaceSectionCompleteness
  source_requirements: PrototypeWorkspaceSourceRequirement[]
  source_counts: Record<string, number>
  parsed_source_counts: Record<string, number>
  total_attached_sources: number
  total_parsed_sources: number
  current_stage: string
  blueprint_state: PrototypeWorkspaceBlueprintState
  stage_blockers: PrototypeWorkspaceStageBlockers
  blocking_items: string[]
  readiness: PrototypeWorkspaceReadinessFlags
}

export interface PrototypeWorkflowStage {
  key: string
  label: string
  status: string
  dependencies: string[]
  blockers: string[]
  recommended_action: string
  latest_run_uuid: string | null
  metadata: Record<string, unknown>
}

export interface PrototypeWorkflowSummary {
  current_stage_key: string
  next_stage_key: string
  total_blocker_count: number
  latest_blueprint_status: string
  blueprint_published: boolean
  latest_assessment_status: string
  assessment_completion_rate: number
  latest_matrix_status: string
  latest_plan_status: string
  latest_blueprint_run_uuid: string | null
  current_published_blueprint_run_uuid: string | null
  latest_assessment_cycle_uuid: string | null
  latest_matrix_run_uuid: string | null
  latest_team_plan_uuid: string | null
}

export interface PrototypeWorkflowStatusResponse {
  workspace: PrototypeWorkspaceDetail
  stages: PrototypeWorkflowStage[]
  summary: PrototypeWorkflowSummary
}

export type PrototypeWorkspaceSourceTransport = 'media_file' | 'inline_text' | 'external_url'

export type PrototypeWorkspaceSourceStatus = 'attached' | 'parsing' | 'parsed' | 'failed' | 'archived'

export type PrototypeMediaFileCategory = 'image' | 'document' | 'word' | 'text' | 'spreadsheet'

export type PrototypeMediaFileStatus = 'pending' | 'uploaded' | 'processing' | 'ready' | 'failed'

export interface PrototypeMediaFile {
  uuid: string
  original_filename: string
  content_type: string
  file_size: number
  file_category: PrototypeMediaFileCategory
  status: PrototypeMediaFileStatus
  error_msg: string
  processing_description: string
  has_persistent: boolean
  has_processing: boolean
  created_at: string
  updated_at: string
  uploaded_by_email: string | null
  uploaded_by_uuid: string | null
}

export interface PrototypeMediaListResponse {
  files: PrototypeMediaFile[]
  total: number
  limit: number
  offset: number
}

export interface PrototypeSignedUrlResponse {
  url: string
  expires_in_seconds: number
  variant_type: string
  file_uuid: string
}

export interface PrototypeMediaUploadResponse {
  file: PrototypeMediaFile
  signed_url: string | null
}

export interface PrototypeWorkspaceSource {
  uuid: string
  workspace_slug: string
  title: string
  notes: string
  source_kind: string
  transport: PrototypeWorkspaceSourceTransport
  media_file_uuid: string | null
  media_filename: string | null
  external_url: string
  inline_text: string
  language_code: string
  status: PrototypeWorkspaceSourceStatus
  parse_error: string
  parse_metadata: Record<string, unknown>
  archived_at: string | null
  created_at: string
  updated_at: string
}

export interface PrototypeWorkspaceSourceListResponse {
  workspace: PrototypeWorkspaceSummary
  sources: PrototypeWorkspaceSource[]
}

export interface PrototypeWorkspaceSourceCreateRequest {
  source_kind: string
  transport: PrototypeWorkspaceSourceTransport
  media_file_uuid?: string
  external_url?: string
  inline_text?: string
  title?: string
  notes?: string
  language_code?: string
}

export interface PrototypeWorkspaceSourceUpdateRequest {
  source_kind?: string
  title?: string
  notes?: string
  language_code?: string
  external_url?: string
  inline_text?: string
}

export interface PrototypeParseSourcesRequest {
  source_uuids?: string[]
  force?: boolean
}

export interface PrototypeParsedSourceResult {
  source_uuid: string
  source_kind: string
  status: string
  parse_error: string
  parse_metadata: Record<string, unknown>
}

export interface PrototypeParseSourcesResponse {
  workspace: PrototypeWorkspaceSummary
  processed: number
  results: PrototypeParsedSourceResult[]
}

export interface PrototypeParsedSourceSummary {
  uuid: string
  source_uuid: string
  source_kind: string
  source_title: string
  source_status: string
  parse_error: string
  parser_name: string
  parser_version: string
  content_type: string
  page_count: number | null
  word_count: number
  char_count: number
  chunk_count: number
  warning_count: number
  language_code: string
  vector_index_status: string
  metadata: Record<string, unknown>
  created_at: string
  updated_at: string
}

export interface PrototypeParsedSourceListResponse {
  workspace_slug: string
  parsed_sources: PrototypeParsedSourceSummary[]
}

export interface PrototypeSourceChunk {
  chunk_index: number
  char_count: number
  text: string
  metadata: Record<string, unknown>
  created_at: string
  updated_at: string
}

export interface PrototypeParsedSourceDetailResponse {
  workspace_slug: string
  parsed_source: PrototypeParsedSourceSummary
  source: PrototypeWorkspaceSource
  extracted_text: string
  chunks: PrototypeSourceChunk[]
}

export interface PrototypeSourceReparseResponse {
  workspace_slug: string
  source: PrototypeWorkspaceSource
  parsed_source: PrototypeParsedSourceSummary | null
  status: string
  parse_error: string
  parse_metadata: Record<string, unknown>
}

export interface PrototypeOrgCsvPreviewResponse {
  workspace_slug: string
  source_uuid: string
  delimiter: string
  row_count: number
  headers: string[]
  inferred_mapping: Record<string, string>
  effective_mapping: Record<string, string>
  ambiguous_targets: Record<string, string[]>
  missing_targets: string[]
  override_applied: Record<string, string>
  warnings: string[]
  sample_rows: Array<Record<string, unknown>>
  can_parse: boolean
}

export interface PrototypeEmployeeResponse {
  uuid: string
  full_name: string
  email: string
  current_title: string
  external_employee_id: string
  metadata: Record<string, unknown>
  cv_availability: PrototypeEmployeeCvAvailability
}

export interface PrototypeOrgContextSummaryResponse {
  workspace_slug: string
  employee_count: number
  org_unit_count: number
  project_count: number
  reporting_line_count: number
  parsed_source_count: number
  role_match_count: number
  skill_evidence_count: number
}

export interface PrototypePlanningContextProfile {
  company_profile: Record<string, unknown>
  tech_stack: string[]
  tech_stack_remove: string[]
  constraints: string[]
  growth_goals: string[]
  inherit_from_parent: boolean
  override_fields: string[]
}

export interface PrototypePlanningContextSummary {
  uuid: string
  name: string
  slug: string
  kind: string
  status: string
  parent_context_uuid: string | null
  child_count: number
  source_count: number
  has_blueprint: boolean
  has_roadmap_analysis: boolean
}

export interface PrototypePlanningContextParent {
  uuid: string
  name: string
  slug: string
}

export interface PrototypePlanningContextProject {
  uuid: string
  name: string
}

export interface PrototypePlanningContextSourceLink {
  uuid: string | null
  workspace_source_uuid: string
  title: string
  source_kind: string
  usage_type: string
  is_active: boolean
  include_in_blueprint: boolean
  include_in_roadmap_analysis: boolean
  origin: string
  inherited_from_context_uuid: string | null
  inherited_from_context_slug: string
  excluded_reason: string
}

export interface PrototypePlanningContextDetail {
  uuid: string
  name: string
  slug: string
  kind: string
  status: string
  description: string
  metadata: Record<string, unknown>
  parent_context: PrototypePlanningContextParent | null
  project: PrototypePlanningContextProject | null
  profile: PrototypePlanningContextProfile
  effective_profile: Record<string, unknown>
  sources: PrototypePlanningContextSourceLink[]
  created_at: string
  updated_at: string
}

export interface PrototypePlanningContextListResponse {
  workspace_slug: string
  contexts: PrototypePlanningContextSummary[]
}

export interface PrototypeWorkspaceProject {
  uuid: string
  name: string
}

export interface PrototypeWorkspaceProjectCreateRequest {
  name: string
}

export interface PrototypeWorkspaceProjectListResponse {
  workspace_slug: string
  projects: PrototypeWorkspaceProject[]
}

export interface PrototypePlanningContextCreateRequest {
  name: string
  slug: string
  kind?: string
  parent_context_uuid?: string | null
  project_uuid?: string | null
  description?: string
  metadata?: Record<string, unknown>
  profile?: Partial<PrototypePlanningContextProfile>
}

export interface PrototypePlanningContextUpdateRequest {
  name?: string
  slug?: string
  status?: string
  description?: string
  metadata?: Record<string, unknown>
  profile?: Partial<PrototypePlanningContextProfile>
}

export interface PrototypePlanningContextSourceCreateRequest {
  workspace_source_uuid: string
  usage_type?: string
  include_in_blueprint?: boolean
  include_in_roadmap_analysis?: boolean
  is_active?: boolean
}

export interface PrototypeRoadmapAnalysisRunRequest {
  force_rebuild?: boolean
}

export interface PrototypeRoadmapAnalysisTriggerResponse {
  run_uuid: string
  status: string
  message: string
}

export interface PrototypeRoadmapAnalysisRunSummary {
  uuid: string
  title: string
  status: string
  planning_context_uuid: string | null
  created_at: string
  updated_at: string
  initiative_count: number
  workstream_count: number
  bundle_count: number
  risk_count: number
  source_count: number
}

export interface PrototypeRoadmapAnalysisStatusResponse {
  has_analysis: boolean
  latest_run: PrototypeRoadmapAnalysisRunSummary | null
}

export interface PrototypeRoadmapAnalysisRun {
  uuid: string
  title: string
  status: string
  planning_context_uuid: string | null
  analysis_version: string
  source_summary: Record<string, unknown>
  input_snapshot: Record<string, unknown>
  initiatives: Array<Record<string, unknown>>
  workstreams: Array<Record<string, unknown>>
  dependencies: Array<Record<string, unknown>>
  delivery_risks: Array<Record<string, unknown>>
  capability_bundles: Array<Record<string, unknown>>
  prd_summaries: Array<Record<string, unknown>>
  clarification_questions: Array<Record<string, unknown>>
  error_message: string
  created_at: string
  updated_at: string
}

export interface PrototypeEmployeeListResponse {
  workspace_slug: string
  employees: PrototypeEmployeeResponse[]
}

export interface PrototypeCVEvidenceSourceResult {
  source_uuid: string
  source_title: string
  status: string
  evidence_quality: string
  employee_uuid: string | null
  full_name: string
  current_title: string
  matched_by: string
  match_confidence: number
  skill_evidence_count: number
  warnings: string[]
  vector_index_status: string
  reused: boolean
}

export interface PrototypeCVEvidenceBuildResponse {
  workspace_slug: string
  processed: number
  rebuilt_count: number
  reused_count: number
  status_counts: Record<string, number>
  results: PrototypeCVEvidenceSourceResult[]
  employees: PrototypeCVEvidenceSourceResult[]
}

export interface PrototypeEmployeeCVProfile {
  source_uuid: string
  source_title: string
  status: string
  evidence_quality: string
  employee_uuid: string | null
  full_name: string
  current_title: string
  matched_by: string
  match_confidence: number
  headline: string
  profile_current_role: string
  seniority: string
  role_family: string
  warnings: string[]
  candidate_matches: Array<Record<string, unknown>>
  fact_counts: Record<string, number>
  review_reasons: string[]
  pending_skill_candidates: PrototypePendingSkillCandidate[]
  vector_index_status: string
  created_at: string
  updated_at: string
}

export interface PrototypeCVEvidenceStatusResponse {
  workspace_slug: string
  total_cv_sources: number
  parsed_cv_sources: number
  pending_source_count: number
  parse_failed_count: number
  processed_profile_count: number
  matched_count: number
  ambiguous_count: number
  unmatched_count: number
  low_confidence_count: number
  extraction_failed_count: number
  strong_profile_count: number
  usable_profile_count: number
  sparse_profile_count: number
  empty_profile_count: number
  employees_with_cv_evidence_count: number
  employees_without_cv_evidence_count: number
  skill_evidence_count: number
  low_confidence_evidence_count: number
  unresolved_source_count: number
  vector_indexed_source_count: number
}

export interface PrototypeUnmatchedCVListResponse {
  workspace_slug: string
  items: PrototypeEmployeeCVProfile[]
}

export interface PrototypeCVEvidenceReviewListResponse {
  workspace_slug: string
  items: PrototypeEmployeeCVProfile[]
}

export interface PrototypeCVMatchResolutionRequest {
  employee_uuid?: string | null
  operator_name?: string
  resolution_note?: string
}

export interface PrototypeEmployeeCvAvailabilityRequest {
  operator_name?: string
  note?: string
}

export interface PrototypeEmployeeCvAvailability {
  status: string
  note: string
  confirmed_by: string
  confirmed_at: string
}

export interface PrototypePendingSkillCandidate {
  candidate_key?: string
  proposed_key?: string
  display_name_en?: string
  display_name_ru?: string
  original_terms?: string[]
  aliases?: string[]
  category?: string
  confidence_score?: number
  evidence_texts?: string[]
}

export interface PrototypePendingSkillApprovalRequest {
  candidate_key: string
  approved_name_en: string
  approved_name_ru?: string
  alias_terms?: string[]
  operator_name?: string
  approval_note?: string
}

export interface PrototypeEmployeeDeleteResponse {
  workspace_slug: string
  employee_uuid: string
  full_name: string
  detached_cv_profile_count: number
}

export interface PrototypeEmployeeWithoutCVEvidence {
  employee_uuid: string
  full_name: string
  current_title: string
  review_reason: string
  review_reasons: string[]
  related_source_uuids: string[]
  cv_profile_count: number
  cv_evidence_row_count: number
  latest_profile_status: string
  warnings: string[]
}

export interface PrototypeEmployeeCoverageGap {
  employee_uuid: string
  review_reason: string
  review_reasons: string[]
  related_source_uuids: string[]
  cv_profile_count: number
  cv_evidence_row_count: number
  latest_profile_status: string
  warnings: string[]
}

export interface PrototypeEmployeesWithoutCVEvidenceListResponse {
  workspace_slug: string
  items: PrototypeEmployeeWithoutCVEvidence[]
}

export interface PrototypeEmployeeSkillEvidenceRow {
  skill_uuid: string
  skill_key: string
  skill_name_en: string
  skill_name_ru: string
  current_level: number
  confidence: number
  weight: number
  evidence_text: string
  source_uuid: string | null
  metadata: Record<string, unknown>
}

export interface PrototypeEmployeeEvidenceDetailResponse {
  workspace_slug: string
  employee_uuid: string
  full_name: string
  current_title: string
  external_employee_id: string
  metadata: Record<string, unknown>
  cv_availability: PrototypeEmployeeCvAvailability
  coverage_gap: PrototypeEmployeeCoverageGap | null
  cv_profiles: PrototypeEmployeeCVProfile[]
  candidate_cv_profiles: PrototypeEmployeeCVProfile[]
  evidence_rows: PrototypeEmployeeSkillEvidenceRow[]
}

export interface PrototypeRoleMatchItem {
  role_name: string
  seniority: string
  fit_score: number
  reason: string
  related_initiatives: string[]
}

export interface PrototypeEmployeeRoleMatchEntry {
  employee_uuid: string
  full_name: string
  matches: PrototypeRoleMatchItem[]
}

export interface PrototypeEmployeeRoleMatchListResponse {
  workspace_slug: string
  employees: PrototypeEmployeeRoleMatchEntry[]
}

export interface PrototypeRoleLibrarySyncRequest {
  base_urls?: string[]
  max_pages?: number
}

export interface PrototypeRoleLibrarySnapshot {
  uuid: string
  provider: string
  status: string
  base_urls: string[]
  discovery_payload: Record<string, unknown>
  summary: Record<string, unknown>
  canonical_family_counts: Record<string, number>
  normalized_skill_count: number
  alias_count: number
  seed_urls_used: string[]
  quality_flags: string[]
  missing_role_families: string[]
  error_message: string
  entry_count: number
  created_at: string
  updated_at: string
}

export interface PrototypeBlueprintGenerateRequest {
  role_library_snapshot_uuid?: string
}

export interface PrototypeBlueprintClarificationUpdateRequest {
  clarification_id: string
  answer?: string
  status?: string
  note?: string
}

export interface PrototypeBlueprintReviewRequest {
  reviewer_name?: string
  review_notes?: string
  clarification_updates?: PrototypeBlueprintClarificationUpdateRequest[]
}

export interface PrototypeBlueprintApproveRequest {
  approver_name?: string
  approval_notes?: string
  clarification_updates?: PrototypeBlueprintClarificationUpdateRequest[]
}

export interface PrototypeClarificationAnswerItemRequest {
  question_uuid?: string
  clarification_id?: string
  answer_text?: string
  status?: string
  status_note?: string
  changed_target_model?: boolean
}

export interface PrototypeClarificationAnswerRequest {
  operator_name?: string
  items: PrototypeClarificationAnswerItemRequest[]
}

export interface PrototypeBlueprintRefreshRequest {
  operator_name?: string
  refresh_note?: string
  skip_employee_matching?: boolean
}

export interface PrototypeBlueprintRevisionRequest {
  operator_name?: string
  revision_reason?: string
  skip_employee_matching?: boolean
}

export interface PrototypeBlueprintPublishRequest {
  publisher_name?: string
  publish_notes?: string
}

export interface PrototypeSkillBlueprintRun {
  uuid: string
  title: string
  status: string
  role_library_snapshot_uuid: string | null
  derived_from_run_uuid: string | null
  roadmap_analysis_uuid: string | null
  planning_context_uuid: string | null
  generation_mode: string
  source_summary: Record<string, unknown>
  input_snapshot: Record<string, unknown>
  company_context: Record<string, unknown>
  roadmap_context: Array<Record<string, unknown>>
  role_candidates: Array<Record<string, unknown>>
  clarification_questions: Array<Record<string, unknown>>
  employee_role_matches: Array<Record<string, unknown>>
  required_skill_set: Array<Record<string, unknown>>
  automation_candidates: Array<Record<string, unknown>>
  occupation_map: Array<Record<string, unknown>>
  gap_summary: Record<string, unknown>
  redundancy_summary: Record<string, unknown>
  assessment_plan: Record<string, unknown>
  review_summary: Record<string, unknown>
  change_log: Array<Record<string, unknown>>
  reviewed_by: string
  review_notes: string
  reviewed_at: string | null
  approved_by: string
  approval_notes: string
  approved_at: string | null
  is_published: boolean
  published_by: string
  published_notes: string
  published_at: string | null
  clarification_cycle_uuid: string | null
  clarification_cycle_status: string
  clarification_cycle_summary: Record<string, unknown>
  approval_blocked: boolean
  latest_for_workspace: boolean
  latest_review_ready_for_workspace: boolean
  latest_approved_for_workspace: boolean
  latest_published_for_workspace: boolean
  default_for_workspace: boolean
  created_at: string
  updated_at: string
}

export interface PrototypeSkillBlueprintRunListResponse {
  workspace_slug: string
  runs: PrototypeSkillBlueprintRun[]
}

export interface PrototypeBlueprintRoadmapResponse {
  workspace_slug: string
  blueprint_uuid: string
  roadmap_context: Array<Record<string, unknown>>
}

export interface PrototypeBlueprintRoleDetailResponse {
  workspace_slug: string
  blueprint_uuid: string
  role_key: string
  role_candidate: Record<string, unknown>
}

export interface PrototypeClarificationQuestion {
  uuid: string
  cycle_uuid: string
  blueprint_uuid: string
  question_key: string
  question_text: string
  scope: string
  priority: string
  intended_respondent_type: string
  rationale: string
  evidence_refs: Array<Record<string, unknown> | string>
  impacted_roles: string[]
  impacted_initiatives: string[]
  status: string
  answer_text: string
  answered_by: string
  answered_at: string | null
  status_note: string
  changed_target_model: boolean
  effect_metadata: Record<string, unknown>
  created_at: string
  updated_at: string
}

export interface PrototypeClarificationCycle {
  uuid: string
  blueprint_uuid: string
  title: string
  status: string
  summary: Record<string, unknown>
  questions: PrototypeClarificationQuestion[]
  created_at: string
  updated_at: string
}

export interface PrototypeClarificationQuestionListResponse {
  workspace_slug: string
  blueprint_uuid: string | null
  questions: PrototypeClarificationQuestion[]
}

export interface PrototypeAssessmentGenerateRequest {
  title?: string
  selected_employee_uuids?: string[]
}

export interface PrototypeAssessmentPromptBlock {
  question_id: string
  question_type?: string
  prompt_title?: string
  why_asked?: string
  prompt?: string
}

export interface PrototypeAssessmentTargetedQuestion {
  question_id: string
  question_type: string
  skill_key: string
  skill_name_en: string
  skill_name_ru: string
  target_level: number
  why_asked: string
  prompt?: string
  optional_example_prompt?: string
}

export interface PrototypeAssessmentQuestionnairePayload {
  schema_version?: string
  introduction?: string
  questions?: Array<Record<string, unknown>>
  targeted_questions?: PrototypeAssessmentTargetedQuestion[]
  hidden_skills_prompt?: PrototypeAssessmentPromptBlock
  aspiration_prompt?: PrototypeAssessmentPromptBlock
  closing_prompt?: string
}

export interface PrototypeAssessmentTargetedAnswer {
  question_id: string
  skill_key: string
  self_rated_level: number
  answer_confidence: number
  example_text: string
  notes: string
}

export interface PrototypeAssessmentHiddenSkillAnswer {
  skill_name_en: string
  skill_name_ru: string
  self_rated_level: number
  answer_confidence: number
  example_text: string
}

export interface PrototypeAssessmentAspirationAnswer {
  target_role_family: string
  notes: string
  interest_signal: string
}

export interface PrototypeAssessmentResponsePayload {
  schema_version?: string
  final_submit?: boolean
  targeted_answers?: PrototypeAssessmentTargetedAnswer[]
  hidden_skills?: PrototypeAssessmentHiddenSkillAnswer[]
  aspiration?: PrototypeAssessmentAspirationAnswer
  confidence_statement?: string
}

export interface PrototypeAssessmentCycle {
  uuid: string
  title: string
  status: string
  blueprint_run_uuid: string | null
  planning_context_uuid: string | null
  uses_self_report: boolean
  uses_performance_reviews: boolean
  uses_feedback_360: boolean
  uses_skill_tests: boolean
  configuration: Record<string, unknown>
  result_summary: Record<string, unknown>
  pack_count: number
  created_at: string
  updated_at: string
}

export interface PrototypeAssessmentPack {
  uuid: string
  cycle_uuid: string
  employee_uuid: string
  employee_name: string
  status: string
  title: string
  questionnaire_version: string
  questionnaire_payload: PrototypeAssessmentQuestionnairePayload
  selection_summary: Record<string, unknown>
  response_payload: PrototypeAssessmentResponsePayload
  fused_summary: Record<string, unknown>
  opened_at: string | null
  submitted_at: string | null
  created_at: string
  updated_at: string
}

export interface PrototypeAssessmentPackListResponse {
  workspace_slug: string
  cycle_uuid: string
  packs: PrototypeAssessmentPack[]
}

export interface PrototypeAssessmentStatusResponse {
  workspace_slug: string
  latest_attempt_uuid: string | null
  latest_attempt_status: string
  current_cycle_uuid: string | null
  current_cycle_status: string
  blueprint_run_uuid: string | null
  planning_context_uuid: string | null
  total_employees: number
  total_packs: number
  generated_packs: number
  opened_packs: number
  submitted_packs: number
  completed_packs: number
  superseded_packs: number
  completion_rate: number
  employees_missing_packs: Array<{
    employee_uuid: string
    full_name: string
    current_title: string
  }>
  employees_with_submitted_self_assessment: number
  cycle_summary: Record<string, unknown>
}

export interface PrototypeAssessmentPackSubmitRequest {
  final_submit: boolean
  targeted_answers: PrototypeAssessmentTargetedAnswer[]
  hidden_skills: PrototypeAssessmentHiddenSkillAnswer[]
  aspiration: PrototypeAssessmentAspirationAnswer
  confidence_statement: string
}

export interface PrototypeEvidenceMatrixBuildRequest {
  title?: string
  assessment_cycle_uuid?: string
}

export interface PrototypeEvidenceMatrixRun {
  uuid: string
  title: string
  status: string
  source_type: string
  blueprint_run_uuid: string | null
  planning_context_uuid: string | null
  connection_label: string
  snapshot_key: string
  matrix_version: string
  input_snapshot: Record<string, unknown>
  summary_payload: Record<string, unknown>
  heatmap_payload: Record<string, unknown>
  risk_payload: Record<string, unknown>
  incompleteness_payload: Record<string, unknown>
  matrix_payload: Record<string, unknown>
  created_at: string
  updated_at: string
}

export interface PrototypeEvidenceMatrixSliceResponse {
  run_uuid: string
  title: string
  status: string
  matrix_version: string
  payload: Record<string, unknown>
  updated_at: string
}

export interface PrototypeDevelopmentPlanGenerateRequest {
  team_title?: string
}

export interface PrototypeDevelopmentPlanRun {
  uuid: string
  workspace_uuid: string
  employee_uuid: string | null
  blueprint_run_uuid: string | null
  matrix_run_uuid: string | null
  planning_context_uuid: string | null
  generation_batch_uuid: string | null
  title: string
  scope: string
  status: string
  is_current: boolean
  plan_version: string
  input_snapshot: Record<string, unknown>
  recommendation_payload: Record<string, unknown>
  final_report_key: string
  summary: Record<string, unknown>
  plan_payload: Record<string, unknown>
  created_at: string
  completed_at: string | null
  updated_at: string
}

export interface PrototypeDevelopmentPlanBatchResponse {
  workspace_slug: string
  team_plan: PrototypeDevelopmentPlanRun
  individual_plans: PrototypeDevelopmentPlanRun[]
}

export interface PrototypeDevelopmentPlanSummaryResponse {
  workspace_slug: string
  blueprint_run_uuid: string | null
  matrix_run_uuid: string | null
  planning_context_uuid: string | null
  generation_batch_uuid: string | null
  team_plan_uuid: string | null
  team_plan_status: string
  batch_status: string
  is_current: boolean
  individual_plan_count: number
  employee_count_in_scope: number
  completed_individual_plan_count: number
  failed_individual_plan_count: number
  missing_individual_plan_count: number
  action_counts: Record<string, number>
  updated_at: string | null
}

export interface PrototypeDevelopmentPlanSliceResponse {
  plan_uuid: string
  scope: string
  title: string
  payload: Record<string, unknown>
  updated_at: string
}

export interface PrototypeDevelopmentPlanArtifact {
  uuid: string
  workspace_uuid: string
  plan_run_uuid: string
  employee_uuid: string | null
  blueprint_run_uuid: string | null
  matrix_run_uuid: string | null
  planning_context_uuid: string | null
  generation_batch_uuid: string | null
  artifact_scope: string
  artifact_format: string
  artifact_version: string
  is_current: boolean
  title: string
  metadata: Record<string, unknown>
  file_uuid: string
  original_filename: string
  content_type: string
  file_size: number
  signed_url: string | null
  expires_in_seconds: number | null
  source_run_completed_at: string | null
  created_at: string
  updated_at: string
}

export interface PrototypeDevelopmentPlanArtifactBundleResponse {
  workspace_slug: string
  plan_uuid: string
  employee_uuid: string | null
  generation_batch_uuid: string | null
  scope: string
  title: string
  status: string
  is_current: boolean
  selected_as_current: boolean
  artifacts: PrototypeDevelopmentPlanArtifact[]
  updated_at: string
}

export interface PrototypeDevelopmentPlanArtifactListResponse {
  workspace_slug: string
  artifacts: PrototypeDevelopmentPlanArtifact[]
  total: number
}

export interface PrototypeWorkspaceCreateRequest {
  company_name: string
  notes?: string
}

export interface ServiceCheck {
  service: string
  healthy: boolean
  critical: boolean
  error?: string
}

export interface DetailedHealthResponse {
  status: 'healthy' | 'degraded' | 'unhealthy'
  critical_healthy: boolean
  all_healthy: boolean
  checks: ServiceCheck[]
}

function buildQueryString(params: Record<string, string | number | boolean | null | undefined>) {
  const searchParams = new URLSearchParams()

  Object.entries(params).forEach(([key, value]) => {
    if (value === undefined || value === null || value === '') {
      return
    }

    searchParams.set(key, String(value))
  })

  const queryString = searchParams.toString()
  return queryString ? `?${queryString}` : ''
}

export interface PrototypePlanningContextOptions {
  planningContextUuid?: string | null
}

function buildPlanningContextQuery(options?: PrototypePlanningContextOptions) {
  return buildQueryString({
    planning_context_uuid: options?.planningContextUuid,
  })
}

export async function listPrototypeWorkspaces() {
  return apiFetch<PrototypeWorkspaceSummary[]>('/api/v1/prototype/workspaces')
}

export async function createPrototypeWorkspace(body: PrototypeWorkspaceCreateRequest) {
  const result = await apiPost<PrototypeWorkspaceDetail>('/api/v1/prototype/workspaces', body)
  if (result.operator_token) setOperatorToken(result.slug, result.operator_token)
  return result
}

export async function getPrototypeWorkspace(workspaceSlug: string) {
  const result = await apiFetch<PrototypeWorkspaceDetail>(`/api/v1/prototype/workspaces/${encodeURIComponent(workspaceSlug)}`)
  if (result.operator_token) setOperatorToken(result.slug, result.operator_token)
  return result
}

export async function getPrototypeWorkspaceReadiness(
  workspaceSlug: string,
  options?: PrototypePlanningContextOptions,
) {
  const queryString = buildPlanningContextQuery(options)
  const result = await apiFetch<PrototypeWorkspaceReadinessResponse>(
    `/api/v1/prototype/workspaces/${encodeURIComponent(workspaceSlug)}/readiness${queryString}`,
  )
  if (result.workspace?.operator_token) {
    setOperatorToken(result.workspace.slug, result.workspace.operator_token)
  }
  return result
}

export async function getPrototypeWorkflowStatus(
  workspaceSlug: string,
  options?: PrototypePlanningContextOptions,
) {
  const queryString = buildPlanningContextQuery(options)
  const result = await apiFetch<PrototypeWorkflowStatusResponse>(
    `/api/v1/prototype/workspaces/${encodeURIComponent(workspaceSlug)}/workflow-status${queryString}`,
  )
  if (result.workspace?.operator_token) {
    setOperatorToken(result.workspace.slug, result.workspace.operator_token)
  }
  return result
}

export async function updatePrototypeWorkspaceProfile(
  workspaceSlug: string,
  body: PrototypeWorkspaceProfileUpdateRequest,
) {
  return apiPatch<PrototypeWorkspaceDetail>(
    `/api/v1/prototype/workspaces/${encodeURIComponent(workspaceSlug)}/profile`,
    body,
  )
}

export async function uploadPrototypeMediaFile({
  file,
  workspaceSlug,
  scope = 'prototype',
}: {
  file: File
  workspaceSlug: string
  scope?: string
}) {
  const formData = new FormData()
  formData.append('file', file)
  formData.append('scope', scope)

  return apiUpload<PrototypeMediaUploadResponse>(
    `/api/v1/prototype/workspaces/${encodeURIComponent(workspaceSlug)}/media/upload`,
    formData,
  )
}

export async function listPrototypeMediaFiles({
  fileCategory,
  status,
  workspaceSlug,
  limit = 50,
  offset = 0,
}: {
  workspaceSlug: string
  fileCategory?: PrototypeMediaFileCategory
  status?: PrototypeMediaFileStatus
  limit?: number
  offset?: number
}) {
  const queryString = buildQueryString({
    file_category: fileCategory,
    status,
    limit,
    offset,
  })

  return apiFetch<PrototypeMediaListResponse>(
    `/api/v1/prototype/workspaces/${encodeURIComponent(workspaceSlug)}/media/files${queryString}`,
  )
}

export async function getPrototypeMediaSignedUrl(fileUuid: string, workspaceSlug: string) {
  return apiFetch<PrototypeSignedUrlResponse>(
    `/api/v1/prototype/workspaces/${encodeURIComponent(workspaceSlug)}/media/files/${encodeURIComponent(fileUuid)}/signed-url`,
  )
}

export async function listPrototypeWorkspaceSources(
  workspaceSlug: string,
  { includeArchived = false }: { includeArchived?: boolean } = {},
) {
  const queryString = buildQueryString({
    include_archived: includeArchived,
  })

  return apiFetch<PrototypeWorkspaceSourceListResponse>(
    `/api/v1/prototype/workspaces/${encodeURIComponent(workspaceSlug)}/sources${queryString}`,
  )
}

export async function createPrototypeWorkspaceSource(
  workspaceSlug: string,
  body: PrototypeWorkspaceSourceCreateRequest,
) {
  return apiPost<PrototypeWorkspaceSource>(
    `/api/v1/prototype/workspaces/${encodeURIComponent(workspaceSlug)}/sources`,
    body,
  )
}

export async function updatePrototypeWorkspaceSource(
  workspaceSlug: string,
  sourceUuid: string,
  body: PrototypeWorkspaceSourceUpdateRequest,
) {
  return apiPatch<PrototypeWorkspaceSource>(
    `/api/v1/prototype/workspaces/${encodeURIComponent(workspaceSlug)}/sources/${encodeURIComponent(sourceUuid)}`,
    body,
  )
}

export async function archivePrototypeWorkspaceSource(workspaceSlug: string, sourceUuid: string) {
  return apiDelete<void>(
    `/api/v1/prototype/workspaces/${encodeURIComponent(workspaceSlug)}/sources/${encodeURIComponent(sourceUuid)}`,
  )
}

export async function getPrototypeWorkspaceSourceDownload(workspaceSlug: string, sourceUuid: string) {
  return apiFetch<PrototypeSignedUrlResponse>(
    `/api/v1/prototype/workspaces/${encodeURIComponent(workspaceSlug)}/sources/${encodeURIComponent(sourceUuid)}/download`,
  )
}

export async function parsePrototypeWorkspaceSources(
  workspaceSlug: string,
  body: PrototypeParseSourcesRequest,
) {
  return apiPost<PrototypeParseSourcesResponse>(
    `/api/v1/prototype/workspaces/${encodeURIComponent(workspaceSlug)}/parse`,
    body,
  )
}

export async function reparsePrototypeWorkspaceSource(
  workspaceSlug: string,
  sourceUuid: string,
  body?: {
    mapping_override?: Record<string, string>
  },
) {
  return apiPost<PrototypeSourceReparseResponse>(
    `/api/v1/prototype/workspaces/${encodeURIComponent(workspaceSlug)}/org-context/sources/${encodeURIComponent(sourceUuid)}/reparse`,
    body,
  )
}

export async function getPrototypeOrgContextSummary(
  workspaceSlug: string,
  options?: PrototypePlanningContextOptions,
) {
  const queryString = buildPlanningContextQuery(options)
  return apiFetch<PrototypeOrgContextSummaryResponse>(
    `/api/v1/prototype/workspaces/${encodeURIComponent(workspaceSlug)}/org-context/summary${queryString}`,
  )
}

export async function runPrototypeRoadmapAnalysis(
  workspaceSlug: string,
  body: PrototypeRoadmapAnalysisRunRequest = {},
  options?: PrototypePlanningContextOptions,
) {
  const queryString = buildPlanningContextQuery(options)
  return apiPost<PrototypeRoadmapAnalysisTriggerResponse>(
    `/api/v1/prototype/workspaces/${encodeURIComponent(workspaceSlug)}/org-context/roadmap-analysis/run${queryString}`,
    body,
  )
}

export async function getPrototypeRoadmapAnalysisStatus(
  workspaceSlug: string,
  options?: PrototypePlanningContextOptions,
) {
  const queryString = buildPlanningContextQuery(options)
  return apiFetch<PrototypeRoadmapAnalysisStatusResponse>(
    `/api/v1/prototype/workspaces/${encodeURIComponent(workspaceSlug)}/org-context/roadmap-analysis/status${queryString}`,
  )
}

export async function getPrototypeLatestRoadmapAnalysis(
  workspaceSlug: string,
  options?: PrototypePlanningContextOptions,
) {
  const queryString = buildPlanningContextQuery(options)
  return apiFetch<PrototypeRoadmapAnalysisRun>(
    `/api/v1/prototype/workspaces/${encodeURIComponent(workspaceSlug)}/org-context/roadmap-analysis/latest${queryString}`,
  )
}

export async function listPrototypeParsedSources(workspaceSlug: string) {
  return apiFetch<PrototypeParsedSourceListResponse>(
    `/api/v1/prototype/workspaces/${encodeURIComponent(workspaceSlug)}/org-context/parsed-sources`,
  )
}

export async function getPrototypeParsedSourceDetail(workspaceSlug: string, parsedSourceUuid: string) {
  return apiFetch<PrototypeParsedSourceDetailResponse>(
    `/api/v1/prototype/workspaces/${encodeURIComponent(workspaceSlug)}/org-context/parsed-sources/${encodeURIComponent(parsedSourceUuid)}`,
  )
}

export async function previewPrototypeOrgCsvSource(
  workspaceSlug: string,
  sourceUuid: string,
  body?: {
    mapping_override?: Record<string, string>
    sample_row_count?: number
  },
) {
  return apiPost<PrototypeOrgCsvPreviewResponse>(
    `/api/v1/prototype/workspaces/${encodeURIComponent(workspaceSlug)}/org-context/sources/${encodeURIComponent(sourceUuid)}/csv-preview`,
    body,
  )
}

export async function listPrototypeWorkspaceEmployees(workspaceSlug: string) {
  return apiFetch<PrototypeEmployeeListResponse>(
    `/api/v1/prototype/workspaces/${encodeURIComponent(workspaceSlug)}/org-context/employees`,
  )
}

export async function listPrototypeWorkspaceProjects(workspaceSlug: string) {
  return apiFetch<PrototypeWorkspaceProjectListResponse>(
    `/api/v1/prototype/workspaces/${encodeURIComponent(workspaceSlug)}/org-context/projects`,
  )
}

export async function createPrototypeWorkspaceProject(
  workspaceSlug: string,
  body: PrototypeWorkspaceProjectCreateRequest,
) {
  return apiPost<PrototypeWorkspaceProject>(
    `/api/v1/prototype/workspaces/${encodeURIComponent(workspaceSlug)}/org-context/projects`,
    body,
  )
}

export async function buildPrototypeCVEvidence(
  workspaceSlug: string,
  body: {
    source_uuids?: string[]
  } = {},
) {
  return apiPost<PrototypeCVEvidenceBuildResponse>(
    `/api/v1/prototype/workspaces/${encodeURIComponent(workspaceSlug)}/org-context/cv-evidence/build`,
    body,
  )
}

export async function rebuildPrototypeCVEvidence(
  workspaceSlug: string,
  body: {
    source_uuids?: string[]
  } = {},
) {
  return apiPost<PrototypeCVEvidenceBuildResponse>(
    `/api/v1/prototype/workspaces/${encodeURIComponent(workspaceSlug)}/org-context/cv-evidence/rebuild`,
    body,
  )
}

export async function getPrototypeCVEvidenceStatus(workspaceSlug: string) {
  return apiFetch<PrototypeCVEvidenceStatusResponse>(
    `/api/v1/prototype/workspaces/${encodeURIComponent(workspaceSlug)}/org-context/cv-evidence/status`,
  )
}

export async function listPrototypeUnmatchedCvs(workspaceSlug: string) {
  return apiFetch<PrototypeUnmatchedCVListResponse>(
    `/api/v1/prototype/workspaces/${encodeURIComponent(workspaceSlug)}/org-context/unmatched-cvs`,
  )
}

export async function listPrototypeCVReviewItems(workspaceSlug: string) {
  return apiFetch<PrototypeCVEvidenceReviewListResponse>(
    `/api/v1/prototype/workspaces/${encodeURIComponent(workspaceSlug)}/org-context/cv-evidence/review-items`,
  )
}

export async function resolvePrototypeCvMatch(
  workspaceSlug: string,
  sourceUuid: string,
  body: PrototypeCVMatchResolutionRequest,
) {
  return apiPost<PrototypeEmployeeCVProfile>(
    `/api/v1/prototype/workspaces/${encodeURIComponent(workspaceSlug)}/org-context/cv-evidence/sources/${encodeURIComponent(sourceUuid)}/resolve-match`,
    body,
  )
}

export async function approvePrototypePendingSkill(
  workspaceSlug: string,
  sourceUuid: string,
  body: PrototypePendingSkillApprovalRequest,
) {
  return apiPost<PrototypeEmployeeCVProfile>(
    `/api/v1/prototype/workspaces/${encodeURIComponent(workspaceSlug)}/org-context/cv-evidence/sources/${encodeURIComponent(sourceUuid)}/approve-pending-skill`,
    body,
  )
}

export async function listPrototypeEmployeesWithoutCvEvidence(workspaceSlug: string) {
  return apiFetch<PrototypeEmployeesWithoutCVEvidenceListResponse>(
    `/api/v1/prototype/workspaces/${encodeURIComponent(workspaceSlug)}/org-context/employees-without-cv-evidence`,
  )
}

export async function getPrototypeEmployeeEvidenceDetail(workspaceSlug: string, employeeUuid: string) {
  return apiFetch<PrototypeEmployeeEvidenceDetailResponse>(
    `/api/v1/prototype/workspaces/${encodeURIComponent(workspaceSlug)}/org-context/employees/${encodeURIComponent(employeeUuid)}/evidence`,
  )
}

export async function markPrototypeEmployeeNoCvAvailable(
  workspaceSlug: string,
  employeeUuid: string,
  body: PrototypeEmployeeCvAvailabilityRequest = {},
) {
  return apiPost<PrototypeEmployeeCvAvailability>(
    `/api/v1/prototype/workspaces/${encodeURIComponent(workspaceSlug)}/org-context/employees/${encodeURIComponent(employeeUuid)}/mark-no-cv`,
    body,
  )
}

export async function clearPrototypeEmployeeNoCvAvailable(workspaceSlug: string, employeeUuid: string) {
  return apiPost<PrototypeEmployeeCvAvailability>(
    `/api/v1/prototype/workspaces/${encodeURIComponent(workspaceSlug)}/org-context/employees/${encodeURIComponent(employeeUuid)}/clear-no-cv`,
  )
}

export async function deletePrototypeEmployee(workspaceSlug: string, employeeUuid: string) {
  return apiDelete<PrototypeEmployeeDeleteResponse>(
    `/api/v1/prototype/workspaces/${encodeURIComponent(workspaceSlug)}/org-context/employees/${encodeURIComponent(employeeUuid)}`,
  )
}

export async function listPrototypeRoleMatches(
  workspaceSlug: string,
  options?: PrototypePlanningContextOptions,
) {
  const queryString = buildPlanningContextQuery(options)
  return apiFetch<PrototypeEmployeeRoleMatchListResponse>(
    `/api/v1/prototype/workspaces/${encodeURIComponent(workspaceSlug)}/org-context/role-matches${queryString}`,
  )
}

export async function listPrototypePlanningContexts(workspaceSlug: string) {
  return apiFetch<PrototypePlanningContextListResponse>(
    `/api/v1/prototype/workspaces/${encodeURIComponent(workspaceSlug)}/planning-contexts`,
  )
}

export async function createPrototypePlanningContext(
  workspaceSlug: string,
  body: PrototypePlanningContextCreateRequest,
) {
  return apiPost<PrototypePlanningContextDetail>(
    `/api/v1/prototype/workspaces/${encodeURIComponent(workspaceSlug)}/planning-contexts`,
    body,
  )
}

export async function getPrototypePlanningContextDetail(workspaceSlug: string, contextSlug: string) {
  return apiFetch<PrototypePlanningContextDetail>(
    `/api/v1/prototype/workspaces/${encodeURIComponent(workspaceSlug)}/planning-contexts/${encodeURIComponent(contextSlug)}`,
  )
}

export async function updatePrototypePlanningContext(
  workspaceSlug: string,
  contextSlug: string,
  body: PrototypePlanningContextUpdateRequest,
) {
  return apiPatch<PrototypePlanningContextDetail>(
    `/api/v1/prototype/workspaces/${encodeURIComponent(workspaceSlug)}/planning-contexts/${encodeURIComponent(contextSlug)}`,
    body,
  )
}

export async function addPrototypePlanningContextSource(
  workspaceSlug: string,
  contextSlug: string,
  body: PrototypePlanningContextSourceCreateRequest,
) {
  return apiPost<PrototypePlanningContextSourceLink>(
    `/api/v1/prototype/workspaces/${encodeURIComponent(workspaceSlug)}/planning-contexts/${encodeURIComponent(contextSlug)}/sources`,
    body,
  )
}

export async function deletePrototypePlanningContextSource(
  workspaceSlug: string,
  contextSlug: string,
  sourceLinkUuid: string,
) {
  return apiDelete<void>(
    `/api/v1/prototype/workspaces/${encodeURIComponent(workspaceSlug)}/planning-contexts/${encodeURIComponent(contextSlug)}/sources/${encodeURIComponent(sourceLinkUuid)}`,
  )
}

export async function syncPrototypeRoleLibrary(
  workspaceSlug: string,
  body: PrototypeRoleLibrarySyncRequest,
) {
  return apiPost<PrototypeRoleLibrarySnapshot>(
    `/api/v1/prototype/workspaces/${encodeURIComponent(workspaceSlug)}/role-library/sync`,
    body,
  )
}

export async function getPrototypeLatestRoleLibrarySnapshot(workspaceSlug: string) {
  return apiFetch<PrototypeRoleLibrarySnapshot>(
    `/api/v1/prototype/workspaces/${encodeURIComponent(workspaceSlug)}/role-library/latest`,
  )
}

export async function generatePrototypeBlueprint(
  workspaceSlug: string,
  body: PrototypeBlueprintGenerateRequest = {},
  options?: PrototypePlanningContextOptions,
) {
  const queryString = buildPlanningContextQuery(options)
  return apiPost<PrototypeSkillBlueprintRun>(
    `/api/v1/prototype/workspaces/${encodeURIComponent(workspaceSlug)}/blueprint/generate${queryString}`,
    body,
  )
}

export async function getPrototypeLatestBlueprintRun(
  workspaceSlug: string,
  options?: PrototypePlanningContextOptions,
) {
  const queryString = buildPlanningContextQuery(options)
  return apiFetch<PrototypeSkillBlueprintRun>(
    `/api/v1/prototype/workspaces/${encodeURIComponent(workspaceSlug)}/blueprint/latest${queryString}`,
  )
}

export async function getPrototypeCurrentBlueprintRun(
  workspaceSlug: string,
  options?: PrototypePlanningContextOptions,
) {
  const queryString = buildPlanningContextQuery(options)
  return apiFetch<PrototypeSkillBlueprintRun>(
    `/api/v1/prototype/workspaces/${encodeURIComponent(workspaceSlug)}/blueprint/current${queryString}`,
  )
}

export async function listPrototypeBlueprintRuns(
  workspaceSlug: string,
  options?: PrototypePlanningContextOptions,
) {
  const queryString = buildPlanningContextQuery(options)
  return apiFetch<PrototypeSkillBlueprintRunListResponse>(
    `/api/v1/prototype/workspaces/${encodeURIComponent(workspaceSlug)}/blueprint/runs${queryString}`,
  )
}

export async function getPrototypeBlueprintRun(workspaceSlug: string, blueprintUuid: string) {
  return apiFetch<PrototypeSkillBlueprintRun>(
    `/api/v1/prototype/workspaces/${encodeURIComponent(workspaceSlug)}/blueprint/${encodeURIComponent(blueprintUuid)}`,
  )
}

export async function reviewPrototypeBlueprintRun(
  workspaceSlug: string,
  blueprintUuid: string,
  body: PrototypeBlueprintReviewRequest,
) {
  return apiPost<PrototypeSkillBlueprintRun>(
    `/api/v1/prototype/workspaces/${encodeURIComponent(workspaceSlug)}/blueprint/${encodeURIComponent(blueprintUuid)}/review`,
    body,
  )
}

export async function approvePrototypeBlueprintRun(
  workspaceSlug: string,
  blueprintUuid: string,
  body: PrototypeBlueprintApproveRequest,
) {
  return apiPost<PrototypeSkillBlueprintRun>(
    `/api/v1/prototype/workspaces/${encodeURIComponent(workspaceSlug)}/blueprint/${encodeURIComponent(blueprintUuid)}/approve`,
    body,
  )
}

export async function publishPrototypeBlueprintRun(
  workspaceSlug: string,
  blueprintUuid: string,
  body: PrototypeBlueprintPublishRequest,
) {
  return apiPost<PrototypeSkillBlueprintRun>(
    `/api/v1/prototype/workspaces/${encodeURIComponent(workspaceSlug)}/blueprint/${encodeURIComponent(blueprintUuid)}/publish`,
    body,
  )
}

export async function answerPrototypeBlueprintClarifications(
  workspaceSlug: string,
  blueprintUuid: string,
  body: PrototypeClarificationAnswerRequest,
) {
  return apiPost<PrototypeSkillBlueprintRun>(
    `/api/v1/prototype/workspaces/${encodeURIComponent(workspaceSlug)}/blueprint/${encodeURIComponent(blueprintUuid)}/clarifications/answer`,
    body,
  )
}

export async function refreshPrototypeBlueprintFromClarifications(
  workspaceSlug: string,
  blueprintUuid: string,
  body: PrototypeBlueprintRefreshRequest,
) {
  return apiPost<PrototypeSkillBlueprintRun>(
    `/api/v1/prototype/workspaces/${encodeURIComponent(workspaceSlug)}/blueprint/${encodeURIComponent(blueprintUuid)}/refresh-from-clarifications`,
    body,
  )
}

export async function startPrototypeBlueprintRevision(
  workspaceSlug: string,
  blueprintUuid: string,
  body: PrototypeBlueprintRevisionRequest,
) {
  return apiPost<PrototypeSkillBlueprintRun>(
    `/api/v1/prototype/workspaces/${encodeURIComponent(workspaceSlug)}/blueprint/${encodeURIComponent(blueprintUuid)}/start-revision`,
    body,
  )
}

export async function getPrototypeLatestClarificationCycle(
  workspaceSlug: string,
  options?: PrototypePlanningContextOptions,
) {
  const queryString = buildPlanningContextQuery(options)
  return apiFetch<PrototypeClarificationCycle>(
    `/api/v1/prototype/workspaces/${encodeURIComponent(workspaceSlug)}/clarifications/latest${queryString}`,
  )
}

export async function getPrototypeOpenClarifications(
  workspaceSlug: string,
  options?: PrototypePlanningContextOptions,
) {
  const queryString = buildPlanningContextQuery(options)
  return apiFetch<PrototypeClarificationQuestionListResponse>(
    `/api/v1/prototype/workspaces/${encodeURIComponent(workspaceSlug)}/clarifications/open${queryString}`,
  )
}

export async function getPrototypeClarificationHistory(
  workspaceSlug: string,
  options?: PrototypePlanningContextOptions,
) {
  const queryString = buildPlanningContextQuery(options)
  return apiFetch<PrototypeClarificationQuestionListResponse>(
    `/api/v1/prototype/workspaces/${encodeURIComponent(workspaceSlug)}/clarifications/history${queryString}`,
  )
}

export async function getPrototypeBlueprintRoadmap(workspaceSlug: string, blueprintUuid: string) {
  return apiFetch<PrototypeBlueprintRoadmapResponse>(
    `/api/v1/prototype/workspaces/${encodeURIComponent(workspaceSlug)}/blueprint/${encodeURIComponent(blueprintUuid)}/roadmap`,
  )
}

export async function getPrototypeBlueprintRoleDetail(
  workspaceSlug: string,
  blueprintUuid: string,
  roleKey: string,
) {
  return apiFetch<PrototypeBlueprintRoleDetailResponse>(
    `/api/v1/prototype/workspaces/${encodeURIComponent(workspaceSlug)}/blueprint/${encodeURIComponent(blueprintUuid)}/roles/${encodeURIComponent(roleKey)}`,
  )
}

export async function generatePrototypeAssessmentCycle(
  workspaceSlug: string,
  body: PrototypeAssessmentGenerateRequest = {},
  options?: PrototypePlanningContextOptions,
) {
  const queryString = buildPlanningContextQuery(options)
  return apiPost<PrototypeAssessmentCycle>(
    `/api/v1/prototype/workspaces/${encodeURIComponent(workspaceSlug)}/assessments/generate${queryString}`,
    body,
  )
}

export async function regeneratePrototypeAssessmentCycle(
  workspaceSlug: string,
  body: PrototypeAssessmentGenerateRequest = {},
  options?: PrototypePlanningContextOptions,
) {
  const queryString = buildPlanningContextQuery(options)
  return apiPost<PrototypeAssessmentCycle>(
    `/api/v1/prototype/workspaces/${encodeURIComponent(workspaceSlug)}/assessments/regenerate${queryString}`,
    body,
  )
}

export async function getPrototypeLatestAssessmentCycle(
  workspaceSlug: string,
  options?: PrototypePlanningContextOptions,
) {
  const queryString = buildPlanningContextQuery(options)
  return apiFetch<PrototypeAssessmentCycle>(
    `/api/v1/prototype/workspaces/${encodeURIComponent(workspaceSlug)}/assessments/latest${queryString}`,
  )
}

export async function getPrototypeAssessmentStatus(
  workspaceSlug: string,
  options?: PrototypePlanningContextOptions,
) {
  const queryString = buildPlanningContextQuery(options)
  return apiFetch<PrototypeAssessmentStatusResponse>(
    `/api/v1/prototype/workspaces/${encodeURIComponent(workspaceSlug)}/assessments/status${queryString}`,
  )
}

export async function listPrototypeLatestAssessmentPacks(
  workspaceSlug: string,
  options?: PrototypePlanningContextOptions,
) {
  const queryString = buildPlanningContextQuery(options)
  return apiFetch<PrototypeAssessmentPackListResponse>(
    `/api/v1/prototype/workspaces/${encodeURIComponent(workspaceSlug)}/assessments/latest/packs${queryString}`,
  )
}

export async function buildPrototypeEvidenceMatrix(
  workspaceSlug: string,
  body: PrototypeEvidenceMatrixBuildRequest = {},
  options?: PrototypePlanningContextOptions,
) {
  const queryString = buildPlanningContextQuery(options)
  return apiPost<PrototypeEvidenceMatrixRun>(
    `/api/v1/prototype/workspaces/${encodeURIComponent(workspaceSlug)}/evidence-matrix/build${queryString}`,
    body,
  )
}

export async function getPrototypeLatestEvidenceMatrix(
  workspaceSlug: string,
  options?: PrototypePlanningContextOptions,
) {
  const queryString = buildPlanningContextQuery(options)
  return apiFetch<PrototypeEvidenceMatrixRun>(
    `/api/v1/prototype/workspaces/${encodeURIComponent(workspaceSlug)}/evidence-matrix/latest${queryString}`,
  )
}

export async function getPrototypeLatestEvidenceMatrixHeatmap(
  workspaceSlug: string,
  options?: PrototypePlanningContextOptions,
) {
  const queryString = buildPlanningContextQuery(options)
  return apiFetch<PrototypeEvidenceMatrixSliceResponse>(
    `/api/v1/prototype/workspaces/${encodeURIComponent(workspaceSlug)}/evidence-matrix/latest/heatmap${queryString}`,
  )
}

export async function getPrototypeLatestEvidenceMatrixCells(
  workspaceSlug: string,
  options?: PrototypePlanningContextOptions,
) {
  const queryString = buildPlanningContextQuery(options)
  return apiFetch<PrototypeEvidenceMatrixSliceResponse>(
    `/api/v1/prototype/workspaces/${encodeURIComponent(workspaceSlug)}/evidence-matrix/latest/cells${queryString}`,
  )
}

export async function getPrototypeLatestEvidenceMatrixRisks(
  workspaceSlug: string,
  options?: PrototypePlanningContextOptions,
) {
  const queryString = buildPlanningContextQuery(options)
  return apiFetch<PrototypeEvidenceMatrixSliceResponse>(
    `/api/v1/prototype/workspaces/${encodeURIComponent(workspaceSlug)}/evidence-matrix/latest/risks${queryString}`,
  )
}

export async function getPrototypeLatestEvidenceMatrixEmployee(
  workspaceSlug: string,
  employeeUuid: string,
  options?: PrototypePlanningContextOptions,
) {
  const queryString = buildPlanningContextQuery(options)
  return apiFetch<PrototypeEvidenceMatrixSliceResponse>(
    `/api/v1/prototype/workspaces/${encodeURIComponent(workspaceSlug)}/evidence-matrix/latest/employees/${encodeURIComponent(employeeUuid)}${queryString}`,
  )
}

export async function generatePrototypeDevelopmentPlans(
  workspaceSlug: string,
  body: PrototypeDevelopmentPlanGenerateRequest = {},
  options?: PrototypePlanningContextOptions,
) {
  const queryString = buildPlanningContextQuery(options)
  return apiPost<PrototypeDevelopmentPlanBatchResponse>(
    `/api/v1/prototype/workspaces/${encodeURIComponent(workspaceSlug)}/development-plans/generate${queryString}`,
    body,
  )
}

export async function getPrototypeLatestPlanSummary(
  workspaceSlug: string,
  options?: PrototypePlanningContextOptions,
) {
  const queryString = buildPlanningContextQuery(options)
  return apiFetch<PrototypeDevelopmentPlanSummaryResponse>(
    `/api/v1/prototype/workspaces/${encodeURIComponent(workspaceSlug)}/development-plans/latest-summary${queryString}`,
  )
}

export async function getPrototypeCurrentPlanSummary(
  workspaceSlug: string,
  options?: PrototypePlanningContextOptions,
) {
  const queryString = buildPlanningContextQuery(options)
  return apiFetch<PrototypeDevelopmentPlanSummaryResponse>(
    `/api/v1/prototype/workspaces/${encodeURIComponent(workspaceSlug)}/development-plans/current-summary${queryString}`,
  )
}

export async function getPrototypeLatestTeamPlan(
  workspaceSlug: string,
  options?: PrototypePlanningContextOptions,
) {
  const queryString = buildPlanningContextQuery(options)
  return apiFetch<PrototypeDevelopmentPlanRun>(
    `/api/v1/prototype/workspaces/${encodeURIComponent(workspaceSlug)}/development-plans/latest-team${queryString}`,
  )
}

export async function getPrototypeCurrentTeamPlan(
  workspaceSlug: string,
  options?: PrototypePlanningContextOptions,
) {
  const queryString = buildPlanningContextQuery(options)
  return apiFetch<PrototypeDevelopmentPlanRun>(
    `/api/v1/prototype/workspaces/${encodeURIComponent(workspaceSlug)}/development-plans/current-team${queryString}`,
  )
}

export async function listPrototypeLatestIndividualPlans(
  workspaceSlug: string,
  options?: PrototypePlanningContextOptions,
) {
  const queryString = buildPlanningContextQuery(options)
  return apiFetch<PrototypeDevelopmentPlanRun[]>(
    `/api/v1/prototype/workspaces/${encodeURIComponent(workspaceSlug)}/development-plans/latest-individual${queryString}`,
  )
}

export async function listPrototypeCurrentIndividualPlans(
  workspaceSlug: string,
  options?: PrototypePlanningContextOptions,
) {
  const queryString = buildPlanningContextQuery(options)
  return apiFetch<PrototypeDevelopmentPlanRun[]>(
    `/api/v1/prototype/workspaces/${encodeURIComponent(workspaceSlug)}/development-plans/current-individual${queryString}`,
  )
}

export async function getPrototypeLatestIndividualPlan(
  workspaceSlug: string,
  employeeUuid: string,
  options?: PrototypePlanningContextOptions,
) {
  const queryString = buildPlanningContextQuery(options)
  return apiFetch<PrototypeDevelopmentPlanRun>(
    `/api/v1/prototype/workspaces/${encodeURIComponent(workspaceSlug)}/development-plans/latest-individual/${encodeURIComponent(employeeUuid)}${queryString}`,
  )
}

export async function getPrototypeCurrentIndividualPlan(
  workspaceSlug: string,
  employeeUuid: string,
  options?: PrototypePlanningContextOptions,
) {
  const queryString = buildPlanningContextQuery(options)
  return apiFetch<PrototypeDevelopmentPlanRun>(
    `/api/v1/prototype/workspaces/${encodeURIComponent(workspaceSlug)}/development-plans/current-individual/${encodeURIComponent(employeeUuid)}${queryString}`,
  )
}

export async function getPrototypeLatestTeamActions(
  workspaceSlug: string,
  options?: PrototypePlanningContextOptions,
) {
  const queryString = buildPlanningContextQuery(options)
  return apiFetch<PrototypeDevelopmentPlanSliceResponse>(
    `/api/v1/prototype/workspaces/${encodeURIComponent(workspaceSlug)}/development-plans/latest-team/actions${queryString}`,
  )
}

export async function getPrototypeCurrentTeamActions(
  workspaceSlug: string,
  options?: PrototypePlanningContextOptions,
) {
  const queryString = buildPlanningContextQuery(options)
  return apiFetch<PrototypeDevelopmentPlanSliceResponse>(
    `/api/v1/prototype/workspaces/${encodeURIComponent(workspaceSlug)}/development-plans/current-team/actions${queryString}`,
  )
}

export async function getPrototypeLatestTeamDownloads(
  workspaceSlug: string,
  options?: PrototypePlanningContextOptions,
) {
  const queryString = buildPlanningContextQuery(options)
  return apiFetch<PrototypeDevelopmentPlanArtifactBundleResponse>(
    `/api/v1/prototype/workspaces/${encodeURIComponent(workspaceSlug)}/development-plans/latest-team/downloads${queryString}`,
  )
}

export async function getPrototypeCurrentTeamDownloads(
  workspaceSlug: string,
  options?: PrototypePlanningContextOptions,
) {
  const queryString = buildPlanningContextQuery(options)
  return apiFetch<PrototypeDevelopmentPlanArtifactBundleResponse>(
    `/api/v1/prototype/workspaces/${encodeURIComponent(workspaceSlug)}/development-plans/current-team/downloads${queryString}`,
  )
}

export async function getPrototypeLatestIndividualDownloads(
  workspaceSlug: string,
  employeeUuid: string,
  options?: PrototypePlanningContextOptions,
) {
  const queryString = buildPlanningContextQuery(options)
  return apiFetch<PrototypeDevelopmentPlanArtifactBundleResponse>(
    `/api/v1/prototype/workspaces/${encodeURIComponent(workspaceSlug)}/development-plans/latest-individual/${encodeURIComponent(employeeUuid)}/downloads${queryString}`,
  )
}

export async function getPrototypeCurrentIndividualDownloads(
  workspaceSlug: string,
  employeeUuid: string,
  options?: PrototypePlanningContextOptions,
) {
  const queryString = buildPlanningContextQuery(options)
  return apiFetch<PrototypeDevelopmentPlanArtifactBundleResponse>(
    `/api/v1/prototype/workspaces/${encodeURIComponent(workspaceSlug)}/development-plans/current-individual/${encodeURIComponent(employeeUuid)}/downloads${queryString}`,
  )
}

export async function listPrototypeWorkspacePlanArtifacts(
  workspaceSlug: string,
  options?: PrototypePlanningContextOptions,
) {
  const queryString = buildPlanningContextQuery(options)
  return apiFetch<PrototypeDevelopmentPlanArtifactListResponse>(
    `/api/v1/prototype/workspaces/${encodeURIComponent(workspaceSlug)}/artifacts${queryString}`,
  )
}

export async function listPrototypeLatestWorkspacePlanArtifacts(
  workspaceSlug: string,
  options?: PrototypePlanningContextOptions,
) {
  const queryString = buildPlanningContextQuery(options)
  return apiFetch<PrototypeDevelopmentPlanArtifactListResponse>(
    `/api/v1/prototype/workspaces/${encodeURIComponent(workspaceSlug)}/artifacts/latest${queryString}`,
  )
}

export async function getDetailedHealth() {
  return apiFetch<DetailedHealthResponse>('/api/v1/health/detailed')
}

export type PublicAssessmentPackResponse = PrototypeAssessmentPack

export async function getPublicAssessmentPack(packUuid: string) {
  return apiFetch<PublicAssessmentPackResponse>(
    `/api/v1/prototype/assessment-packs/${encodeURIComponent(packUuid)}`,
  )
}

export async function openPublicAssessmentPack(packUuid: string) {
  return apiPost<PublicAssessmentPackResponse>(
    `/api/v1/prototype/assessment-packs/${encodeURIComponent(packUuid)}/open`,
  )
}

export async function submitPublicAssessmentPack(
  packUuid: string,
  body: PrototypeAssessmentPackSubmitRequest,
) {
  return apiPost<PublicAssessmentPackResponse>(
    `/api/v1/prototype/assessment-packs/${encodeURIComponent(packUuid)}/submit`,
    body,
  )
}
