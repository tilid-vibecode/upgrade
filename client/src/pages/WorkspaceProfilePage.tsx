import { useEffect, useState, type FormEvent, type ReactNode } from 'react'

import { AppLink, useNavigationBlocker } from '../app/navigation'
import { useWorkspaceShell } from '../app/WorkspaceLayout'
import { CollapsibleHero } from '../shared/ui/CollapsibleHero'
import { getApiErrorMessage, getApiErrorMessages } from '../shared/api'
import { formatDateTime, formatPercent } from '../shared/formatters'
import {
  buildWorkspaceProfileDraft,
  buildWorkspaceProfileUpdateRequest,
  countDirtyProfileSections,
  getDirtyProfileSections,
  hasDirtyProfileSections,
  PROFILE_SECTION_LABELS,
  validateWorkspaceProfileDraft,
  type ChecklistChoice,
  type ProfileDraft,
} from '../shared/profileForm'
import {
  getPrototypeWorkspaceReadiness,
  updatePrototypeWorkspaceProfile,
  type PrototypeWorkspaceReadinessResponse,
  type PrototypeWorkspaceSectionCompleteness,
  type PrototypeWorkspaceSourceRequirement,
} from '../shared/prototypeApi'
import { humanizeToken } from '../shared/workflow'
import ErrorState from '../shared/ui/ErrorState'
import LoadingState from '../shared/ui/LoadingState'
import StatusChip from '../shared/ui/StatusChip'

type SaveState = 'idle' | 'saving' | 'saved' | 'failed'

type SectionCardProps = {
  kicker: string
  title: string
  description: string
  completeness?: PrototypeWorkspaceSectionCompleteness
  children: ReactNode
}

type HelperCardProps = {
  title: string
  items: string[]
  tone: 'required' | 'recommended'
}

type SourceRequirementItemProps = {
  requirement: PrototypeWorkspaceSourceRequirement
}

const CHECKLIST_OPTIONS: Array<{ value: ChecklistChoice; label: string }> = [
  { value: 'unknown', label: 'Unknown' },
  { value: 'yes', label: 'Yes' },
  { value: 'no', label: 'No' },
]

const BASE_SCOPE_MODE_OPTIONS = ['whole_company', 'selected_functions', 'selected_roles', 'selected_products', 'mixed_scope']

function buildSavedMessage(dirtySections: ReturnType<typeof getDirtyProfileSections>) {
  const labels = Object.entries(dirtySections)
    .filter(([, isDirty]) => isDirty)
    .map(([sectionKey]) => PROFILE_SECTION_LABELS[sectionKey as keyof typeof PROFILE_SECTION_LABELS])

  if (labels.length === 0) {
    return 'Workspace context saved.'
  }

  if (labels.length === 1) {
    return `${labels[0]} saved successfully.`
  }

  return `${labels.slice(0, -1).join(', ')}, and ${labels[labels.length - 1]} saved successfully.`
}

function SectionCard({ kicker, title, description, completeness, children }: SectionCardProps) {
  return (
    <section className="profile-section-card">
      <div className="profile-section-head">
        <div className="panel-heading">
          <span className="section-tag">{kicker}</span>
          <h3>{title}</h3>
          <p>{description}</p>
        </div>

        {completeness ? (
          <div className="profile-completeness-badge">
            <span className="summary-label">Section completeness</span>
            <strong>
              {completeness.completed_fields}/{completeness.total_fields} complete
            </strong>
            <div className="status-row">
              <StatusChip status={completeness.is_complete ? 'completed' : 'blocked'} />
              <span className="form-helper-copy">{formatPercent(completeness.completion_ratio)}</span>
            </div>
          </div>
        ) : null}
      </div>

      {children}
    </section>
  )
}

function HelperCard({ title, items, tone }: HelperCardProps) {
  if (items.length === 0) {
    return null
  }

  return (
    <article className={tone === 'required' ? 'helper-card is-required' : 'helper-card is-recommended'}>
      <strong>{title}</strong>
      <ul className="helper-list">
        {items.map((item) => (
          <li key={item}>{item}</li>
        ))}
      </ul>
    </article>
  )
}

