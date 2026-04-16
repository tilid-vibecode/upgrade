import { useCallback, useEffect, useMemo, useRef, useState, type ChangeEvent, type FormEvent } from 'react'

import { useWorkspaceShell } from '../app/WorkspaceLayout'
import { useNavigationBlocker } from '../app/navigation'
import { getApiErrorMessage, getApiErrorMessages, isApiError } from '../shared/api'
import { formatDateTime } from '../shared/formatters'
import {
  addPrototypePlanningContextSource,
  createPrototypePlanningContext,
  createPrototypeWorkspaceProject,
  deletePrototypePlanningContextSource,
  getPrototypePlanningContextDetail,
  listPrototypeWorkspaceProjects,
  listPrototypeWorkspaceSources,
  updatePrototypePlanningContext,
  type PrototypePlanningContextDetail,
  type PrototypePlanningContextSourceLink,
  type PrototypeWorkspaceProject,
  type PrototypeWorkspaceSource,
} from '../shared/prototypeApi'
import { getSourceKindLabel } from '../shared/sourceLibrary'
import { buildWorkspaceSlugCandidate, humanizeToken } from '../shared/workflow'
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

type ContextEditorDraft = {
  name: string
  slug: string
  status: string
  description: string
  metadataText: string
  inheritFromParent: boolean
  techStackText: string
  techStackRemoveText: string
  constraintsText: string
  growthGoalsText: string
  overrideFieldsText: string
  companyProfileText: string
}

type ContextCreateDraft = {
  name: string
  slug: string
  kind: string
  parentContextUuid: string
  projectUuid: string
  description: string
}

type WorkspaceProjectCreateDraft = {
  name: string
}

type SourceOverrideDraft = {
  usage_type: string
  include_in_blueprint: boolean
  include_in_roadmap_analysis: boolean
  is_active: boolean
}

const CONTEXT_KIND_OPTIONS = [
  { value: 'org', label: 'Org' },
  { value: 'project', label: 'Project' },
  { value: 'scenario', label: 'Scenario' },
]

const CONTEXT_STATUS_OPTIONS = [
  { value: 'active', label: 'Active' },
  { value: 'draft', label: 'Draft' },
  { value: 'archived', label: 'Archived' },
]

const SOURCE_USAGE_OPTIONS = [
  { value: 'roadmap', label: 'Roadmap' },
  { value: 'strategy', label: 'Strategy' },
  { value: 'role_reference', label: 'Role reference' },
  { value: 'org_structure', label: 'Org structure' },
  { value: 'employee_cv', label: 'Employee CV' },
  { value: 'other', label: 'Other' },
]

