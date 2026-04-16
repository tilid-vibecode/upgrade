import { useEffect, useMemo, useRef, useState, type ChangeEvent, type FormEvent } from 'react'

import { AppLink, useNavigationBlocker } from '../app/navigation'
import { useWorkspaceShell } from '../app/WorkspaceLayout'
import { CollapsibleHero } from '../shared/ui/CollapsibleHero'
import { getApiErrorMessage, getApiErrorMessages } from '../shared/api'
import { formatDateTime, formatFileSize } from '../shared/formatters'
import {
  archivePrototypeWorkspaceSource,
  createPrototypeWorkspaceSource,
  getPrototypeMediaSignedUrl,
  getPrototypeWorkspaceReadiness,
  getPrototypeWorkspaceSourceDownload,
  listPrototypeMediaFiles,
  listPrototypeWorkspaceSources,
  parsePrototypeWorkspaceSources,
  reparsePrototypeWorkspaceSource,
  updatePrototypeWorkspaceSource,
  uploadPrototypeMediaFile,
  type PrototypeMediaFile,
  type PrototypeWorkspaceReadinessResponse,
  type PrototypeWorkspaceSource,
} from '../shared/prototypeApi'
import {
  areSourceEditorDraftsEqual,
  buildAttachDraftFromMedia,
  buildSourceEditorDraft,
  buildSourceTransportSummary,
  canArchiveSource,
  canEditSource,
  canOpenSource,
  canRunSourceParse,
  canRunSourceReparse,
  canSelectSourceForParse,
  getAcceptedFileCategoryHint,
  getSourceKindDescription,
  getSourceKindLabel,
  getSourceOpenLabel,
  getSourceTransportLabel,
  getSourceWarningCount,
  isFileCategoryCompatibleWithSourceKind,
  SOURCE_KIND_OPTIONS,
} from '../shared/sourceLibrary'
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

type AttachDraft = ReturnType<typeof buildAttachDraftFromMedia>
type SourceEditorDraft = ReturnType<typeof buildSourceEditorDraft>

type SourceRequirementCardProps = {
  requirement: PrototypeWorkspaceReadinessResponse['source_requirements'][number]
}

const FILE_INPUT_ACCEPT =
  '.pdf,.txt,.csv,.doc,.docx,.xls,.xlsx,.png,.jpg,.jpeg,.webp,.md,application/pdf,text/*'

