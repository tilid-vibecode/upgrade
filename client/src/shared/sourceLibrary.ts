import type {
  PrototypeMediaFile,
  PrototypeMediaFileCategory,
  PrototypeWorkspaceSource,
  PrototypeWorkspaceSourceTransport,
} from './prototypeApi'
import { humanizeToken } from './workflow'

export type SourceKindOption = {
  value: string
  label: string
  description: string
}

export const SOURCE_KIND_OPTIONS: SourceKindOption[] = [
  {
    value: 'strategy',
    label: 'Strategy',
    description: 'Company strategy, planning notes, or executive direction documents.',
  },
  {
    value: 'roadmap',
    label: 'Roadmap',
    description: 'Product or delivery roadmap files that describe what is changing next.',
  },
  {
    value: 'job_description',
    label: 'Job description',
    description: 'Role descriptions, hiring scorecards, or responsibility outlines.',
  },
  {
    value: 'existing_matrix',
    label: 'Existing matrix',
    description: 'Any prior skills matrix, rubric, or capability mapping artifact.',
  },
  {
    value: 'org_csv',
    label: 'Organization spreadsheet',
    description: 'Employee roster or org structure spreadsheet used to seed org context.',
  },
  {
    value: 'employee_cv',
    label: 'Employee CV',
    description: 'Resume or CV files that support employee skill evidence.',
  },
  {
    value: 'other',
    label: 'Other',
    description: 'Supporting material that does not fit the core source buckets above.',
  },
]

export const SOURCE_KIND_FILE_CATEGORY_HINTS: Record<string, string[]> = {
  strategy: ['document', 'word', 'text', 'spreadsheet'],
  roadmap: ['document', 'word', 'text', 'spreadsheet'],
  job_description: ['document', 'word', 'text'],
  existing_matrix: ['spreadsheet', 'document', 'word', 'text'],
  org_csv: ['spreadsheet'],
  employee_cv: ['document', 'word', 'text'],
  other: ['image', 'document', 'word', 'text', 'spreadsheet'],
}

export const TRANSPORT_LABELS: Record<PrototypeWorkspaceSourceTransport, string> = {
  media_file: 'Uploaded file',
  external_url: 'External URL',
  inline_text: 'Inline text',
}

export function getSourceKindLabel(sourceKind: string) {
  return SOURCE_KIND_OPTIONS.find((item) => item.value === sourceKind)?.label ?? humanizeToken(sourceKind)
}

export function getSourceKindDescription(sourceKind: string) {
  return SOURCE_KIND_OPTIONS.find((item) => item.value === sourceKind)?.description ?? 'Supporting workspace source.'
}

export function getSourceTransportLabel(transport: PrototypeWorkspaceSourceTransport) {
  return TRANSPORT_LABELS[transport] ?? humanizeToken(transport)
}

export function getAcceptedFileCategoryHint(sourceKind: string) {
  const categories = SOURCE_KIND_FILE_CATEGORY_HINTS[sourceKind] ?? []
  return categories.length > 0 ? categories.join(', ') : 'any supported file type'
}

export function isMediaCompatibleWithSourceKind(mediaFile: PrototypeMediaFile, sourceKind: string) {
  const categories = SOURCE_KIND_FILE_CATEGORY_HINTS[sourceKind]
  if (!categories || categories.length === 0) {
    return true
  }

  return categories.includes(mediaFile.file_category)
}

export function isFileCategoryCompatibleWithSourceKind(
  sourceKind: string,
  fileCategory: PrototypeMediaFileCategory,
) {
  const categories = SOURCE_KIND_FILE_CATEGORY_HINTS[sourceKind]
  return Array.isArray(categories) ? categories.includes(fileCategory) : true
}

export function normalizeSourceTitleFromFilename(filename: string) {
  const trimmed = filename.trim()
  if (!trimmed) {
    return ''
  }

  return trimmed.replace(/\.[^.]+$/, '')
}

export function buildAttachDraftFromMedia(mediaFile: PrototypeMediaFile) {
  const suggestedKind = mediaFile.file_category === 'spreadsheet' ? 'org_csv' : 'other'

  return {
    source_kind: suggestedKind,
    title: normalizeSourceTitleFromFilename(mediaFile.original_filename),
    notes: '',
    language_code: '',
  }
}

export function buildSourceEditorDraft(source: PrototypeWorkspaceSource) {
  return {
    source_kind: source.source_kind,
    title: source.title,
    notes: source.notes,
    language_code: source.language_code,
    external_url: source.external_url,
    inline_text: source.inline_text,
  }
}

export function areSourceEditorDraftsEqual(
  left: ReturnType<typeof buildSourceEditorDraft>,
  right: ReturnType<typeof buildSourceEditorDraft>,
) {
  return (
    left.source_kind === right.source_kind &&
    left.title === right.title &&
    left.notes === right.notes &&
    left.language_code === right.language_code &&
    left.external_url === right.external_url &&
    left.inline_text === right.inline_text
  )
}

export function canSelectSourceForParse(source: PrototypeWorkspaceSource) {
  return source.status === 'attached' || source.status === 'failed'
}

export function canRunSourceParse(source: PrototypeWorkspaceSource) {
  return canSelectSourceForParse(source)
}

export function canRunSourceReparse(source: PrototypeWorkspaceSource) {
  return source.status === 'parsed'
}

export function canArchiveSource(source: PrototypeWorkspaceSource) {
  return source.status !== 'archived'
}

export function canEditSource(source: PrototypeWorkspaceSource) {
  return source.status !== 'archived'
}

export function canOpenSource(source: PrototypeWorkspaceSource) {
  return source.status !== 'archived' && (
    (source.transport === 'media_file' && Boolean(source.media_file_uuid)) ||
    (source.transport === 'external_url' && Boolean(source.external_url))
  )
}

export function getSourceOpenLabel(source: PrototypeWorkspaceSource) {
  if (source.transport === 'external_url') {
    return 'Open URL'
  }

  if (source.transport === 'media_file') {
    return 'Open file'
  }

  return 'Open'
}

export function buildSourceTransportSummary(source: PrototypeWorkspaceSource) {
  if (source.transport === 'media_file') {
    return source.media_filename || 'Uploaded file'
  }

  if (source.transport === 'external_url') {
    return source.external_url
  }

  return source.inline_text ? `${source.inline_text.length} characters of inline text` : 'Inline text'
}

export function getSourceWarningCount(source: PrototypeWorkspaceSource) {
  const warnings = source.parse_metadata.warnings
  return Array.isArray(warnings) ? warnings.length : 0
}