function requestOptional<T>(request: Promise<T>) {
  return request.catch((error: unknown) => {
    if (isApiError(error) && error.status === 404) {
      return null
    }
    throw error
  })
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

function normalizeOptionalString(value: string) {
  const normalized = value.trim()
  return normalized || undefined
}

function readJson(value: string, label: string) {
  const trimmed = value.trim()
  if (!trimmed) {
    return {}
  }

  try {
    const parsed = JSON.parse(trimmed)
    if (typeof parsed === 'object' && parsed !== null && !Array.isArray(parsed)) {
      return parsed
    }
    throw new Error(`${label} must be a JSON object.`)
  } catch (error) {
    throw new Error(error instanceof Error ? error.message : `${label} must be valid JSON.`)
  }
}

function stringifyJson(value: unknown) {
  try {
    return JSON.stringify(value ?? {}, null, 2)
  } catch {
    return '{}'
  }
}

function splitTextList(value: string) {
  return value
    .split('\n')
    .map((item) => item.trim())
    .filter(Boolean)
}

function defaultUsageTypeForSourceKind(sourceKind: string) {
  switch (sourceKind) {
    case 'roadmap':
      return 'roadmap'
    case 'strategy':
      return 'strategy'
    case 'job_description':
      return 'role_reference'
    case 'org_csv':
      return 'org_structure'
    case 'employee_cv':
      return 'employee_cv'
    default:
      return 'other'
  }
}

function defaultRoadmapInclusion(usageType: string) {
  return usageType === 'roadmap' || usageType === 'strategy'
}

function buildCreateDraft() {
  return {
    name: '',
    slug: '',
    kind: 'project',
    parentContextUuid: '',
    projectUuid: '',
    description: '',
  }
}

function buildProjectCreateDraft() {
  return {
    name: '',
  }
}

function sortWorkspaceProjects(projects: PrototypeWorkspaceProject[]) {
  return [...projects].sort((left, right) => left.name.localeCompare(right.name, undefined, { sensitivity: 'base' }))
}

function buildEditorDraft(detail: PrototypePlanningContextDetail): ContextEditorDraft {
  return {
    name: detail.name,
    slug: detail.slug,
    status: detail.status,
    description: detail.description || '',
    metadataText: stringifyJson(detail.metadata),
    inheritFromParent: Boolean(detail.profile.inherit_from_parent),
    techStackText: (detail.profile.tech_stack || []).join('\n'),
    techStackRemoveText: (detail.profile.tech_stack_remove || []).join('\n'),
    constraintsText: (detail.profile.constraints || []).join('\n'),
    growthGoalsText: (detail.profile.growth_goals || []).join('\n'),
    overrideFieldsText: (detail.profile.override_fields || []).join('\n'),
    companyProfileText: stringifyJson(detail.profile.company_profile),
  }
}

function buildSourceDraft(
  source: PrototypeWorkspaceSource,
  link: PrototypePlanningContextSourceLink | null,
): SourceOverrideDraft {
  const usageType = link?.usage_type || defaultUsageTypeForSourceKind(source.source_kind)
  return {
    usage_type: usageType,
    include_in_blueprint: link?.include_in_blueprint ?? true,
    include_in_roadmap_analysis: link?.include_in_roadmap_analysis ?? defaultRoadmapInclusion(usageType),
    is_active: link?.is_active ?? true,
  }
}

function areContextEditorDraftsEqual(left: ContextEditorDraft | null, right: ContextEditorDraft | null) {
  if (left === right) {
    return true
  }

  if (left === null || right === null) {
    return false
  }

  return (
    left.name === right.name &&
    left.slug === right.slug &&
    left.status === right.status &&
    left.description === right.description &&
    left.metadataText === right.metadataText &&
    left.inheritFromParent === right.inheritFromParent &&
    left.techStackText === right.techStackText &&
    left.techStackRemoveText === right.techStackRemoveText &&
    left.constraintsText === right.constraintsText &&
    left.growthGoalsText === right.growthGoalsText &&
    left.overrideFieldsText === right.overrideFieldsText &&
    left.companyProfileText === right.companyProfileText
  )
}

function areSourceOverrideDraftsEqual(left: SourceOverrideDraft, right: SourceOverrideDraft) {
  return (
    left.usage_type === right.usage_type &&
    left.include_in_blueprint === right.include_in_blueprint &&
    left.include_in_roadmap_analysis === right.include_in_roadmap_analysis &&
    left.is_active === right.is_active
  )
}

function canDeleteDirectSourceOverride(link: PrototypePlanningContextSourceLink | null) {
  if (!link || !link.uuid) {
    return false
  }

  return link.origin === 'direct' || (link.origin === 'excluded' && !link.inherited_from_context_uuid)
}

function getSourceOriginCopy(link: PrototypePlanningContextSourceLink | null) {
  if (!link) {
    return 'Not linked anywhere in this context lineage yet.'
  }
  if (link.origin === 'direct') {
    return 'Direct override at this context.'
  }
  if (link.origin === 'inherited') {
    return link.inherited_from_context_slug
      ? `Inherited from ${link.inherited_from_context_slug}.`
      : 'Inherited from an ancestor context.'
  }
  return link.excluded_reason || 'Excluded by a context rule.'
}

export default function WorkspaceContextsPage() {
  const {
    workspace,
    planningContexts,
    activePlanningContext,
    contextWarning,
    refreshShell,
    setActivePlanningContextSlug,
  } = useWorkspaceShell()

  const [projects, setProjects] = useState<PrototypeWorkspaceProject[]>([])
  const [workspaceSources, setWorkspaceSources] = useState<PrototypeWorkspaceSource[]>([])
  const [detail, setDetail] = useState<PrototypePlanningContextDetail | null>(null)
  const [editorDraft, setEditorDraft] = useState<ContextEditorDraft | null>(null)
  const [sourceDrafts, setSourceDrafts] = useState<Record<string, SourceOverrideDraft>>({})
  const [createDraft, setCreateDraft] = useState<ContextCreateDraft>(() => buildCreateDraft())
  const [projectCreateDraft, setProjectCreateDraft] = useState<WorkspaceProjectCreateDraft>(() => buildProjectCreateDraft())
  const [loading, setLoading] = useState(true)
  const [loadError, setLoadError] = useState<string | null>(null)
  const [busyAction, setBusyAction] = useState<string | null>(null)
  const [banner, setBanner] = useState<BannerState | null>(null)
  const loadRequestIdRef = useRef(0)

  const activeDetailSlug = activePlanningContext?.slug || null

  const loadContextPage = useCallback(async () => {
    const requestId = loadRequestIdRef.current + 1
    loadRequestIdRef.current = requestId
    setLoading(true)
    setLoadError(null)

    try {
      const [projectsResponse, sourcesResponse, detailResponse] = await Promise.all([
        listPrototypeWorkspaceProjects(workspace.slug),
        listPrototypeWorkspaceSources(workspace.slug, { includeArchived: true }),
        activeDetailSlug
          ? requestOptional(getPrototypePlanningContextDetail(workspace.slug, activeDetailSlug))
          : Promise.resolve(null),
      ])

      if (loadRequestIdRef.current !== requestId) {
        return
      }

      setProjects(sortWorkspaceProjects(projectsResponse.projects))
      setWorkspaceSources(
        [...sourcesResponse.sources].sort((left, right) => left.title.localeCompare(right.title, undefined, { sensitivity: 'base' })),
      )
      setDetail(detailResponse)
      setEditorDraft(detailResponse ? buildEditorDraft(detailResponse) : null)
      setSourceDrafts({})
    } catch (requestError) {
      if (loadRequestIdRef.current === requestId) {
        setLoadError(getApiErrorMessage(requestError, 'Failed to load planning-context management data.'))
      }
    } finally {
      if (loadRequestIdRef.current === requestId) {
        setLoading(false)
      }
    }
  }, [activeDetailSlug, workspace.slug])

  useEffect(() => {
    void loadContextPage()
  }, [loadContextPage])

  const contextsByUuid = useMemo(
    () => new Map(planningContexts.map((context) => [context.uuid, context])),
    [planningContexts],
  )
  const orgParentOptions = useMemo(
    () => planningContexts.filter((context) => context.kind === 'org'),
    [planningContexts],
  )
  const scenarioParentOptions = useMemo(
    () => planningContexts.filter((context) => context.kind === 'org' || context.kind === 'project'),
    [planningContexts],
  )
  const sourceLinksBySourceUuid = useMemo(
    () => new Map((detail?.sources || []).map((link) => [link.workspace_source_uuid, link])),
    [detail?.sources],
  )
  const initialCreateDraft = useMemo(() => buildCreateDraft(), [])
  const initialProjectCreateDraft = useMemo(() => buildProjectCreateDraft(), [])
  const hasDirtyCreateDraft = (
    createDraft.name !== initialCreateDraft.name ||
    createDraft.slug !== initialCreateDraft.slug ||
    createDraft.kind !== initialCreateDraft.kind ||
    createDraft.parentContextUuid !== initialCreateDraft.parentContextUuid ||
    createDraft.projectUuid !== initialCreateDraft.projectUuid ||
    createDraft.description !== initialCreateDraft.description
  )
  const hasDirtyProjectCreateDraft = projectCreateDraft.name !== initialProjectCreateDraft.name
  const hasDirtyEditorDraft = !areContextEditorDraftsEqual(
    editorDraft,
    detail ? buildEditorDraft(detail) : null,
  )
  const hasDirtySourceDrafts = Object.entries(sourceDrafts).some(([sourceUuid, draft]) => {
    const source = workspaceSources.find((item) => item.uuid === sourceUuid)
    if (!source) {
      return false
    }

    return !areSourceOverrideDraftsEqual(
      draft,
      buildSourceDraft(source, sourceLinksBySourceUuid.get(source.uuid) ?? null),
    )
  })

  useNavigationBlocker(
    hasDirtyCreateDraft || hasDirtyProjectCreateDraft || hasDirtyEditorDraft || hasDirtySourceDrafts,
    'You have unsaved planning-context changes. Leave this page without saving?',
  )
  const isArchivedDetailContext = detail?.status === 'archived'

  function getSourceDraft(source: PrototypeWorkspaceSource) {
    return sourceDrafts[source.uuid] ?? buildSourceDraft(source, sourceLinksBySourceUuid.get(source.uuid) ?? null)
  }

  function updateSourceDraft(sourceUuid: string, patch: Partial<SourceOverrideDraft>) {
    const source = workspaceSources.find((item) => item.uuid === sourceUuid)
    if (!source) {
      return
    }

    setSourceDrafts((currentValue) => ({
      ...currentValue,
      [sourceUuid]: {
        ...getSourceDraft(source),
        ...patch,
      },
    }))
  }

  function handleCreateNameChange(event: ChangeEvent<HTMLInputElement>) {
    const nextName = event.target.value
    setCreateDraft((currentValue) => ({
      ...currentValue,
      name: nextName,
      slug: currentValue.slug ? currentValue.slug : buildWorkspaceSlugCandidate(nextName),
    }))
  }

  function handleCreateKindChange(event: ChangeEvent<HTMLSelectElement>) {
    const nextKind = event.target.value
    setCreateDraft((currentValue) => ({
      ...currentValue,
      kind: nextKind,
      parentContextUuid: nextKind === 'org' ? '' : currentValue.parentContextUuid,
      projectUuid: nextKind === 'project' ? currentValue.projectUuid : '',
    }))
  }

  async function handleCreateProject(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    setBusyAction('create-project')
    setBanner(null)

    try {
      const created = await createPrototypeWorkspaceProject(workspace.slug, {
        name: projectCreateDraft.name.trim(),
      })
      const shouldAutoSelectProject = createDraft.kind === 'project' && (!createDraft.projectUuid || projects.length === 0)

      setProjects((currentValue) => sortWorkspaceProjects([...currentValue, created]))
      setProjectCreateDraft(buildProjectCreateDraft())
      if (shouldAutoSelectProject) {
        setCreateDraft((currentValue) => ({ ...currentValue, projectUuid: created.uuid }))
      }
      setBanner({
        tone: 'success',
        title: `${created.name} was added as a workspace project.`,
        messages: ['It is now available in the project-context dropdown.'],
      })
    } catch (requestError) {
      setBanner({
        tone: 'error',
        title: 'Workspace project could not be created.',
        messages: getApiErrorMessages(requestError),
      })
    } finally {
      setBusyAction(null)
    }
  }

  async function handleCreateContext(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    setBusyAction('create')
    setBanner(null)

    try {
      const created = await createPrototypePlanningContext(workspace.slug, {
        name: createDraft.name.trim(),
        slug: createDraft.slug.trim(),
        kind: createDraft.kind,
        parent_context_uuid: normalizeOptionalString(createDraft.parentContextUuid) || null,
        project_uuid: createDraft.kind === 'project' ? normalizeOptionalString(createDraft.projectUuid) || null : null,
        description: createDraft.description.trim(),
      })

      setBanner({
        tone: 'success',
        title: `${created.name} was created.`,
        messages: ['The new planning context is now selected for editing and downstream stage scoping.'],
      })
      setCreateDraft(buildCreateDraft())
      setActivePlanningContextSlug(created.slug)
      await refreshShell({ contextSlug: created.slug })
    } catch (requestError) {
      setBanner({
        tone: 'error',
        title: 'Planning context could not be created.',
        messages: getApiErrorMessages(requestError),
      })
    } finally {
      setBusyAction(null)
    }
  }

  async function handleSaveContext(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    if (!detail || !editorDraft) {
      return
    }

    setBusyAction('save-context')
    setBanner(null)

    try {
      const updated = await updatePrototypePlanningContext(workspace.slug, detail.slug, {
        name: editorDraft.name.trim(),
        slug: editorDraft.slug.trim(),
        status: editorDraft.status,
        description: editorDraft.description.trim(),
        metadata: readJson(editorDraft.metadataText, 'Metadata'),
        profile: {
          inherit_from_parent:
            detail.kind === 'org' && detail.parent_context === null ? false : editorDraft.inheritFromParent,
          tech_stack: splitTextList(editorDraft.techStackText),
          tech_stack_remove: splitTextList(editorDraft.techStackRemoveText),
          constraints: splitTextList(editorDraft.constraintsText),
          growth_goals: splitTextList(editorDraft.growthGoalsText),
          override_fields: splitTextList(editorDraft.overrideFieldsText),
          company_profile: readJson(editorDraft.companyProfileText, 'Company profile overrides'),
        },
      })

      setDetail(updated)
      setEditorDraft(buildEditorDraft(updated))
      setBanner({
        tone: 'success',
        title: `${updated.name} was updated.`,
        messages: ['Profile overrides and context metadata are now aligned with the latest server state.'],
      })
      if (detail.slug !== updated.slug) {
        setActivePlanningContextSlug(updated.slug, { replace: true })
        await refreshShell({ contextSlug: updated.slug })
        return
      }
      await Promise.all([refreshShell(), loadContextPage()])
    } catch (requestError) {
      setBanner({
        tone: 'error',
        title: 'Planning context could not be saved.',
        messages: getApiErrorMessages(requestError),
      })
    } finally {
      setBusyAction(null)
    }
  }

  async function handleSaveSourceOverride(source: PrototypeWorkspaceSource) {
    if (!detail) {
      return
    }

    const draft = getSourceDraft(source)
    setBusyAction(`save-source:${source.uuid}`)
    setBanner(null)

    try {
      await addPrototypePlanningContextSource(workspace.slug, detail.slug, {
        workspace_source_uuid: source.uuid,
        usage_type: draft.usage_type,
        include_in_blueprint: draft.include_in_blueprint,
        include_in_roadmap_analysis: draft.include_in_roadmap_analysis,
        is_active: draft.is_active,
      })
      setBanner({
        tone: 'success',
        title: `${source.title} override saved.`,
        messages: ['The effective context-source linkage has been refreshed.'],
      })
      await refreshShell()
      await loadContextPage()
    } catch (requestError) {
      setBanner({
        tone: 'error',
        title: `${source.title} override could not be saved.`,
        messages: getApiErrorMessages(requestError),
      })
    } finally {
      setBusyAction(null)
    }
  }

  async function handleDeleteSourceOverride(link: PrototypePlanningContextSourceLink) {
    if (!detail || !link.uuid) {
      return
    }

    setBusyAction(`delete-source:${link.workspace_source_uuid}`)
    setBanner(null)

    try {
      await deletePrototypePlanningContextSource(workspace.slug, detail.slug, link.uuid)
      setBanner({
        tone: 'success',
        title: `${link.title} reverted to inherited behavior.`,
        messages: ['The direct override was removed without archiving the underlying workspace source.'],
      })
      await refreshShell()
      await loadContextPage()
    } catch (requestError) {
      setBanner({
        tone: 'error',
        title: `${link.title} override could not be deleted.`,
        messages: getApiErrorMessages(requestError),
      })
    } finally {
      setBusyAction(null)
    }
  }

  if (loading) {
    return (
      <LoadingState
        title="Loading planning contexts"
        description="Fetching planning scopes, source linkage, and editable context details."
      />
    )
  }

  if (loadError) {
    return (
      <ErrorState
        title="Failed to load planning contexts"
        description={loadError}
        onRetry={() => void loadContextPage()}
      />
    )
  }

  return (
    <div className="page-stack">
      {renderBanner(banner)}
      {contextWarning ? (
        <section className="inline-banner inline-banner-warn">
          <strong>Scope reset</strong>
          <span>{contextWarning}</span>
        </section>
      ) : null}

      {isArchivedDetailContext ? (
        <section className="inline-banner inline-banner-warn">
          <strong>{detail?.name || 'This planning context'} is archived and read-only.</strong>
          <span>Profile overrides and source linkage remain visible here, but editing is disabled while the context is archived.</span>
        </section>
      ) : null}

      <section className="summary-grid">
        <article className="summary-card">
          <span className="summary-label">Current scope</span>
          <strong>{activePlanningContext?.name || 'Legacy workspace'}</strong>
          <p>{activePlanningContext ? `${humanizeToken(activePlanningContext.kind)} · ${humanizeToken(activePlanningContext.status)}` : 'No planning context selected in the URL.'}</p>
        </article>
        <article className="summary-card">
          <span className="summary-label">Planning contexts</span>
          <strong>{planningContexts.length}</strong>
          <p>The backend-created `org-baseline` context should already exist for each workspace.</p>
        </article>
        <article className="summary-card">
          <span className="summary-label">Workspace projects</span>
          <strong>{projects.length}</strong>
          <p>{projects.length > 0 ? 'Project-scoped contexts use these project identifiers during creation.' : 'Create the first workspace project below before adding a project-scoped context.'}</p>
        </article>
      </section>

      <div className="workspace-entry-grid">
        <section className="board-panel">
          <div className="panel-heading">
            <span className="section-tag">Planning Scopes</span>
            <h3>Contexts in this workspace</h3>
            <p>Select a context to scope downstream blueprint, assessment, matrix, and plan workflows.</p>
          </div>

          {planningContexts.length > 0 ? (
            <div className="review-card-grid">
              {planningContexts.map((context) => {
                const parent = context.parent_context_uuid ? contextsByUuid.get(context.parent_context_uuid) : null
                const isSelected = activePlanningContext?.uuid === context.uuid
                return (
                  <article
                    key={context.uuid}
                    className={isSelected ? 'review-card is-selected' : 'review-card'}
                  >
                    <div className="review-card-head">
                      <div>
                        <span className="summary-label">{humanizeToken(context.kind)}</span>
                        <h4>{context.name}</h4>
                      </div>
                      <StatusChip status={context.status === 'archived' ? 'blocked' : context.status === 'draft' ? 'ready' : 'completed'} />
                    </div>
                    <div className="detail-stack">
                      <p>Slug: {context.slug}</p>
                      {parent ? <p>Parent: {parent.name}</p> : <p>Root context</p>}
                      <p>{context.child_count} child context(s) · {context.source_count} source link(s)</p>
                    </div>
                    <div className="form-actions">
                      <button
                        className="secondary-button"
                        type="button"
                        onClick={() => setActivePlanningContextSlug(context.slug)}
                      >
                        {isSelected ? 'Selected' : 'Open context'}
                      </button>
                    </div>
                  </article>
                )
              })}
            </div>
          ) : (
            <EmptyState
              title="No planning contexts found"
              description="This workspace does not have any planning contexts yet."
            />
          )}
        </section>

        <section className="board-panel">
          <div className="panel-heading">
            <span className="section-tag">Create Context</span>
            <h3>Add a new planning scope</h3>
            <p>Project contexts require an org parent and a workspace project. Scenario contexts inherit from an org or project context.</p>
          </div>

          {createDraft.kind === 'project' && projects.length === 0 ? (
            <section className="inline-banner inline-banner-info">
              <strong>No workspace projects exist yet.</strong>
              <span>Create one below first, then it will become selectable for this project context.</span>
            </section>
          ) : null}

          <form className="profile-form-grid" onSubmit={(event) => void handleCreateProject(event)}>
            <label className="field-label field-span-full">
              <span>New workspace project</span>
              <input
                className="text-input"
                value={projectCreateDraft.name}
                onChange={(event) => setProjectCreateDraft({ name: event.target.value })}
                placeholder="e.g. Hyperskill"
                required
              />
              <span className="form-helper-copy">Use the product, initiative, or stream name that should back project-scoped contexts.</span>
            </label>
            <div className="form-actions field-span-full">
              <button
                className="secondary-button"
                type="submit"
                disabled={busyAction !== null || !projectCreateDraft.name.trim()}
              >
                {busyAction === 'create-project' ? 'Creating project…' : 'Create workspace project'}
              </button>
            </div>
          </form>

          <form className="profile-form-grid" onSubmit={(event) => void handleCreateContext(event)}>
            <label className="field-label">
              <span>Name</span>
              <input className="text-input" value={createDraft.name} onChange={handleCreateNameChange} required />
            </label>
            <label className="field-label">
              <span>Slug</span>
              <input
                className="text-input"
                value={createDraft.slug}
                onChange={(event) => setCreateDraft((currentValue) => ({ ...currentValue, slug: event.target.value }))}
                required
              />
            </label>
            <label className="field-label">
              <span>Kind</span>
              <select className="select-input" value={createDraft.kind} onChange={handleCreateKindChange}>
                {CONTEXT_KIND_OPTIONS.map((option) => (
                  <option key={option.value} value={option.value}>{option.label}</option>
                ))}
              </select>
            </label>

            {createDraft.kind === 'project' ? (
              <label className="field-label">
                <span>Org parent</span>
                <select
                  className="select-input"
                  value={createDraft.parentContextUuid}
                  onChange={(event) => setCreateDraft((currentValue) => ({ ...currentValue, parentContextUuid: event.target.value }))}
                  required
                >
                  <option value="">Select an org context</option>
                  {orgParentOptions.map((context) => (
                    <option key={context.uuid} value={context.uuid}>{context.name}</option>
                  ))}
                </select>
              </label>
            ) : createDraft.kind === 'scenario' ? (
              <label className="field-label">
                <span>Parent context</span>
                <select
                  className="select-input"
                  value={createDraft.parentContextUuid}
                  onChange={(event) => setCreateDraft((currentValue) => ({ ...currentValue, parentContextUuid: event.target.value }))}
                  required
                >
                  <option value="">Select an org or project context</option>
                  {scenarioParentOptions.map((context) => (
                    <option key={context.uuid} value={context.uuid}>{context.name}</option>
                  ))}
                </select>
              </label>
            ) : (
              <div className="field-span-full inline-banner inline-banner-info">
                <strong>Org contexts are roots</strong>
                <span>Org contexts do not take a parent or project during creation.</span>
              </div>
            )}

            {createDraft.kind === 'project' ? (
              <>
                <label className="field-label">
                  <span>Workspace project</span>
                  <select
                    className="select-input"
                    value={createDraft.projectUuid}
                    onChange={(event) => setCreateDraft((currentValue) => ({ ...currentValue, projectUuid: event.target.value }))}
                    required
                    disabled={projects.length === 0}
                  >
                    <option value="">{projects.length > 0 ? 'Select a workspace project' : 'Create a project above first'}</option>
                    {projects.map((project) => (
                      <option key={project.uuid} value={project.uuid}>{project.name}</option>
                    ))}
                  </select>
                </label>
                {projects.length > 0 ? (
                  <div className="field-span-full inline-banner inline-banner-info">
                    <strong>Need another project?</strong>
                    <span>The workspace-project form above adds a new option here immediately.</span>
                  </div>
                ) : null}
              </>
            ) : null}

            <label className="field-label field-span-full">
              <span>Description</span>
              <textarea
                className="textarea-input"
                rows={4}
                value={createDraft.description}
                onChange={(event) => setCreateDraft((currentValue) => ({ ...currentValue, description: event.target.value }))}
              />
            </label>

            <div className="form-actions field-span-full">
              <button
                className="primary-button"
                type="submit"
                disabled={busyAction !== null || (createDraft.kind === 'project' && projects.length === 0)}
              >
                Create context
              </button>
            </div>
          </form>
        </section>
      </div>

      {detail && editorDraft ? (
        <>
          <section className="board-panel">
            <div className="panel-heading">
              <span className="section-tag">Selected Context</span>
              <h3>{detail.name}</h3>
              <p>Update the context identity, profile overrides, and inherited behavior. Parent and project links remain read-only after creation.</p>
            </div>

            <div className="summary-grid">
              <article className="summary-card">
                <span className="summary-label">Kind</span>
                <strong>{humanizeToken(detail.kind)}</strong>
                <p>{detail.parent_context ? `Parent: ${detail.parent_context.name}` : 'Root context'}</p>
              </article>
              <article className="summary-card">
                <span className="summary-label">Project</span>
                <strong>{detail.project?.name || 'No linked project'}</strong>
                <p>{detail.project ? detail.project.uuid : 'Project selection is only used for project contexts.'}</p>
              </article>
              <article className="summary-card">
                <span className="summary-label">Updated</span>
                <strong>{formatDateTime(detail.updated_at)}</strong>
                <p>{detail.sources.length} effective source link(s)</p>
              </article>
            </div>

            <form className="detail-stack" onSubmit={(event) => void handleSaveContext(event)}>
              <div className="profile-form-grid">
                <label className="field-label">
                  <span>Name</span>
                  <input
                    className="text-input"
                    value={editorDraft.name}
                    onChange={(event) => setEditorDraft((currentValue) => currentValue ? { ...currentValue, name: event.target.value } : currentValue)}
                    required
                    disabled={isArchivedDetailContext}
                  />
                </label>
                <label className="field-label">
                  <span>Slug</span>
                  <input
                    className="text-input"
                    value={editorDraft.slug}
                    onChange={(event) => setEditorDraft((currentValue) => currentValue ? { ...currentValue, slug: event.target.value } : currentValue)}
                    required
                    disabled={isArchivedDetailContext}
                  />
                </label>
                <label className="field-label">
                  <span>Status</span>
                  <select
                    className="select-input"
                    value={editorDraft.status}
                    onChange={(event) => setEditorDraft((currentValue) => currentValue ? { ...currentValue, status: event.target.value } : currentValue)}
                    disabled={isArchivedDetailContext}
                  >
                    {CONTEXT_STATUS_OPTIONS.map((option) => (
                      <option key={option.value} value={option.value}>{option.label}</option>
                    ))}
                  </select>
                </label>

                <label className="field-label field-span-full">
                  <span>Description</span>
                  <textarea
                    className="textarea-input"
                    rows={4}
                    value={editorDraft.description}
                    onChange={(event) => setEditorDraft((currentValue) => currentValue ? { ...currentValue, description: event.target.value } : currentValue)}
                    disabled={isArchivedDetailContext}
                  />
                </label>

                <label className="field-label field-span-full">
                  <span>Metadata JSON</span>
                  <textarea
                    className="textarea-input"
                    rows={6}
                    value={editorDraft.metadataText}
                    onChange={(event) => setEditorDraft((currentValue) => currentValue ? { ...currentValue, metadataText: event.target.value } : currentValue)}
                    disabled={isArchivedDetailContext}
                  />
                </label>

                <label className="checkbox-row">
                  <input
                    type="checkbox"
                    checked={detail.kind === 'org' && detail.parent_context === null ? false : editorDraft.inheritFromParent}
                    onChange={(event) => setEditorDraft((currentValue) => currentValue ? { ...currentValue, inheritFromParent: event.target.checked } : currentValue)}
                    disabled={isArchivedDetailContext || (detail.kind === 'org' && detail.parent_context === null)}
                  />
                  <span>Inherit from parent</span>
                </label>

                <label className="field-label">
                  <span>Tech stack additions</span>
                  <textarea
                    className="textarea-input textarea-input-compact"
                    rows={5}
                    value={editorDraft.techStackText}
                    onChange={(event) => setEditorDraft((currentValue) => currentValue ? { ...currentValue, techStackText: event.target.value } : currentValue)}
                    disabled={isArchivedDetailContext}
                  />
                </label>
                <label className="field-label">
                  <span>Tech stack removals</span>
                  <textarea
                    className="textarea-input textarea-input-compact"
                    rows={5}
                    value={editorDraft.techStackRemoveText}
                    onChange={(event) => setEditorDraft((currentValue) => currentValue ? { ...currentValue, techStackRemoveText: event.target.value } : currentValue)}
                    disabled={isArchivedDetailContext}
                  />
                </label>
                <label className="field-label">
                  <span>Constraints</span>
                  <textarea
                    className="textarea-input textarea-input-compact"
                    rows={5}
                    value={editorDraft.constraintsText}
                    onChange={(event) => setEditorDraft((currentValue) => currentValue ? { ...currentValue, constraintsText: event.target.value } : currentValue)}
                    disabled={isArchivedDetailContext}
                  />
                </label>
                <label className="field-label">
                  <span>Growth goals</span>
                  <textarea
                    className="textarea-input textarea-input-compact"
                    rows={5}
                    value={editorDraft.growthGoalsText}
                    onChange={(event) => setEditorDraft((currentValue) => currentValue ? { ...currentValue, growthGoalsText: event.target.value } : currentValue)}
                    disabled={isArchivedDetailContext}
                  />
                </label>
                <label className="field-label">
                  <span>Override fields</span>
                  <textarea
                    className="textarea-input textarea-input-compact"
                    rows={5}
                    value={editorDraft.overrideFieldsText}
                    onChange={(event) => setEditorDraft((currentValue) => currentValue ? { ...currentValue, overrideFieldsText: event.target.value } : currentValue)}
                    disabled={isArchivedDetailContext}
                  />
                </label>
                <label className="field-label field-span-full">
                  <span>Company profile override JSON</span>
                  <textarea
                    className="textarea-input"
                    rows={8}
                    value={editorDraft.companyProfileText}
                    onChange={(event) => setEditorDraft((currentValue) => currentValue ? { ...currentValue, companyProfileText: event.target.value } : currentValue)}
                    disabled={isArchivedDetailContext}
                  />
                </label>
              </div>

              <div className="form-actions">
                <button className="primary-button" type="submit" disabled={busyAction !== null || isArchivedDetailContext}>
                  Save context
                </button>
              </div>
            </form>
          </section>

          <section className="workspace-entry-grid">
            <section className="board-panel">
              <div className="panel-heading">
                <span className="section-tag">Effective Profile</span>
                <h3>Resolved context view</h3>
                <p>This preview reflects parent inheritance plus the overrides stored on the selected context.</p>
              </div>
              <pre className="code-block">{stringifyJson(detail.effective_profile)}</pre>
            </section>

            <section className="board-panel">
              <div className="panel-heading">
                <span className="section-tag">Source Overrides</span>
                <h3>Effective source linkage</h3>
                <p>Each workspace source can be overridden directly at this context without archiving the underlying source.</p>
              </div>

              <div className="review-card-grid">
                {workspaceSources.map((source) => {
                  const link = sourceLinksBySourceUuid.get(source.uuid) ?? null
                  const draft = getSourceDraft(source)
                  const isSourceEditable = !isArchivedDetailContext && source.status !== 'archived'
                  const canDeleteSourceOverride = !isArchivedDetailContext
                  return (
                    <article key={source.uuid} className="review-card">
                      <div className="review-card-head">
                        <div>
                          <span className="summary-label">{getSourceKindLabel(source.source_kind)}</span>
                          <h4>{source.title}</h4>
                        </div>
                        <StatusChip
                          status={
                            link?.origin === 'excluded'
                              ? 'blocked'
                              : link?.origin === 'direct'
                                ? 'completed'
                                : link?.origin === 'inherited'
                                  ? 'ready'
                                  : 'not_started'
                          }
                        />
                      </div>

                      <div className="detail-stack">
                        <p>{getSourceOriginCopy(link)}</p>
                        <p>Workspace status: {humanizeToken(source.status)}</p>
                        {source.status === 'archived' ? (
                          <p>This source is archived in the workspace library, so its context linkage is shown read-only.</p>
                        ) : null}
                      </div>

                      <div className="profile-form-grid">
                        <label className="field-label">
                          <span>Usage type</span>
                          <select
                            className="select-input"
                            value={draft.usage_type}
                            onChange={(event) => {
                              const usageType = event.target.value
                              const nextRoadmapDefault = defaultRoadmapInclusion(usageType)
                              const previousRoadmapDefault = defaultRoadmapInclusion(draft.usage_type)
                              updateSourceDraft(source.uuid, {
                                usage_type: usageType,
                                include_in_roadmap_analysis:
                                  draft.include_in_roadmap_analysis === previousRoadmapDefault
                                    ? nextRoadmapDefault
                                    : draft.include_in_roadmap_analysis,
                              })
                            }}
                            disabled={!isSourceEditable}
                          >
                            {SOURCE_USAGE_OPTIONS.map((option) => (
                              <option key={option.value} value={option.value}>{option.label}</option>
                            ))}
                          </select>
                        </label>

                        <label className="checkbox-row">
                          <input
                            type="checkbox"
                            checked={draft.include_in_blueprint}
                            onChange={(event) => updateSourceDraft(source.uuid, { include_in_blueprint: event.target.checked })}
                            disabled={!isSourceEditable}
                          />
                          <span>Include in blueprint</span>
                        </label>

                        <label className="checkbox-row">
                          <input
                            type="checkbox"
                            checked={draft.include_in_roadmap_analysis}
                            onChange={(event) => updateSourceDraft(source.uuid, { include_in_roadmap_analysis: event.target.checked })}
                            disabled={!isSourceEditable}
                          />
                          <span>Include in roadmap analysis</span>
                        </label>

                        <label className="checkbox-row">
                          <input
                            type="checkbox"
                            checked={draft.is_active}
                            onChange={(event) => updateSourceDraft(source.uuid, { is_active: event.target.checked })}
                            disabled={!isSourceEditable}
                          />
                          <span>Active in this context</span>
                        </label>
                      </div>

                      <div className="form-actions">
                        <button
                          className="primary-button"
                          type="button"
                          onClick={() => void handleSaveSourceOverride(source)}
                          disabled={busyAction !== null || !isSourceEditable}
                        >
                          Save override
                        </button>
                        {canDeleteDirectSourceOverride(link) ? (
                          <button
                            className="secondary-button"
                            type="button"
                            onClick={() => link && void handleDeleteSourceOverride(link)}
                            disabled={busyAction !== null || !canDeleteSourceOverride}
                          >
                            Remove override
                          </button>
                        ) : null}
                      </div>
                    </article>
                  )
                })}
              </div>
            </section>
          </section>
        </>
      ) : (
        <section className="board-panel">
          <EmptyState
            title="No context selected"
            description="Legacy workspace mode is active. Choose a context from the list to edit overrides and scope downstream workflow pages."
          />
        </section>
      )}
    </div>
  )
}