function getRequirementStatus(requirement: PrototypeWorkspaceSourceRequirement) {
  if (!requirement.required && requirement.attached_count === 0) {
    return 'ready'
  }

  if (requirement.is_parsed_ready) {
    return 'completed'
  }

  if (requirement.is_satisfied) {
    return 'action_required'
  }

  return 'blocked'
}

function SourceRequirementItem({ requirement }: SourceRequirementItemProps) {
  return (
    <article className="requirement-item">
      <div className="requirement-item-head">
        <div>
          <strong>{requirement.label}</strong>
          <div className="requirement-tag-row">
            {requirement.required ? <span className="quiet-pill">Required</span> : <span className="quiet-pill">Optional</span>}
            {requirement.required_for_parse ? <span className="quiet-pill">Parse gate</span> : null}
            {requirement.required_for_blueprint ? <span className="quiet-pill">Blueprint gate</span> : null}
            {requirement.required_for_evidence ? <span className="quiet-pill">Evidence gate</span> : null}
          </div>
        </div>
        <StatusChip status={getRequirementStatus(requirement)} />
      </div>

      <p className="form-helper-copy">
        Attached {requirement.attached_count}/{requirement.required_min_count}
        {' '}and parsed {requirement.parsed_count}/{requirement.required_min_count}.
      </p>

      {requirement.notes.length > 0 ? (
        <ul className="helper-list helper-list-compact">
          {requirement.notes.map((note) => (
            <li key={note}>{note}</li>
          ))}
        </ul>
      ) : null}
    </article>
  )
}

