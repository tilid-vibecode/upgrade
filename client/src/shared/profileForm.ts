import type {
  PrototypeWorkspaceDetail,
  PrototypeWorkspaceProfileUpdateRequest,
} from './prototypeApi'

export type ChecklistChoice = 'unknown' | 'yes' | 'no'

export type ProfileSectionKey =
  | 'company_profile'
  | 'pilot_scope'
  | 'source_checklist'
  | 'operator_notes'

export type ProfileDraft = {
  company_profile: {
    company_name: string
    website_url: string
    company_description: string
    main_products_text: string
    primary_market_geography: string
    locations_text: string
    target_customers_text: string
    current_tech_stack_text: string
    planned_tech_stack_text: string
    rough_employee_count_input: string
    pilot_scope_notes: string
    notable_constraints_or_growth_plans: string
  }
  pilot_scope: {
    scope_mode: string
    departments_in_scope_text: string
    roles_in_scope_text: string
    products_in_scope_text: string
    employee_count_in_scope_input: string
    stakeholder_contact: string
    analyst_notes: string
  }
  source_checklist: {
    existing_matrix_available: ChecklistChoice
    sales_growth_plan_available: ChecklistChoice
    architecture_overview_available: ChecklistChoice
    product_notes_available: ChecklistChoice
    hr_notes_available: ChecklistChoice
    notes: string
  }
  operator_notes: string
}

export type ProfileDirtySections = Record<ProfileSectionKey, boolean>

export const PROFILE_SECTION_LABELS: Record<ProfileSectionKey, string> = {
  company_profile: 'Company profile',
  pilot_scope: 'Pilot scope',
  source_checklist: 'Source checklist',
  operator_notes: 'Operator notes',
}

const EMPTY_DIRTY_SECTIONS: ProfileDirtySections = {
  company_profile: false,
  pilot_scope: false,
  source_checklist: false,
  operator_notes: false,
}

function formatMultilineList(values: string[]) {
  return values.join('\n')
}

function parseMultilineList(value: string) {
  const seen = new Set<string>()

  return value
    .split('\n')
    .map((item) => item.trim())
    .filter((item) => {
      if (!item || seen.has(item)) {
        return false
      }

      seen.add(item)
      return true
    })
}

function formatPositiveInteger(value: number | null) {
  return typeof value === 'number' ? String(value) : ''
}

function isValidPositiveIntegerInput(value: string) {
  const trimmedValue = value.trim()
  if (!trimmedValue) {
    return true
  }

  return /^\d+$/.test(trimmedValue) && Number(trimmedValue) >= 1
}

function parsePositiveIntegerInput(value: string) {
  const trimmedValue = value.trim()
  if (!trimmedValue) {
    return null
  }

  return Number.parseInt(trimmedValue, 10)
}

function choiceFromBoolean(value: boolean | null) {
  if (value === true) {
    return 'yes'
  }

  if (value === false) {
    return 'no'
  }

  return 'unknown'
}

function booleanFromChoice(choice: ChecklistChoice) {
  if (choice === 'yes') {
    return true
  }

  if (choice === 'no') {
    return false
  }

  return null
}

function areSectionsEqual<T>(left: T, right: T) {
  return JSON.stringify(left) === JSON.stringify(right)
}

export function buildWorkspaceProfileDraft(workspace: PrototypeWorkspaceDetail): ProfileDraft {
  return {
    company_profile: {
      company_name: workspace.company_profile.company_name,
      website_url: workspace.company_profile.website_url,
      company_description: workspace.company_profile.company_description,
      main_products_text: formatMultilineList(workspace.company_profile.main_products),
      primary_market_geography: workspace.company_profile.primary_market_geography,
      locations_text: formatMultilineList(workspace.company_profile.locations),
      target_customers_text: formatMultilineList(workspace.company_profile.target_customers),
      current_tech_stack_text: formatMultilineList(workspace.company_profile.current_tech_stack),
      planned_tech_stack_text: formatMultilineList(workspace.company_profile.planned_tech_stack),
      rough_employee_count_input: formatPositiveInteger(workspace.company_profile.rough_employee_count),
      pilot_scope_notes: workspace.company_profile.pilot_scope_notes,
      notable_constraints_or_growth_plans: workspace.company_profile.notable_constraints_or_growth_plans,
    },
    pilot_scope: {
      scope_mode: workspace.pilot_scope.scope_mode,
      departments_in_scope_text: formatMultilineList(workspace.pilot_scope.departments_in_scope),
      roles_in_scope_text: formatMultilineList(workspace.pilot_scope.roles_in_scope),
      products_in_scope_text: formatMultilineList(workspace.pilot_scope.products_in_scope),
      employee_count_in_scope_input: formatPositiveInteger(workspace.pilot_scope.employee_count_in_scope),
      stakeholder_contact: workspace.pilot_scope.stakeholder_contact,
      analyst_notes: workspace.pilot_scope.analyst_notes,
    },
    source_checklist: {
      existing_matrix_available: choiceFromBoolean(workspace.source_checklist.existing_matrix_available),
      sales_growth_plan_available: choiceFromBoolean(workspace.source_checklist.sales_growth_plan_available),
      architecture_overview_available: choiceFromBoolean(workspace.source_checklist.architecture_overview_available),
      product_notes_available: choiceFromBoolean(workspace.source_checklist.product_notes_available),
      hr_notes_available: choiceFromBoolean(workspace.source_checklist.hr_notes_available),
      notes: workspace.source_checklist.notes,
    },
    operator_notes: workspace.operator_notes,
  }
}