function SourceRequirementCard({ requirement }: SourceRequirementCardProps) {
  const status = requirement.is_parsed_ready
    ? 'completed'
    : requirement.is_satisfied
      ? 'action_required'
      : requirement.required
        ? 'blocked'
        : 'ready'

  return (
    <article className="requirement-item">
      <div className="requirement-item-head">
        <div>
          <strong>{requirement.label}</strong>
          <div className="requirement-tag-row">
            <span className="quiet-pill">{requirement.required ? 'Required' : 'Optional'}</span>
            {requirement.required_for_parse ? <span className="quiet-pill">Parse gate</span> : null}
            {requirement.required_for_blueprint ? <span className="quiet-pill">Blueprint gate</span> : null}
            {requirement.required_for_evidence ? <span className="quiet-pill">Evidence gate</span> : null}
          </div>
        </div>
        <StatusChip status={status} />
      </div>

      <p className="form-helper-copy">
        Attached {requirement.attached_count}/{requirement.required_min_count} and parsed {requirement.parsed_count}/
        {requirement.required_min_count}.
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

function buildParseResultBanner(processed: number, results: Array<{ status: string; parse_error: string }>): BannerState {
  const failedCount = results.filter((item) => item.status === 'failed').length
  const parsedCount = results.filter((item) => item.status === 'parsed').length
  const warningMessages = results
    .filter((item) => item.status === 'failed' && item.parse_error)
    .map((item) => item.parse_error)

  if (failedCount > 0) {
    return {
      tone: 'warn',
      title: `Processed ${processed} source${processed === 1 ? '' : 's'} with ${failedCount} failure${failedCount === 1 ? '' : 's'}.`,
      messages: warningMessages.slice(0, 3),
    }
  }

  return {
    tone: 'success',
    title: `Processed ${processed} source${processed === 1 ? '' : 's'}.`,
    messages: [`${parsedCount} source${parsedCount === 1 ? '' : 's'} are now parsed or confirmed as already parsed.`],
  }
}

function buildReparseBanner(source: PrototypeWorkspaceSource): BannerState {
  const warningCount = getSourceWarningCount(source)

  return {
    tone: source.status === 'failed' ? 'warn' : 'success',
    title: source.status === 'failed' ? `${source.title} failed to parse again.` : `${source.title} reparsed successfully.`,
    messages:
      source.status === 'failed'
        ? [source.parse_error || 'The parser reported a failure.']
        : warningCount > 0
          ? [`Parser warnings: ${warningCount}. Review this source on the parse page if needed.`]
          : ['The source is ready for the parse-stage review flow.'],
  }
}

function openUrl(url: string) {
  const popup = window.open(url, '_blank', 'noopener,noreferrer')

  if (popup === null) {
    window.location.assign(url)
  }
}

export default function WorkspaceSourcesPage() {
  const {
    workspace,
    workflow,
    activePlanningContext,
    planningContextOptions,
    refreshShell,
    buildScopedWorkspacePath,
  } = useWorkspaceShell()
  const [readiness, setReadiness] = useState<PrototypeWorkspaceReadinessResponse | null>(null)
  const [sources, setSources] = useState<PrototypeWorkspaceSource[]>([])
  const [workspaceMedia, setWorkspaceMedia] = useState<PrototypeMediaFile[]>([])
  const [isLoading, setIsLoading] = useState(true)
  const [loadError, setLoadError] = useState<string | null>(null)
  const [reloadToken, setReloadToken] = useState(0)
  const [preferredMediaUuid, setPreferredMediaUuid] = useState<string | null>(null)
  const [includeArchived, setIncludeArchived] = useState(false)
  const [banner, setBanner] = useState<BannerState | null>(null)
  const [uploadFile, setUploadFile] = useState<File | null>(null)
  const [uploadInputKey, setUploadInputKey] = useState(0)
  const [isUploading, setIsUploading] = useState(false)
  const [selectedMediaUuid, setSelectedMediaUuid] = useState<string | null>(null)
  const [attachDraft, setAttachDraft] = useState<AttachDraft | null>(null)
  const [isAttaching, setIsAttaching] = useState(false)
  const [selectedSourceIds, setSelectedSourceIds] = useState<string[]>([])
  const [isBulkParsing, setIsBulkParsing] = useState(false)
  const [rowActionKey, setRowActionKey] = useState<string | null>(null)
  const [editingSourceUuid, setEditingSourceUuid] = useState<string | null>(null)
  const [editorDraft, setEditorDraft] = useState<SourceEditorDraft | null>(null)
  const [isSavingEditor, setIsSavingEditor] = useState(false)

  const selectedMediaUuidRef = useRef<string | null>(selectedMediaUuid)
  const editingSourceUuidRef = useRef<string | null>(editingSourceUuid)
  const hasDirtyEditorRef = useRef(false)

  selectedMediaUuidRef.current = selectedMediaUuid
  editingSourceUuidRef.current = editingSourceUuid

  const activeSources = useMemo(
    () => sources.filter((source) => source.status !== 'archived'),
    [sources],
  )
  const attachedActiveMediaUuids = useMemo(
    () =>
      new Set(
        activeSources
          .map((source) => source.media_file_uuid)
          .filter((mediaFileUuid): mediaFileUuid is string => Boolean(mediaFileUuid)),
      ),
    [activeSources],
  )
  const availableMedia = useMemo(
    () => workspaceMedia.filter((mediaFile) => !attachedActiveMediaUuids.has(mediaFile.uuid)),
    [attachedActiveMediaUuids, workspaceMedia],
  )
  const selectedMedia =
    availableMedia.find((mediaFile) => mediaFile.uuid === selectedMediaUuid) ??
    null
  const parseEligibleSources = activeSources.filter(canSelectSourceForParse)
  const editingSource =
    sources.find((source) => source.uuid === editingSourceUuid) ??
    null
  const savedEditorDraft = editingSource ? buildSourceEditorDraft(editingSource) : null
  const hasDirtyEditor =
    editingSource !== null &&
    editorDraft !== null &&
    savedEditorDraft !== null &&
    !areSourceEditorDraftsEqual(editorDraft, savedEditorDraft)
  const editorTriggersReparse =
    editingSource !== null &&
    editorDraft !== null &&
    (
      editorDraft.source_kind !== editingSource.source_kind ||
      editorDraft.language_code !== editingSource.language_code ||
      editorDraft.external_url !== editingSource.external_url ||
      editorDraft.inline_text !== editingSource.inline_text
    )
  const defaultAttachDraft = selectedMedia ? buildAttachDraftFromMedia(selectedMedia) : null
  const hasDirtyAttachDraft =
    selectedMedia !== null &&
    attachDraft !== null &&
    defaultAttachDraft !== null &&
    (
      attachDraft.source_kind !== defaultAttachDraft.source_kind ||
      attachDraft.title !== defaultAttachDraft.title ||
      attachDraft.notes !== defaultAttachDraft.notes ||
      attachDraft.language_code !== defaultAttachDraft.language_code
    )
  const isPageBusy =
    isUploading ||
    isAttaching ||
    isBulkParsing ||
    rowActionKey !== null ||
    isSavingEditor

  hasDirtyEditorRef.current = hasDirtyEditor

  useNavigationBlocker(
    hasDirtyEditor || hasDirtyAttachDraft,
    hasDirtyEditor
      ? 'You have unsaved source edits. Leave this page without saving?'
      : 'You have an unsaved source attachment draft. Leave this page without attaching it?',
  )

  useEffect(() => {
    let cancelled = false

    async function loadPageData() {
      setIsLoading(true)
      setLoadError(null)

      try {
        const [readinessResponse, sourcesResponse, mediaResponse] = await Promise.all([
          getPrototypeWorkspaceReadiness(workspace.slug, planningContextOptions),
          listPrototypeWorkspaceSources(workspace.slug, { includeArchived }),
          listPrototypeMediaFiles({ workspaceSlug: workspace.slug, limit: 200 }),
        ])

        if (cancelled) {
          return
        }

        setReadiness(readinessResponse)
        setSources(sourcesResponse.sources)
        setWorkspaceMedia(mediaResponse.files)

        const activeAttachedMediaUuids = new Set(
          sourcesResponse.sources
            .filter((source) => source.status !== 'archived')
            .map((source) => source.media_file_uuid)
            .filter((mediaFileUuid): mediaFileUuid is string => Boolean(mediaFileUuid)),
        )
        const nextAvailableMedia = mediaResponse.files.filter(
          (mediaFile) => !activeAttachedMediaUuids.has(mediaFile.uuid),
        )
        const desiredMediaUuid = preferredMediaUuid ?? selectedMediaUuidRef.current
        const nextSelectedMedia =
          nextAvailableMedia.find((mediaFile) => mediaFile.uuid === desiredMediaUuid) ??
          null

        setSelectedMediaUuid(nextSelectedMedia?.uuid ?? null)
        setAttachDraft((currentDraft) => {
          if (nextSelectedMedia === null) {
            return null
          }

          if (selectedMediaUuidRef.current === nextSelectedMedia.uuid && currentDraft !== null) {
            return currentDraft
          }

          return buildAttachDraftFromMedia(nextSelectedMedia)
        })

        const currentEditingUuid = editingSourceUuidRef.current
        const nextEditingSource = currentEditingUuid
          ? sourcesResponse.sources.find((source) => source.uuid === currentEditingUuid) ?? null
          : null

        if (currentEditingUuid && nextEditingSource === null) {
          setEditingSourceUuid(null)
          setEditorDraft(null)
        } else if (nextEditingSource !== null && !hasDirtyEditorRef.current) {
          setEditorDraft(buildSourceEditorDraft(nextEditingSource))
        }

        setSelectedSourceIds((currentIds) =>
          currentIds.filter((sourceUuid) =>
            sourcesResponse.sources.some(
              (source) => source.uuid === sourceUuid && canSelectSourceForParse(source),
            ),
          ),
        )
      } catch (requestError) {
        if (!cancelled) {
          setLoadError(getApiErrorMessage(requestError, 'Failed to load the source library.'))
        }
      } finally {
        if (!cancelled) {
          setIsLoading(false)
        }
      }
    }

    loadPageData()

    return () => {
      cancelled = true
    }
  }, [workspace.slug, planningContextOptions, includeArchived, reloadToken, preferredMediaUuid])

  const reloadPage = (nextPreferredMediaUuid?: string | null) => {
    if (nextPreferredMediaUuid !== undefined) {
      setPreferredMediaUuid(nextPreferredMediaUuid)
    }
    setReloadToken((value) => value + 1)
  }

  const handleUploadFileChange = (event: ChangeEvent<HTMLInputElement>) => {
    const nextFile = event.target.files?.[0] ?? null
    setUploadFile(nextFile)
  }

  const confirmDiscardAttachDraftChanges = async () => {
    if (!hasDirtyAttachDraft) {
      return true
    }

    return requestGlobalConfirmation({
      title: 'Discard source attachment draft?',
      description: 'Switching to a different upload will discard the current unsaved source attachment draft.',
      confirmLabel: 'Discard draft',
      cancelLabel: 'Keep editing',
      tone: 'warn',
    })
  }

  const handleSelectMediaForAttach = async (mediaFile: PrototypeMediaFile) => {
    if (
      selectedMediaUuid !== null &&
      selectedMediaUuid !== mediaFile.uuid &&
      !(await confirmDiscardAttachDraftChanges())
    ) {
      return false
    }

    setSelectedMediaUuid(mediaFile.uuid)
    setPreferredMediaUuid(mediaFile.uuid)
    setAttachDraft(buildAttachDraftFromMedia(mediaFile))
    setBanner(null)
    return true
  }

  const clearAttachSelection = () => {
    setSelectedMediaUuid(null)
    setPreferredMediaUuid(null)
    setAttachDraft(null)
  }

  const discardEditorChanges = () => {
    if (editingSource) {
      setEditorDraft(buildSourceEditorDraft(editingSource))
      return
    }

    setEditorDraft(null)
  }

  const confirmDiscardEditorChanges = async () => {
    if (!hasDirtyEditor) {
      return true
    }

    return requestGlobalConfirmation({
      title: 'Discard source edits?',
      description: 'Your unsaved source edits will be lost if you continue.',
      confirmLabel: 'Discard edits',
      cancelLabel: 'Keep editing',
      tone: 'warn',
    })
  }

  const discardEditorChangesForAction = async () => {
    if (!(await confirmDiscardEditorChanges())) {
      return false
    }

    discardEditorChanges()
    return true
  }

  const openSourceEditor = async (source: PrototypeWorkspaceSource) => {
    if (editingSourceUuid === source.uuid) {
      return
    }

    if (!(await confirmDiscardEditorChanges())) {
      return
    }

    setEditingSourceUuid(source.uuid)
    setEditorDraft(buildSourceEditorDraft(source))
  }

  const closeSourceEditor = async () => {
    if (!(await confirmDiscardEditorChanges())) {
      return
    }

    setEditingSourceUuid(null)
    setEditorDraft(null)
  }

  const handleUploadSubmit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault()

    if (isPageBusy) {
      return
    }

    if (uploadFile === null) {
      setBanner({
        tone: 'warn',
        title: 'Choose a file before uploading.',
        messages: ['Stage 04 uses the media-first flow: upload a raw file, then attach it as a workspace source.'],
      })
      return
    }

    setIsUploading(true)
    setBanner(null)

    try {
      const response = await uploadPrototypeMediaFile({
        file: uploadFile,
        workspaceSlug: workspace.slug,
      })

      setUploadFile(null)
      setUploadInputKey((value) => value + 1)
      setWorkspaceMedia((currentFiles) => [
        response.file,
        ...currentFiles.filter((mediaFile) => mediaFile.uuid !== response.file.uuid),
      ])
      setBanner({
        tone: 'success',
        title: `${response.file.original_filename} uploaded successfully.`,
        messages: ['Classify it below to turn the upload into a workspace source.'],
      })
      const selectedUploadedFile = await handleSelectMediaForAttach(response.file)
      reloadPage(selectedUploadedFile ? response.file.uuid : selectedMediaUuidRef.current)
    } catch (requestError) {
      setBanner({
        tone: 'error',
        title: 'Upload failed.',
        messages: getApiErrorMessages(requestError),
      })
    } finally {
      setIsUploading(false)
    }
  }

  const handleAttachSubmit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault()

    if (isPageBusy) {
      return
    }

    if (selectedMedia === null || attachDraft === null) {
      setBanner({
        tone: 'warn',
        title: 'Choose an uploaded file before attaching it.',
        messages: ['You can upload a fresh file or reuse one from the recovery list on the right.'],
      })
      return
    }

    if (!attachDraft.title.trim()) {
      setBanner({
        tone: 'warn',
        title: 'Add a source title before attaching this file.',
        messages: ['The title is what operators will see throughout the workspace source library.'],
      })
      return
    }

    setIsAttaching(true)
    setBanner(null)

    try {
      const source = await createPrototypeWorkspaceSource(workspace.slug, {
        source_kind: attachDraft.source_kind,
        transport: 'media_file',
        media_file_uuid: selectedMedia.uuid,
        title: attachDraft.title.trim(),
        notes: attachDraft.notes.trim(),
        language_code: attachDraft.language_code.trim(),
      })

      clearAttachSelection()
      refreshShell()
      setBanner({
        tone: 'success',
        title: `${source.title} is now attached as a workspace source.`,
        messages: ['Use Parse all pending or the row-level Parse action when you are ready to extract structured content.'],
      })
      reloadPage(null)
    } catch (requestError) {
      setBanner({
        tone: 'error',
        title: 'Failed to attach the uploaded file as a source.',
        messages: getApiErrorMessages(requestError),
      })
    } finally {
      setIsAttaching(false)
    }
  }

  const handleToggleSourceSelection = (sourceUuid: string) => {
    setSelectedSourceIds((currentIds) =>
      currentIds.includes(sourceUuid)
        ? currentIds.filter((item) => item !== sourceUuid)
        : [...currentIds, sourceUuid],
    )
  }

  const handleParseBatch = async (mode: 'selected' | 'all_pending') => {
    if (isPageBusy) {
      return
    }

    if (!(await discardEditorChangesForAction())) {
      return
    }

    const sourceUuids =
      mode === 'selected'
        ? selectedSourceIds
        : parseEligibleSources.map((source) => source.uuid)

    if (sourceUuids.length === 0) {
      setBanner({
        tone: 'warn',
        title: mode === 'selected' ? 'Select at least one source to parse.' : 'There are no pending or failed sources to parse.',
        messages: ['Already parsed sources stay available for row-level reparse so bulk actions stay focused on pending work.'],
      })
      return
    }

    setIsBulkParsing(true)
    setBanner(null)

    try {
      const response = await parsePrototypeWorkspaceSources(workspace.slug, {
        source_uuids: sourceUuids,
        force: false,
      })

      setSelectedSourceIds([])
      refreshShell()
      setBanner(buildParseResultBanner(response.processed, response.results))
      reloadPage()
    } catch (requestError) {
      setBanner({
        tone: 'error',
        title: 'Bulk parse failed.',
        messages: getApiErrorMessages(requestError),
      })
    } finally {
      setIsBulkParsing(false)
    }
  }

  const handleParseSingleSource = async (source: PrototypeWorkspaceSource) => {
    if (isPageBusy) {
      return
    }

    if (!(await discardEditorChangesForAction())) {
      return
    }

    setRowActionKey(`parse:${source.uuid}`)
    setBanner(null)

    try {
      const response = await parsePrototypeWorkspaceSources(workspace.slug, {
        source_uuids: [source.uuid],
        force: false,
      })

      refreshShell()
      setBanner(buildParseResultBanner(response.processed, response.results))
      reloadPage()
    } catch (requestError) {
      setBanner({
        tone: 'error',
        title: `Failed to parse ${source.title}.`,
        messages: getApiErrorMessages(requestError),
      })
    } finally {
      setRowActionKey(null)
    }
  }

  const handleReparseSingleSource = async (source: PrototypeWorkspaceSource) => {
    if (isPageBusy) {
      return
    }

    if (!(await discardEditorChangesForAction())) {
      return
    }

    setRowActionKey(`reparse:${source.uuid}`)
    setBanner(null)

    try {
      const response = await reparsePrototypeWorkspaceSource(workspace.slug, source.uuid)

      refreshShell()
      setBanner(buildReparseBanner(response.source))
      reloadPage()
    } catch (requestError) {
      setBanner({
        tone: 'error',
        title: `Failed to reparse ${source.title}.`,
        messages: getApiErrorMessages(requestError),
      })
    } finally {
      setRowActionKey(null)
    }
  }

  const handleOpenSource = async (source: PrototypeWorkspaceSource) => {
    if (isPageBusy) {
      return
    }

    setRowActionKey(`open:${source.uuid}`)

    try {
      if (source.transport === 'external_url') {
        openUrl(source.external_url)
        return
      }

      const download = await getPrototypeWorkspaceSourceDownload(workspace.slug, source.uuid)
      openUrl(download.url)
    } catch (requestError) {
      setBanner({
        tone: 'error',
        title: `Could not open ${source.title}.`,
        messages: getApiErrorMessages(requestError),
      })
    } finally {
      setRowActionKey(null)
    }
  }

  const handleOpenMedia = async (mediaFile: PrototypeMediaFile) => {
    if (isPageBusy) {
      return
    }

    setRowActionKey(`media:${mediaFile.uuid}`)

    try {
      const download = await getPrototypeMediaSignedUrl(mediaFile.uuid, workspace.slug)
      openUrl(download.url)
    } catch (requestError) {
      setBanner({
        tone: 'error',
        title: `Could not open ${mediaFile.original_filename}.`,
        messages: getApiErrorMessages(requestError),
      })
    } finally {
      setRowActionKey(null)
    }
  }

  const handleArchiveSource = async (source: PrototypeWorkspaceSource) => {
    if (isPageBusy) {
      return
    }

    if (!(await discardEditorChangesForAction())) {
      return
    }

    if (!(await requestGlobalConfirmation({
      title: `Archive ${source.title}?`,
      description: 'This source will be removed from the active source library and downstream review flows.',
      details: ['Archived sources remain viewable through the include-archived toggle.'],
      confirmLabel: 'Archive source',
      cancelLabel: 'Keep source',
      tone: 'danger',
    }))) {
      return
    }

    setRowActionKey(`archive:${source.uuid}`)
    setBanner(null)

    try {
      await archivePrototypeWorkspaceSource(workspace.slug, source.uuid)

      if (editingSourceUuid === source.uuid) {
        setEditingSourceUuid(null)
        setEditorDraft(null)
      }

      refreshShell()
      setBanner({
        tone: 'success',
        title: `${source.title} was archived.`,
        messages: ['Archived sources stay available through the include-archived toggle if you need to review or re-attach related uploads later.'],
      })
      reloadPage()
    } catch (requestError) {
      setBanner({
        tone: 'error',
        title: `Failed to archive ${source.title}.`,
        messages: getApiErrorMessages(requestError),
      })
    } finally {
      setRowActionKey(null)
    }
  }

  const handleSaveEditor = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault()

    if (editingSource === null || editorDraft === null) {
      return
    }

    if (!editorDraft.title.trim()) {
      setBanner({
        tone: 'warn',
        title: 'Source title cannot be empty.',
        messages: ['Keep titles operator-readable so the source library remains scan-friendly.'],
      })
      return
    }

    setIsSavingEditor(true)
    setBanner(null)

    try {
      const updatedSource = await updatePrototypeWorkspaceSource(workspace.slug, editingSource.uuid, {
        source_kind: editorDraft.source_kind,
        title: editorDraft.title.trim(),
        notes: editorDraft.notes.trim(),
        language_code: editorDraft.language_code.trim(),
        external_url: editingSource.transport === 'external_url' ? editorDraft.external_url.trim() : undefined,
        inline_text: editingSource.transport === 'inline_text' ? editorDraft.inline_text : undefined,
      })

      setEditorDraft(buildSourceEditorDraft(updatedSource))
      refreshShell()
      setBanner({
        tone: editorTriggersReparse ? 'info' : 'success',
        title: editorTriggersReparse ? `${updatedSource.title} was updated and moved back to Attached.` : `${updatedSource.title} was updated.`,
        messages: editorTriggersReparse
          ? ['A source-kind, language, URL, or inline-text change resets parse status so the updated content can be parsed again.']
          : ['Notes and title changes do not trigger a reparse on their own.'],
      })
      reloadPage()
    } catch (requestError) {
      setBanner({
        tone: 'error',
        title: `Failed to save changes for ${editingSource.title}.`,
        messages: getApiErrorMessages(requestError),
      })
    } finally {
      setIsSavingEditor(false)
    }
  }

  const currentStage = workflow.stages.find((stage) => stage.key === 'sources') ?? null
  const parseStage = workflow.stages.find((stage) => stage.key === 'parse') ?? null
  const sourceCoverageCount = readiness?.source_requirements.filter((requirement) => requirement.is_satisfied).length ?? 0
  const totalRequirementCount = readiness?.source_requirements.length ?? 0
  const parseBlockers = readiness?.stage_blockers.parse ?? []
  const selectedParseCount = selectedSourceIds.length
  const attachCategoryHint = attachDraft ? getAcceptedFileCategoryHint(attachDraft.source_kind) : ''
  const attachCategoryMismatch =
    selectedMedia !== null &&
    attachDraft !== null &&
    !isFileCategoryCompatibleWithSourceKind(attachDraft.source_kind, selectedMedia.file_category)

  if (isLoading && readiness === null) {
    return (
      <LoadingState
        title="Loading source library"
        description="Fetching required source guidance, active workspace sources, and recoverable uploads."
      />
    )
  }

  if (loadError && readiness === null) {
    return (
      <ErrorState
        title="Source library failed to load"
        description={loadError}
        onRetry={() => reloadPage()}
      />
    )
  }

  return (
    <div className="page-stack">
      <CollapsibleHero
        tag="Stage 04"
        title="Source library"
        statusSlot={<StatusChip status={currentStage?.status || 'not_started'} />}
      >
        <div className="hero-copy">
          <p>
            Upload workspace-bound files, attach them as typed sources, and trigger parse actions from one operator surface.
            This page treats attached sources as the canonical library and keeps raw uploads as a recovery layer.
          </p>

          <div className="hero-actions">
            <AppLink className="secondary-button link-button" to={buildScopedWorkspacePath('profile')}>
              Review profile
            </AppLink>
            <AppLink className="secondary-button link-button" to={buildScopedWorkspacePath('parse')}>
              Open parse page
            </AppLink>
          </div>

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
        </div>

        <div className="route-badge">
          <span className="summary-label">Sources stage</span>
          <strong>{currentStage?.label || 'Sources'}</strong>
          <StatusChip status={currentStage?.status || 'not_started'} />
          <p className="form-helper-copy">
            {currentStage?.recommended_action || 'Upload and attach the workspace source set before moving deeper into parsing.'}
          </p>
        </div>
      </CollapsibleHero>

      {activePlanningContext ? (
        <section className="inline-banner inline-banner-info">
          <strong>Source library editing stays workspace-owned.</strong>
          <span>
            This page still edits the shared workspace source library. Use Contexts to override whether a source is active, inherited, or excluded for {activePlanningContext.name}.
          </span>
          <div className="form-actions">
            <AppLink className="secondary-button link-button" to={buildScopedWorkspacePath('contexts')}>
              Manage context overrides
            </AppLink>
          </div>
        </section>
      ) : null}

      <section className="board-panel">
        <div className="panel-heading">
          <span className="section-tag">Requirements</span>
          <h3>Required source coverage and parse gates</h3>
          <p>
            Stage 04 stays grounded in readiness so operators can see which required source groups are attached, parsed, or still blocking the next phase.
          </p>
        </div>

        <div className="summary-grid">
          <article className="summary-card">
            <span className="summary-label">Attached sources</span>
            <strong>{readiness?.total_attached_sources ?? activeSources.length}</strong>
            <p>Active sources in the workspace library.</p>
          </article>
          <article className="summary-card">
            <span className="summary-label">Parsed sources</span>
            <strong>{readiness?.total_parsed_sources ?? activeSources.filter((source) => source.status === 'parsed').length}</strong>
            <p>Sources already parsed into downstream context artifacts.</p>
          </article>
          <article className="summary-card">
            <span className="summary-label">Requirement coverage</span>
            <strong>
              {sourceCoverageCount}/{totalRequirementCount}
            </strong>
            <p>Required or optional source groups currently satisfied.</p>
          </article>
          <article className="summary-card">
            <span className="summary-label">Parse readiness</span>
            <strong>{readiness?.readiness.ready_for_parse ? 'Ready' : 'Blocked'}</strong>
            <StatusChip status={readiness?.readiness.ready_for_parse ? 'ready' : 'blocked'} />
          </article>
        </div>

        {parseBlockers.length > 0 ? (
          <div className="inline-banner inline-banner-warn">
            <strong>Parsing is still blocked.</strong>
            <ul className="inline-detail-list">
              {parseBlockers.map((blocker) => (
                <li key={blocker}>{blocker}</li>
              ))}
            </ul>
          </div>
        ) : (
          <div className="inline-banner inline-banner-success">
            <strong>Parse-stage prerequisites are currently satisfied.</strong>
            <span>You can move to parse actions here or continue to the dedicated parse review page afterward.</span>
          </div>
        )}

        <div className="requirement-list">
          {(readiness?.source_requirements ?? []).map((requirement) => (
            <SourceRequirementCard key={requirement.key} requirement={requirement} />
          ))}
        </div>
      </section>

      <section className="sources-control-grid">
        <article className="board-panel">
          <div className="panel-heading">
            <span className="section-tag">Upload and attach</span>
            <h3>Use the media-first source flow</h3>
            <p>
              Upload a raw file first. Then classify it as a workspace source with the correct kind, title, notes, and optional language hint.
            </p>
          </div>

          <form className="source-upload-form" onSubmit={handleUploadSubmit}>
            <label className="field-label">
              <span>Raw file upload</span>
              <input
                key={uploadInputKey}
                className="text-input"
                type="file"
                accept={FILE_INPUT_ACCEPT}
                disabled={isPageBusy}
                onChange={handleUploadFileChange}
              />
            </label>

            <div className="form-actions">
                  <button className="primary-button" type="submit" disabled={isPageBusy}>
                    {isUploading ? 'Uploading...' : 'Upload file'}
                  </button>
              {uploadFile ? (
                <span className="form-helper-copy">
                  Ready to upload: {uploadFile.name} ({formatFileSize(uploadFile.size)})
                </span>
              ) : (
                <span className="form-helper-copy">Supported files include documents, spreadsheets, text, and image formats accepted by the backend.</span>
              )}
            </div>
          </form>

          {selectedMedia ? (
            <article className="source-selected-media-card">
              <div className="workspace-card-head">
                <div>
                  <span className="section-tag">Selected upload</span>
                  <h4>{selectedMedia.original_filename}</h4>
                </div>
                <StatusChip status={selectedMedia.status} />
              </div>

              <div className="source-meta-grid">
                <div>
                  <span className="summary-label">Category</span>
                  <strong>{humanizeToken(selectedMedia.file_category)}</strong>
                </div>
                <div>
                  <span className="summary-label">Size</span>
                  <strong>{formatFileSize(selectedMedia.file_size)}</strong>
                </div>
                <div>
                  <span className="summary-label">Uploaded</span>
                  <strong>{formatDateTime(selectedMedia.created_at)}</strong>
                </div>
              </div>

              <form className="profile-page-form" onSubmit={handleAttachSubmit}>
                <div className="profile-form-grid">
                  <label className="field-label">
                    <span>Source kind</span>
                    <select
                      className="select-input"
                      value={attachDraft?.source_kind || 'other'}
                      disabled={isPageBusy}
                      onChange={(event) =>
                        setAttachDraft((currentDraft) =>
                          currentDraft
                            ? {
                                ...currentDraft,
                                source_kind: event.target.value,
                              }
                            : currentDraft,
                        )
                      }
                    >
                      {SOURCE_KIND_OPTIONS.map((option) => (
                        <option key={option.value} value={option.value}>
                          {option.label}
                        </option>
                      ))}
                    </select>
                  </label>

                  <label className="field-label">
                    <span>Source title</span>
                    <input
                      className="text-input"
                      value={attachDraft?.title || ''}
                      disabled={isPageBusy}
                      onChange={(event) =>
                        setAttachDraft((currentDraft) =>
                          currentDraft
                            ? {
                                ...currentDraft,
                                title: event.target.value,
                              }
                            : currentDraft,
                        )
                      }
                    />
                  </label>

                  <label className="field-label">
                    <span>Language hint</span>
                    <input
                      className="text-input"
                      value={attachDraft?.language_code || ''}
                      placeholder="en, ru, de..."
                      disabled={isPageBusy}
                      onChange={(event) =>
                        setAttachDraft((currentDraft) =>
                          currentDraft
                            ? {
                                ...currentDraft,
                                language_code: event.target.value,
                              }
                            : currentDraft,
                        )
                      }
                    />
                  </label>

                  <div className="field-label">
                    <span>Compatibility hint</span>
                    <div className="inline-banner inline-banner-info">
                      <strong>{getSourceKindLabel(attachDraft?.source_kind || 'other')}</strong>
                      <span>{getSourceKindDescription(attachDraft?.source_kind || 'other')}</span>
                      <span>Expected file categories: {attachCategoryHint}.</span>
                    </div>
                  </div>

                  <label className="field-label field-span-full">
                    <span>Operator notes</span>
                    <textarea
                      className="textarea-input textarea-input-compact"
                      value={attachDraft?.notes || ''}
                      placeholder="Why this source matters, how current it is, or anything worth flagging before parsing."
                      disabled={isPageBusy}
                      onChange={(event) =>
                        setAttachDraft((currentDraft) =>
                          currentDraft
                            ? {
                                ...currentDraft,
                                notes: event.target.value,
                              }
                            : currentDraft,
                        )
                      }
                    />
                  </label>
                </div>

                {attachCategoryMismatch ? (
                  <div className="inline-banner inline-banner-warn">
                    <strong>Selected file category may not match this source kind.</strong>
                    <span>
                      This upload is a {humanizeToken(selectedMedia.file_category)} file, while {getSourceKindLabel(attachDraft.source_kind).toLowerCase()} usually expects {attachCategoryHint}.
                    </span>
                  </div>
                ) : null}

                <div className="form-actions">
                  <button className="primary-button" type="submit" disabled={isPageBusy}>
                    {isAttaching ? 'Attaching...' : 'Attach source'}
                  </button>
                  <button className="secondary-button" type="button" onClick={clearAttachSelection} disabled={isPageBusy}>
                    Clear selection
                  </button>
                </div>
              </form>
            </article>
          ) : (
            <EmptyState
              title="No upload selected yet"
              description="Upload a new file or choose one from the recovery list to classify it as a workspace source."
            />
          )}
        </article>

        <article className="board-panel">
          <div className="panel-heading">
            <span className="section-tag">Recovery layer</span>
            <h3>Workspace-bound uploads not attached to active sources</h3>
            <p>
              This panel is intentionally secondary. It helps recover raw uploads after refresh without treating the raw media list as the source library itself.
            </p>
          </div>

          {availableMedia.length === 0 ? (
            <EmptyState
              title="No unattached uploads"
              description="Every current workspace upload is already attached to an active source, or nothing has been uploaded yet."
            />
          ) : (
            <div className="media-recovery-list">
              {availableMedia.map((mediaFile) => (
                <article
                  key={mediaFile.uuid}
                  className={selectedMediaUuid === mediaFile.uuid ? 'media-recovery-card is-selected' : 'media-recovery-card'}
                >
                  <div className="workspace-card-head">
                    <div>
                      <h4>{mediaFile.original_filename}</h4>
                      <p className="form-helper-copy">{humanizeToken(mediaFile.file_category)} file</p>
                    </div>
                    <StatusChip status={mediaFile.status} />
                  </div>

                  <div className="source-meta-grid">
                    <div>
                      <span className="summary-label">Size</span>
                      <strong>{formatFileSize(mediaFile.file_size)}</strong>
                    </div>
                    <div>
                      <span className="summary-label">Updated</span>
                      <strong>{formatDateTime(mediaFile.updated_at)}</strong>
                    </div>
                  </div>

                  {mediaFile.processing_description ? <p>{mediaFile.processing_description}</p> : null}
                  {mediaFile.error_msg ? (
                    <div className="inline-banner inline-banner-warn">
                      <strong>Upload warning</strong>
                      <span>{mediaFile.error_msg}</span>
                    </div>
                  ) : null}

                  <div className="form-actions">
                    <button className="primary-button" type="button" onClick={() => void handleSelectMediaForAttach(mediaFile)} disabled={isPageBusy}>
                      {selectedMediaUuid === mediaFile.uuid ? 'Selected' : 'Use this file'}
                    </button>
                    {mediaFile.has_persistent ? (
                      <button
                        className="secondary-button"
                        type="button"
                        onClick={() => handleOpenMedia(mediaFile)}
                        disabled={isPageBusy || rowActionKey === `media:${mediaFile.uuid}`}
                      >
                        {rowActionKey === `media:${mediaFile.uuid}` ? 'Opening...' : 'Open upload'}
                      </button>
                    ) : null}
                  </div>
                </article>
              ))}
            </div>
          )}
        </article>
      </section>

      <section className="board-panel">
        <div className="panel-heading">
          <span className="section-tag">Source library</span>
          <h3>Attached sources, parse status, and row actions</h3>
          <p>
            This table is the authoritative operator view for stage 04. Use bulk parse for pending work and keep row-level reparse for already parsed sources.
          </p>
        </div>

        <div className="sources-toolbar">
          <div className="form-actions">
            <button
              className="primary-button"
              type="button"
              onClick={() => void handleParseBatch('all_pending')}
                disabled={isPageBusy || parseEligibleSources.length === 0}
              >
              {isBulkParsing ? 'Parsing...' : `Parse all pending (${parseEligibleSources.length})`}
            </button>
            <button
              className="secondary-button"
              type="button"
              onClick={() => void handleParseBatch('selected')}
                disabled={isPageBusy || selectedParseCount === 0}
              >
                Parse selected ({selectedParseCount})
              </button>
            {selectedParseCount > 0 ? (
              <button className="secondary-button" type="button" onClick={() => setSelectedSourceIds([])} disabled={isPageBusy}>
                Clear selection
              </button>
            ) : null}
          </div>

          <div className="form-actions">
            <button
              className="secondary-button"
              type="button"
              onClick={() => void (async () => {
                if (!(await confirmDiscardEditorChanges())) {
                  return
                }

                setIncludeArchived((value) => !value)
                discardEditorChanges()
              })()}
              disabled={isPageBusy}
            >
              {includeArchived ? 'Hide archived' : 'Include archived'}
            </button>
            <button className="secondary-button" type="button" onClick={() => reloadPage()} disabled={isPageBusy}>
              Refresh
            </button>
          </div>
        </div>

        {sources.length === 0 ? (
          <EmptyState
            title={includeArchived ? 'No sources found' : 'No active sources attached yet'}
            description={
              includeArchived
                ? 'This workspace does not have attached or archived sources yet.'
                : 'Upload a file and attach it as a workspace source to start building the source library.'
            }
          />
        ) : (
          <div className="source-table-shell">
            <table className="source-table">
              <thead>
                <tr>
                  <th aria-label="Select source">Select</th>
                  <th>Source</th>
                  <th>Transport</th>
                  <th>Status</th>
                  <th>Details</th>
                  <th>Updated</th>
                  <th>Actions</th>
                </tr>
              </thead>
              <tbody>
                {sources.map((source) => {
                  const isSelected = selectedSourceIds.includes(source.uuid)
                  const warningCount = getSourceWarningCount(source)
                  const rowIsBusy = rowActionKey?.endsWith(source.uuid) ?? false

                  return (
                    <tr key={source.uuid} className={editingSourceUuid === source.uuid ? 'is-active' : undefined}>
                      <td>
                        {canSelectSourceForParse(source) ? (
                          <input
                            type="checkbox"
                            checked={isSelected}
                            disabled={isPageBusy}
                            onChange={() => handleToggleSourceSelection(source.uuid)}
                            aria-label={`Select ${source.title}`}
                          />
                        ) : (
                          <span className="form-helper-copy">-</span>
                        )}
                      </td>
                      <td>
                        <div className="source-primary-cell">
                          <strong>{source.title}</strong>
                          <span className="form-helper-copy">{getSourceKindLabel(source.source_kind)}</span>
                          {source.notes ? <p>{source.notes}</p> : null}
                        </div>
                      </td>
                      <td>
                        <div className="source-secondary-cell">
                          <span>{getSourceTransportLabel(source.transport)}</span>
                          <span className="form-helper-copy">{buildSourceTransportSummary(source)}</span>
                        </div>
                      </td>
                      <td>
                        <div className="source-secondary-cell">
                          <StatusChip status={source.status} />
                          {warningCount > 0 ? <span className="quiet-pill">Warnings: {warningCount}</span> : null}
                        </div>
                      </td>
                      <td>
                        <div className="source-secondary-cell">
                          {source.parse_error ? <span className="source-error-copy">{source.parse_error}</span> : null}
                          {!source.parse_error ? (
                            <span className="form-helper-copy">
                              {source.language_code ? `Language hint: ${source.language_code}` : 'No language hint set'}
                            </span>
                          ) : null}
                        </div>
                      </td>
                      <td>
                        <div className="source-secondary-cell">
                          <span>{formatDateTime(source.updated_at)}</span>
                          <span className="form-helper-copy">Created {formatDateTime(source.created_at)}</span>
                        </div>
                      </td>
                      <td>
                        <div className="source-action-group">
                          {canEditSource(source) ? (
                            <button className="secondary-button source-action-button" type="button" onClick={() => void openSourceEditor(source)} disabled={isPageBusy}>
                              Edit
                            </button>
                          ) : (
                            <span className="quiet-pill">Archived</span>
                          )}
                          {canOpenSource(source) ? (
                            <button
                              className="secondary-button source-action-button"
                              type="button"
                              onClick={() => void handleOpenSource(source)}
                              disabled={isPageBusy || rowIsBusy}
                            >
                              {rowActionKey === `open:${source.uuid}` ? 'Opening...' : getSourceOpenLabel(source)}
                            </button>
                          ) : null}
                          {canRunSourceParse(source) ? (
                            <button
                              className="secondary-button source-action-button"
                              type="button"
                              onClick={() => void handleParseSingleSource(source)}
                              disabled={isPageBusy || rowIsBusy}
                            >
                              {rowActionKey === `parse:${source.uuid}`
                                ? 'Parsing...'
                                : source.status === 'failed'
                                  ? 'Retry parse'
                                  : 'Parse'}
                            </button>
                          ) : null}
                          {canRunSourceReparse(source) ? (
                            <button
                              className="secondary-button source-action-button"
                              type="button"
                              onClick={() => void handleReparseSingleSource(source)}
                              disabled={isPageBusy || rowIsBusy}
                            >
                              {rowActionKey === `reparse:${source.uuid}` ? 'Reparsing...' : 'Reparse'}
                            </button>
                          ) : null}
                          {canArchiveSource(source) ? (
                            <button
                              className="secondary-button source-action-button is-danger"
                              type="button"
                              onClick={() => void handleArchiveSource(source)}
                              disabled={isPageBusy || rowIsBusy || !canArchiveSource(source)}
                            >
                              {rowActionKey === `archive:${source.uuid}` ? 'Archiving...' : 'Archive'}
                            </button>
                          ) : null}
                        </div>
                      </td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          </div>
        )}
      </section>

      <SlideOverPanel
        title={editingSource ? `Edit ${editingSource.title}` : 'Source editor'}
        open={editingSource !== null && editorDraft !== null}
        onClose={() => void closeSourceEditor()}
      >
        {editingSource && editorDraft ? (
          <>
            <div className="status-row">
              <StatusChip status={editingSource.status} />
              <span className="form-helper-copy">{getSourceKindLabel(editingSource.source_kind)}</span>
            </div>

            {editorTriggersReparse ? (
              <div className="inline-banner inline-banner-info">
                <strong>Saving will reset parse status.</strong>
                <span>Source kind, language, URL, or inline text changes require re-parsing.</span>
              </div>
            ) : null}

            <form className="profile-page-form" onSubmit={handleSaveEditor}>
              <div style={{ display: 'grid', gap: '14px' }}>
                <label className="field-label">
                  <span>Source kind</span>
                  <select
                    className="select-input"
                    value={editorDraft.source_kind}
                    disabled={isPageBusy}
                    onChange={(event) =>
                      setEditorDraft((currentDraft) =>
                        currentDraft ? { ...currentDraft, source_kind: event.target.value } : currentDraft,
                      )
                    }
                  >
                    {SOURCE_KIND_OPTIONS.map((option) => (
                      <option key={option.value} value={option.value}>{option.label}</option>
                    ))}
                  </select>
                </label>

                <label className="field-label">
                  <span>Source title</span>
                  <input
                    className="text-input"
                    value={editorDraft.title}
                    disabled={isPageBusy}
                    onChange={(event) =>
                      setEditorDraft((currentDraft) =>
                        currentDraft ? { ...currentDraft, title: event.target.value } : currentDraft,
                      )
                    }
                  />
                </label>

                <label className="field-label">
                  <span>Language hint</span>
                  <input
                    className="text-input"
                    value={editorDraft.language_code}
                    placeholder="en, ru, de..."
                    disabled={isPageBusy}
                    onChange={(event) =>
                      setEditorDraft((currentDraft) =>
                        currentDraft ? { ...currentDraft, language_code: event.target.value } : currentDraft,
                      )
                    }
                  />
                </label>

                <div className="field-label">
                  <span>Transport</span>
                  <div className="inline-banner inline-banner-info">
                    <strong>{getSourceTransportLabel(editingSource.transport)}</strong>
                    <span>{buildSourceTransportSummary(editingSource)}</span>
                  </div>
                </div>

                {editingSource.transport === 'external_url' ? (
                  <label className="field-label">
                    <span>External URL</span>
                    <input
                      className="text-input"
                      value={editorDraft.external_url}
                      disabled={isPageBusy}
                      onChange={(event) =>
                        setEditorDraft((currentDraft) =>
                          currentDraft ? { ...currentDraft, external_url: event.target.value } : currentDraft,
                        )
                      }
                    />
                  </label>
                ) : null}

                {editingSource.transport === 'inline_text' ? (
                  <label className="field-label">
                    <span>Inline text</span>
                    <textarea
                      className="textarea-input textarea-input-tall"
                      value={editorDraft.inline_text}
                      disabled={isPageBusy}
                      onChange={(event) =>
                        setEditorDraft((currentDraft) =>
                          currentDraft ? { ...currentDraft, inline_text: event.target.value } : currentDraft,
                        )
                      }
                    />
                  </label>
                ) : null}

                <label className="field-label">
                  <span>Operator notes</span>
                  <textarea
                    className="textarea-input textarea-input-compact"
                    value={editorDraft.notes}
                    disabled={isPageBusy}
                    onChange={(event) =>
                      setEditorDraft((currentDraft) =>
                        currentDraft ? { ...currentDraft, notes: event.target.value } : currentDraft,
                      )
                    }
                  />
                </label>
              </div>

              <div className="form-actions">
                <button className="primary-button" type="submit" disabled={isPageBusy || !hasDirtyEditor}>
                  {isSavingEditor ? 'Saving...' : 'Save source'}
                </button>
                <button
                  className="secondary-button"
                  type="button"
                  disabled={isPageBusy || !hasDirtyEditor}
                  onClick={() => setEditorDraft(buildSourceEditorDraft(editingSource))}
                >
                  Reset
                </button>
              </div>
            </form>
          </>
        ) : null}
      </SlideOverPanel>

      {loadError && readiness !== null ? (
        <ErrorState
          compact
          title="Latest reload failed"
          description={`${loadError} Showing the most recent successful source payload.`}
          onRetry={() => reloadPage()}
        />
      ) : null}

      {parseStage ? (
        <section className="board-panel">
          <div className="panel-heading">
            <span className="section-tag">Next stage</span>
            <h3>{parseStage.label}</h3>
            <p>{parseStage.recommended_action || 'Continue into parsed-source review and org-context inspection after parsing.'}</p>
          </div>
          <div className="hero-actions">
            <AppLink className="primary-button link-button" to={buildScopedWorkspacePath('parse')}>
              Continue to Parse
            </AppLink>
          </div>
        </section>
      ) : null}
    </div>
  )
}