export default function WorkspaceProfilePage() {
  const {
    workspace,
    workflow,
    activePlanningContext,
    planningContextOptions,
    refreshShell,
    buildScopedWorkspacePath,
  } = useWorkspaceShell()
  const [readiness, setReadiness] = useState<PrototypeWorkspaceReadinessResponse | null>(null)
  const [savedWorkspace, setSavedWorkspace] = useState<PrototypeWorkspaceReadinessResponse['workspace'] | null>(null)
  const [draft, setDraft] = useState<ProfileDraft | null>(null)
  const [isLoading, setIsLoading] = useState(true)
  const [loadError, setLoadError] = useState<unknown>(null)
  const [reloadToken, setReloadToken] = useState(0)
  const [saveState, setSaveState] = useState<SaveState>('idle')
  const [saveMessages, setSaveMessages] = useState<string[]>([])
  const [readinessWarning, setReadinessWarning] = useState<string | null>(null)

  useEffect(() => {
    let cancelled = false

    async function loadProfileContext() {
      setIsLoading(true)
      setLoadError(null)

      try {
        const response = await getPrototypeWorkspaceReadiness(workspace.slug, planningContextOptions)

        if (cancelled) {
          return
        }

        setReadiness(response)
        setSavedWorkspace(response.workspace)
        setDraft(buildWorkspaceProfileDraft(response.workspace))
        setSaveState('idle')
        setSaveMessages([])
        setReadinessWarning(null)
      } catch (requestError) {
        if (!cancelled) {
          setLoadError(requestError)
        }
      } finally {
        if (!cancelled) {
          setIsLoading(false)
        }
      }
    }

    loadProfileContext()

    return () => {
      cancelled = true
    }
  }, [planningContextOptions, workspace.slug, reloadToken])

  const dirtySections = getDirtyProfileSections(savedWorkspace, draft)
  const hasDirtyChanges = hasDirtyProfileSections(dirtySections)
  const dirtySectionCount = countDirtyProfileSections(dirtySections)
  const validationMessages = draft ? validateWorkspaceProfileDraft(draft) : []
  const canSave = draft !== null && hasDirtyChanges && validationMessages.length === 0 && saveState !== 'saving'
  const nextWorkflowStage = workflow.stages.find((stage) => stage.key === workflow.summary.next_stage_key) ?? null
  const checklistRequirements = readiness?.source_requirements
    .slice()
    .sort((left, right) => {
      const leftPriority = Number(left.required_for_parse || left.required_for_blueprint || left.required)
      const rightPriority = Number(right.required_for_parse || right.required_for_blueprint || right.required)

      if (leftPriority !== rightPriority) {
        return rightPriority - leftPriority
      }

      return left.label.localeCompare(right.label)
    }) ?? []
  const scopeModeOptions = draft?.pilot_scope.scope_mode &&
    !BASE_SCOPE_MODE_OPTIONS.includes(draft.pilot_scope.scope_mode)
    ? [...BASE_SCOPE_MODE_OPTIONS, draft.pilot_scope.scope_mode]
    : BASE_SCOPE_MODE_OPTIONS
  const nonBlockingMessage = workflow.summary.next_stage_key
    ? `Profile context is not blocking this workspace. The shell currently points to ${nextWorkflowStage?.label || humanizeToken(workflow.summary.next_stage_key)}.`
    : 'Profile context is not blocking the workflow right now. No next stage is currently flagged in the shared shell summary.'

  useNavigationBlocker(hasDirtyChanges, 'You have unsaved profile changes. Leave this page without saving?')

  const refreshPage = () => setReloadToken((value) => value + 1)

  const resetDraft = () => {
    if (savedWorkspace === null) {
      return
    }

    setDraft(buildWorkspaceProfileDraft(savedWorkspace))
    setSaveState('idle')
    setSaveMessages([])
    setReadinessWarning(null)
  }

  const updateCompanyProfileField = <Field extends keyof ProfileDraft['company_profile']>(
    field: Field,
    value: ProfileDraft['company_profile'][Field],
  ) => {
    setDraft((currentDraft) => {
      if (currentDraft === null) {
        return currentDraft
      }

      return {
        ...currentDraft,
        company_profile: {
          ...currentDraft.company_profile,
          [field]: value,
        },
      }
    })
    setSaveState('idle')
    setSaveMessages([])
    setReadinessWarning(null)
  }

  const updatePilotScopeField = <Field extends keyof ProfileDraft['pilot_scope']>(
    field: Field,
    value: ProfileDraft['pilot_scope'][Field],
  ) => {
    setDraft((currentDraft) => {
      if (currentDraft === null) {
        return currentDraft
      }

      return {
        ...currentDraft,
        pilot_scope: {
          ...currentDraft.pilot_scope,
          [field]: value,
        },
      }
    })
    setSaveState('idle')
    setSaveMessages([])
    setReadinessWarning(null)
  }

  const updateChecklistField = <Field extends keyof ProfileDraft['source_checklist']>(
    field: Field,
    value: ProfileDraft['source_checklist'][Field],
  ) => {
    setDraft((currentDraft) => {
      if (currentDraft === null) {
        return currentDraft
      }

      return {
        ...currentDraft,
        source_checklist: {
          ...currentDraft.source_checklist,
          [field]: value,
        },
      }
    })
    setSaveState('idle')
    setSaveMessages([])
    setReadinessWarning(null)
  }

  const updateOperatorNotes = (value: string) => {
    setDraft((currentDraft) => {
      if (currentDraft === null) {
        return currentDraft
      }

      return {
        ...currentDraft,
        operator_notes: value,
      }
    })
    setSaveState('idle')
    setSaveMessages([])
    setReadinessWarning(null)
  }

  const handleSubmit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault()

    if (draft === null || savedWorkspace === null) {
      return
    }

    if (!hasDirtyChanges) {
      return
    }

    if (validationMessages.length > 0) {
      setSaveState('failed')
      setSaveMessages([])
      return
    }

    const currentDirtySections = getDirtyProfileSections(savedWorkspace, draft)

    setSaveState('saving')
    setSaveMessages([])
    setReadinessWarning(null)

    try {
      const updatedWorkspace = await updatePrototypeWorkspaceProfile(
        workspace.slug,
        buildWorkspaceProfileUpdateRequest(draft, currentDirtySections),
      )

      setSavedWorkspace(updatedWorkspace)
      setDraft(buildWorkspaceProfileDraft(updatedWorkspace))
      setReadiness((currentReadiness) => (
        currentReadiness === null
          ? currentReadiness
          : {
              ...currentReadiness,
              workspace: updatedWorkspace,
            }
      ))

      try {
        const refreshedReadiness = await getPrototypeWorkspaceReadiness(workspace.slug, planningContextOptions)

        setReadiness(refreshedReadiness)
        setSavedWorkspace(refreshedReadiness.workspace)
        setDraft(buildWorkspaceProfileDraft(refreshedReadiness.workspace))
      } catch (refreshError) {
        setReadinessWarning(
          getApiErrorMessage(refreshError, 'Profile saved, but readiness guidance could not refresh.'),
        )
      }

      setSaveState('saved')
      setSaveMessages([buildSavedMessage(currentDirtySections)])
      void refreshShell()
    } catch (requestError) {
      const messages = getApiErrorMessages(requestError)
      setSaveState('failed')
      setSaveMessages(
        messages.length > 0 ? messages : [getApiErrorMessage(requestError, 'Profile save failed.')],
      )
    }
  }

  if (isLoading && readiness === null && draft === null) {
    return (
      <LoadingState
        title="Loading profile context"
        description="Fetching workspace readiness, saved profile values, and current blockers."
      />
    )
  }

  if (loadError && readiness === null && draft === null) {
    return (
      <ErrorState
        title="Profile page failed to load"
        description={getApiErrorMessage(loadError, 'The profile context could not be loaded.')}
        onRetry={refreshPage}
      />
    )
  }

  if (readiness === null || draft === null) {
    return (
      <ErrorState
        title="Profile page is unavailable"
        description="The profile route resolved, but its readiness payload was not available."
        onRetry={refreshPage}
      />
    )
  }

  return (
    <div className="page-stack">
      <CollapsibleHero
        tag="Stage 03"
        title="Capture the company profile and pilot scope before source collection moves forward."
        statusSlot={<StatusChip status={readiness.readiness.ready_for_parse ? 'completed' : 'blocked'} />}
      >
        <div className="hero-copy">
          <p>
            This page is the first full operator edit surface in the new shell. It loads current
            backend values, keeps edits local until save, and refreshes readiness guidance after each successful update.
          </p>
          <p className="form-helper-copy">
            Latest saved workspace update: {formatDateTime(savedWorkspace?.updated_at || readiness.workspace.updated_at)}
          </p>
        </div>

        <div className="hero-actions">
          <div className="route-badge">
            <span className="summary-label">Current route</span>
            <strong>/workspaces/{workspace.slug}/profile</strong>
          </div>
          <div className="route-badge">
            <span className="summary-label">Open blockers</span>
            <strong>{workflow.summary.total_blocker_count}</strong>
          </div>
          <div className="route-badge">
            <span className="summary-label">Parse readiness</span>
            <strong>{readiness.readiness.ready_for_parse ? 'Ready' : 'Blocked'}</strong>
          </div>
          <div className="route-badge">
            <span className="summary-label">Blueprint readiness</span>
            <strong>{readiness.readiness.ready_for_blueprint ? 'Ready' : 'Not yet ready'}</strong>
          </div>
        </div>
      </CollapsibleHero>

      {activePlanningContext ? (
        <div className="inline-banner inline-banner-info">
          <strong>Workspace baseline editing stays global.</strong>
          <span>
            You are editing the shared workspace profile. Context-specific overrides for {activePlanningContext.name} live on the Contexts page.
          </span>
          <div className="form-actions">
            <AppLink className="secondary-button link-button" to={buildScopedWorkspacePath('contexts')}>
              Open Contexts
            </AppLink>
          </div>
        </div>
      ) : null}

      {readiness.blocking_items.length > 0 ? (
        <div className="inline-banner inline-banner-warn">
          <strong>Current readiness blockers are still active.</strong>
          <span>
            These blockers come from the {humanizeToken(readiness.current_stage)} readiness gate and may require action on this page, on the sources stage, or on later workflow stages.
          </span>
          <ul className="inline-detail-list">
            {readiness.blocking_items.map((item) => (
              <li key={item}>{item}</li>
            ))}
          </ul>
        </div>
      ) : (
        <div className="inline-banner inline-banner-info">
          <strong>Profile context is not blocking the current stage.</strong>
          <span>{nonBlockingMessage}</span>
        </div>
      )}

      {readinessWarning ? (
        <div className="inline-banner inline-banner-warn">
          <strong>Saved, but guidance refresh is stale.</strong>
          <span>{readinessWarning}</span>
        </div>
      ) : null}

      <form className="profile-page-form" onSubmit={handleSubmit}>
        <div className="profile-card-grid">
          <SectionCard
            kicker="Company profile"
            title="Capture company context"
            description="These answers shape early parsing, roadmap interpretation, and later blueprint synthesis."
            completeness={readiness.company_profile_completeness}
          >
            <div className="profile-form-grid">
              <label className="field-label">
                <span>Company name</span>
                <input
                  className="text-input"
                  value={draft.company_profile.company_name}
                  onChange={(event) => updateCompanyProfileField('company_name', event.target.value)}
                  placeholder="Acme Cloud"
                  autoComplete="organization"
                  disabled={saveState === 'saving'}
                />
              </label>

              <label className="field-label">
                <span>Website URL</span>
                <input
                  className="text-input"
                  type="url"
                  value={draft.company_profile.website_url}
                  onChange={(event) => updateCompanyProfileField('website_url', event.target.value)}
                  placeholder="https://example.com"
                  autoComplete="url"
                  disabled={saveState === 'saving'}
                />
              </label>

              <label className="field-label field-span-full">
                <span>Company description</span>
                <textarea
                  className="textarea-input"
                  value={draft.company_profile.company_description}
                  onChange={(event) => updateCompanyProfileField('company_description', event.target.value)}
                  placeholder="What the company does, what it sells, and where the business is heading."
                  disabled={saveState === 'saving'}
                />
              </label>

              <label className="field-label">
                <span>Main products</span>
                <textarea
                  className="textarea-input textarea-input-compact"
                  value={draft.company_profile.main_products_text}
                  onChange={(event) => updateCompanyProfileField('main_products_text', event.target.value)}
                  placeholder={'One product per line\nAcme Core\nAcme Insights'}
                  disabled={saveState === 'saving'}
                />
              </label>

              <label className="field-label">
                <span>Target customers</span>
                <textarea
                  className="textarea-input textarea-input-compact"
                  value={draft.company_profile.target_customers_text}
                  onChange={(event) => updateCompanyProfileField('target_customers_text', event.target.value)}
                  placeholder={'One customer segment per line\nMid-market SaaS teams\nEnterprise platform groups'}
                  disabled={saveState === 'saving'}
                />
              </label>

              <label className="field-label">
                <span>Primary market geography</span>
                <input
                  className="text-input"
                  value={draft.company_profile.primary_market_geography}
                  onChange={(event) => updateCompanyProfileField('primary_market_geography', event.target.value)}
                  placeholder="North America and Western Europe"
                  disabled={saveState === 'saving'}
                />
              </label>

              <label className="field-label">
                <span>Team locations or hiring geographies</span>
                <textarea
                  className="textarea-input textarea-input-compact"
                  value={draft.company_profile.locations_text}
                  onChange={(event) => updateCompanyProfileField('locations_text', event.target.value)}
                  placeholder={'One location per line\nRemote\nNorth America\nEurope'}
                  disabled={saveState === 'saving'}
                />
              </label>

              <label className="field-label">
                <span>Rough employee count</span>
                <input
                  className="text-input"
                  type="number"
                  min="1"
                  inputMode="numeric"
                  value={draft.company_profile.rough_employee_count_input}
                  onChange={(event) => updateCompanyProfileField('rough_employee_count_input', event.target.value)}
                  placeholder="54"
                  disabled={saveState === 'saving'}
                />
              </label>

              <label className="field-label">
                <span>Current tech stack</span>
                <textarea
                  className="textarea-input textarea-input-compact"
                  value={draft.company_profile.current_tech_stack_text}
                  onChange={(event) => updateCompanyProfileField('current_tech_stack_text', event.target.value)}
                  placeholder={'One technology per line\nPython\nDjango\nPostgres\nReact'}
                  disabled={saveState === 'saving'}
                />
              </label>

              <label className="field-label">
                <span>Planned tech stack or major tooling shifts</span>
                <textarea
                  className="textarea-input textarea-input-compact"
                  value={draft.company_profile.planned_tech_stack_text}
                  onChange={(event) => updateCompanyProfileField('planned_tech_stack_text', event.target.value)}
                  placeholder={'One change per line\nFastAPI\nOpenAI API\nQdrant\ndbt'}
                  disabled={saveState === 'saving'}
                />
              </label>

              <label className="field-label field-span-full">
                <span>Why this pilot matters now</span>
                <textarea
                  className="textarea-input"
                  value={draft.company_profile.pilot_scope_notes}
                  onChange={(event) => updateCompanyProfileField('pilot_scope_notes', event.target.value)}
                  placeholder="Pilot starts with product and engineering because roadmap execution is under pressure."
                  disabled={saveState === 'saving'}
                />
              </label>

              <label className="field-label field-span-full">
                <span>Constraints or growth plans</span>
                <textarea
                  className="textarea-input"
                  value={draft.company_profile.notable_constraints_or_growth_plans}
                  onChange={(event) => updateCompanyProfileField('notable_constraints_or_growth_plans', event.target.value)}
                  placeholder="Hiring plans, funding constraints, roadmap pressure, reorganizations, or strategic shifts."
                  disabled={saveState === 'saving'}
                />
              </label>
            </div>

            <div className="helper-grid">
              <HelperCard
                title="Missing required profile context"
                items={readiness.company_profile_completeness.missing_required_fields}
                tone="required"
              />
              <HelperCard
                title="Recommended detail to add"
                items={readiness.company_profile_completeness.missing_recommended_fields}
                tone="recommended"
              />
            </div>
          </SectionCard>

          <SectionCard
            kicker="Pilot scope"
            title="Define who the pilot covers"
            description="The pilot scope tells later stages which functions, roles, and products should drive evidence and blueprinting."
            completeness={readiness.pilot_scope_completeness}
          >
            <div className="profile-form-grid">
              <label className="field-label">
                <span>Scope mode</span>
                <input
                  className="text-input"
                  list="scope-mode-suggestions"
                  value={draft.pilot_scope.scope_mode}
                  onChange={(event) => updatePilotScopeField('scope_mode', event.target.value)}
                  placeholder="whole_company or a custom scope label"
                  disabled={saveState === 'saving'}
                />
                <datalist id="scope-mode-suggestions">
                  {scopeModeOptions.map((option) => (
                    <option key={option} value={option}>
                      {humanizeToken(option)}
                    </option>
                  ))}
                </datalist>
              </label>

              <label className="field-label">
                <span>Stakeholder contact</span>
                <input
                  className="text-input"
                  value={draft.pilot_scope.stakeholder_contact}
                  onChange={(event) => updatePilotScopeField('stakeholder_contact', event.target.value)}
                  placeholder="Alex Product Ops"
                  disabled={saveState === 'saving'}
                />
              </label>

              <label className="field-label">
                <span>Departments in scope</span>
                <textarea
                  className="textarea-input textarea-input-compact"
                  value={draft.pilot_scope.departments_in_scope_text}
                  onChange={(event) => updatePilotScopeField('departments_in_scope_text', event.target.value)}
                  placeholder={'One department per line\nEngineering\nProduct'}
                  disabled={saveState === 'saving'}
                />
              </label>

              <label className="field-label">
                <span>Roles in scope</span>
                <textarea
                  className="textarea-input textarea-input-compact"
                  value={draft.pilot_scope.roles_in_scope_text}
                  onChange={(event) => updatePilotScopeField('roles_in_scope_text', event.target.value)}
                  placeholder={'One role per line\nBackend Engineer\nProduct Manager'}
                  disabled={saveState === 'saving'}
                />
              </label>

              <label className="field-label">
                <span>Products in scope</span>
                <textarea
                  className="textarea-input textarea-input-compact"
                  value={draft.pilot_scope.products_in_scope_text}
                  onChange={(event) => updatePilotScopeField('products_in_scope_text', event.target.value)}
                  placeholder={'One product per line\nAcme Core'}
                  disabled={saveState === 'saving'}
                />
              </label>

              <label className="field-label">
                <span>Employee count in scope</span>
                <input
                  className="text-input"
                  type="number"
                  min="1"
                  inputMode="numeric"
                  value={draft.pilot_scope.employee_count_in_scope_input}
                  onChange={(event) => updatePilotScopeField('employee_count_in_scope_input', event.target.value)}
                  placeholder="22"
                  disabled={saveState === 'saving'}
                />
              </label>

              <label className="field-label field-span-full">
                <span>Analyst notes</span>
                <textarea
                  className="textarea-input"
                  value={draft.pilot_scope.analyst_notes}
                  onChange={(event) => updatePilotScopeField('analyst_notes', event.target.value)}
                  placeholder="Capture scope assumptions, exclusions, and caveats for later review."
                  disabled={saveState === 'saving'}
                />
              </label>
            </div>

            <div className="helper-grid">
              <HelperCard
                title="Required to unblock early workflow steps"
                items={readiness.pilot_scope_completeness.missing_required_fields}
                tone="required"
              />
              <HelperCard
                title="Recommended pilot detail to add"
                items={readiness.pilot_scope_completeness.missing_recommended_fields}
                tone="recommended"
              />
            </div>
          </SectionCard>

          <SectionCard
            kicker="Source checklist"
            title="Record what context should exist"
            description="This checklist does not upload files yet. It tells the operator and the next stage which source groups are expected, available, or intentionally missing."
          >
            <div className="tri-state-grid">
              <label className="field-label">
                <span>Existing matrix available</span>
                <select
                  className="select-input"
                  value={draft.source_checklist.existing_matrix_available}
                  onChange={(event) => updateChecklistField('existing_matrix_available', event.target.value as ChecklistChoice)}
                  disabled={saveState === 'saving'}
                >
                  {CHECKLIST_OPTIONS.map((option) => (
                    <option key={option.value} value={option.value}>
                      {option.label}
                    </option>
                  ))}
                </select>
              </label>

              <label className="field-label">
                <span>Sales or growth plan available</span>
                <select
                  className="select-input"
                  value={draft.source_checklist.sales_growth_plan_available}
                  onChange={(event) => updateChecklistField('sales_growth_plan_available', event.target.value as ChecklistChoice)}
                  disabled={saveState === 'saving'}
                >
                  {CHECKLIST_OPTIONS.map((option) => (
                    <option key={option.value} value={option.value}>
                      {option.label}
                    </option>
                  ))}
                </select>
              </label>

              <label className="field-label">
                <span>Architecture overview available</span>
                <select
                  className="select-input"
                  value={draft.source_checklist.architecture_overview_available}
                  onChange={(event) => updateChecklistField('architecture_overview_available', event.target.value as ChecklistChoice)}
                  disabled={saveState === 'saving'}
                >
                  {CHECKLIST_OPTIONS.map((option) => (
                    <option key={option.value} value={option.value}>
                      {option.label}
                    </option>
                  ))}
                </select>
              </label>

              <label className="field-label">
                <span>Product notes available</span>
                <select
                  className="select-input"
                  value={draft.source_checklist.product_notes_available}
                  onChange={(event) => updateChecklistField('product_notes_available', event.target.value as ChecklistChoice)}
                  disabled={saveState === 'saving'}
                >
                  {CHECKLIST_OPTIONS.map((option) => (
                    <option key={option.value} value={option.value}>
                      {option.label}
                    </option>
                  ))}
                </select>
              </label>

              <label className="field-label">
                <span>HR notes available</span>
                <select
                  className="select-input"
                  value={draft.source_checklist.hr_notes_available}
                  onChange={(event) => updateChecklistField('hr_notes_available', event.target.value as ChecklistChoice)}
                  disabled={saveState === 'saving'}
                >
                  {CHECKLIST_OPTIONS.map((option) => (
                    <option key={option.value} value={option.value}>
                      {option.label}
                    </option>
                  ))}
                </select>
              </label>

              <label className="field-label field-span-full">
                <span>Checklist notes</span>
                <textarea
                  className="textarea-input"
                  value={draft.source_checklist.notes}
                  onChange={(event) => updateChecklistField('notes', event.target.value)}
                  placeholder="Use Unknown when availability is not yet confirmed. Use No when the source genuinely does not exist."
                  disabled={saveState === 'saving'}
                />
              </label>
            </div>

            <div className="profile-section-copy">
              <p className="form-helper-copy">
                Unknown means not confirmed yet. No means the source is intentionally unavailable. The actual attachment and parsing flow starts on the sources stage.
              </p>
              <div className="hero-actions">
                <AppLink className="secondary-button link-button" to={buildScopedWorkspacePath('sources')}>
                  Open sources stage
                </AppLink>
              </div>
            </div>

            <div className="requirement-list">
              {checklistRequirements.map((requirement) => (
                <SourceRequirementItem key={requirement.key} requirement={requirement} />
              ))}
            </div>
          </SectionCard>

          <SectionCard
            kicker="Operator notes"
            title="Capture internal handoff context"
            description="Keep the notes operator-facing. This is the place for caveats, follow-up reminders, and intake context that should persist across stages."
          >
            <label className="field-label">
              <span>Operator notes</span>
              <textarea
                className="textarea-input textarea-input-tall"
                value={draft.operator_notes}
                onChange={(event) => updateOperatorNotes(event.target.value)}
                placeholder="Initial pilot intake created by analyst. Product and engineering are the first departments in scope."
                disabled={saveState === 'saving'}
              />
            </label>
          </SectionCard>
        </div>

        <section className="save-bar" aria-live="polite">
          <div className="save-bar-copy">
            <span className="section-tag">Save bar</span>
            <h3>Save profile context explicitly</h3>
            <p>
              {hasDirtyChanges
                ? `${dirtySectionCount} section(s) have unsaved changes.`
                : 'No unsaved changes. The form reflects the latest saved backend response.'}
            </p>
          </div>

          {hasDirtyChanges && validationMessages.length > 0 ? (
            <div className="inline-banner inline-banner-error">
              <strong>Fix these values before saving.</strong>
              <ul className="inline-detail-list">
                {validationMessages.map((message) => (
                  <li key={message}>{message}</li>
                ))}
              </ul>
            </div>
          ) : null}

          {saveMessages.length > 0 ? (
            <div className={saveState === 'failed' ? 'inline-banner inline-banner-error' : 'inline-banner inline-banner-info'}>
              <strong>
                {saveState === 'saving'
                  ? 'Saving profile context...'
                  : saveState === 'failed'
                    ? 'Save failed'
                    : 'Save complete'}
              </strong>
              {saveMessages.length === 1 ? (
                <span>{saveMessages[0]}</span>
              ) : (
                <ul className="inline-detail-list">
                  {saveMessages.map((message) => (
                    <li key={message}>{message}</li>
                  ))}
                </ul>
              )}
            </div>
          ) : null}

          <div className="form-actions">
            <button
              className="secondary-button"
              type="button"
              onClick={resetDraft}
              disabled={saveState === 'saving' || !hasDirtyChanges}
            >
              Reset changes
            </button>
            <button className="primary-button" type="submit" disabled={!canSave}>
              {saveState === 'saving' ? 'Saving profile context...' : 'Save profile context'}
            </button>
            <AppLink className="secondary-button link-button" to={buildScopedWorkspacePath('overview')}>
              Back to overview
            </AppLink>
          </div>
        </section>
      </form>
    </div>
  )
}