export function getDirtyProfileSections(
  savedWorkspace: PrototypeWorkspaceDetail | null,
  draft: ProfileDraft | null,
): ProfileDirtySections {
  if (savedWorkspace === null || draft === null) {
    return EMPTY_DIRTY_SECTIONS
  }

  const savedDraft = buildWorkspaceProfileDraft(savedWorkspace)

  return {
    company_profile: !areSectionsEqual(savedDraft.company_profile, draft.company_profile),
    pilot_scope: !areSectionsEqual(savedDraft.pilot_scope, draft.pilot_scope),
    source_checklist: !areSectionsEqual(savedDraft.source_checklist, draft.source_checklist),
    operator_notes: savedDraft.operator_notes !== draft.operator_notes,
  }
}

export function hasDirtyProfileSections(sections: ProfileDirtySections) {
  return Object.values(sections).some(Boolean)
}

export function countDirtyProfileSections(sections: ProfileDirtySections) {
  return Object.values(sections).filter(Boolean).length
}

export function validateWorkspaceProfileDraft(draft: ProfileDraft) {
  const messages: string[] = []

  if (!isValidPositiveIntegerInput(draft.company_profile.rough_employee_count_input)) {
    messages.push('Rough employee count must be a whole number greater than 0 or left blank.')
  }

  if (!isValidPositiveIntegerInput(draft.pilot_scope.employee_count_in_scope_input)) {
    messages.push('Employee count in scope must be a whole number greater than 0 or left blank.')
  }

  return messages
}

export function buildWorkspaceProfileUpdateRequest(
  draft: ProfileDraft,
  dirtySections: ProfileDirtySections,
): PrototypeWorkspaceProfileUpdateRequest {
  const payload: PrototypeWorkspaceProfileUpdateRequest = {}

  if (dirtySections.company_profile) {
    payload.company_profile = {
      company_name: draft.company_profile.company_name.trim(),
      website_url: draft.company_profile.website_url.trim(),
      company_description: draft.company_profile.company_description.trim(),
      main_products: parseMultilineList(draft.company_profile.main_products_text),
      primary_market_geography: draft.company_profile.primary_market_geography.trim(),
      locations: parseMultilineList(draft.company_profile.locations_text),
      target_customers: parseMultilineList(draft.company_profile.target_customers_text),
      current_tech_stack: parseMultilineList(draft.company_profile.current_tech_stack_text),
      planned_tech_stack: parseMultilineList(draft.company_profile.planned_tech_stack_text),
      rough_employee_count: parsePositiveIntegerInput(draft.company_profile.rough_employee_count_input),
      pilot_scope_notes: draft.company_profile.pilot_scope_notes.trim(),
      notable_constraints_or_growth_plans: draft.company_profile.notable_constraints_or_growth_plans.trim(),
    }
  }

  if (dirtySections.pilot_scope) {
    payload.pilot_scope = {
      scope_mode: draft.pilot_scope.scope_mode.trim(),
      departments_in_scope: parseMultilineList(draft.pilot_scope.departments_in_scope_text),
      roles_in_scope: parseMultilineList(draft.pilot_scope.roles_in_scope_text),
      products_in_scope: parseMultilineList(draft.pilot_scope.products_in_scope_text),
      employee_count_in_scope: parsePositiveIntegerInput(draft.pilot_scope.employee_count_in_scope_input),
      stakeholder_contact: draft.pilot_scope.stakeholder_contact.trim(),
      analyst_notes: draft.pilot_scope.analyst_notes.trim(),
    }
  }

  if (dirtySections.source_checklist) {
    payload.source_checklist = {
      existing_matrix_available: booleanFromChoice(draft.source_checklist.existing_matrix_available),
      sales_growth_plan_available: booleanFromChoice(draft.source_checklist.sales_growth_plan_available),
      architecture_overview_available: booleanFromChoice(draft.source_checklist.architecture_overview_available),
      product_notes_available: booleanFromChoice(draft.source_checklist.product_notes_available),
      hr_notes_available: booleanFromChoice(draft.source_checklist.hr_notes_available),
      notes: draft.source_checklist.notes.trim(),
    }
  }

  if (dirtySections.operator_notes) {
    payload.operator_notes = draft.operator_notes.trim()
  }

  return payload
}
