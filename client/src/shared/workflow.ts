export type WorkflowStatusValue =
  | 'not_started'
  | 'blocked'
  | 'ready'
  | 'running'
  | 'action_required'
  | 'completed'
  | 'failed'

export type WorkspacePageKey =
  | 'overview'
  | 'profile'
  | 'contexts'
  | 'sources'
  | 'parse'
  | 'blueprint'
  | 'clarifications'
  | 'assessments'
  | 'matrix'
  | 'plans'

export type WorkspaceNavItem = {
  key: WorkspacePageKey
  label: string
  segment: string
  description: string
  nextStageLabel: string
  plannedComponents: string[]
}

export const WORKFLOW_STATUS_LABELS: Record<WorkflowStatusValue, string> = {
  not_started: 'Not started',
  blocked: 'Blocked',
  ready: 'Ready',
  running: 'Running',
  action_required: 'Action required',
  completed: 'Completed',
  failed: 'Failed',
}

export const WORKSPACE_NAV_ITEMS: WorkspaceNavItem[] = [
  {
    key: 'overview',
    label: 'Overview',
    segment: '',
    description: 'Workspace summary, blockers, next actions, and stage status live here.',
    nextStageLabel: 'Stage 02',
    plannedComponents: [
      'workspace header',
      'workflow-stage strip',
      'blockers card',
      'next action card',
      'latest-run badges',
    ],
  },
  {
    key: 'profile',
    label: 'Profile',
    segment: 'profile',
    description: 'Company profile, pilot scope, checklist, and operator notes live here.',
    nextStageLabel: 'Stage 03',
    plannedComponents: [
      'company profile form card',
      'pilot scope form card',
      'checklist card',
      'notes card',
      'save bar',
    ],
  },
  {
    key: 'contexts',
    label: 'Contexts',
    segment: 'contexts',
    description: 'Planning scopes, profile overrides, and context-specific source linkage live here.',
    nextStageLabel: 'Scope',
    plannedComponents: [
      'scope summary card',
      'context list',
      'create context form',
      'context detail editor',
      'source override manager',
    ],
  },
  {
    key: 'sources',
    label: 'Sources',
    segment: 'sources',
    description: 'Workspace-bound uploads, source attachment, parse actions, and source editing live here.',
    nextStageLabel: 'Stage 04',
    plannedComponents: [
      'upload panel',
      'source attach form',
      'source table',
      'source row actions',
      'parse all bar',
    ],
  },
  {
    key: 'parse',
    label: 'Parse',
    segment: 'parse',
    description: 'Parsed-source review, org-context inspection, and CV evidence triage live here.',
    nextStageLabel: 'Stage 05',
    plannedComponents: [
      'parsed-source table',
      'parsed-source detail panel',
      'CSV preview panel',
      'org-context summary cards',
      'employee list',
      'CV review queues',
    ],
  },
  {
    key: 'blueprint',
    label: 'Blueprint',
    segment: 'blueprint',
    description: 'Blueprint generation, run review, role inspection, and publication controls live here.',
    nextStageLabel: 'Stage 06',
    plannedComponents: [
      'generate/run controls',
      'blueprint summary tabs',
      'roadmap context section',
      'role candidate list',
      'skill list and gaps section',
      'review/publish action bar',
    ],
  },
  {
    key: 'clarifications',
    label: 'Clarifications',
    segment: 'clarifications',
    description: 'Clarification queue review, batch answers, and clarification history live here.',
    nextStageLabel: 'Stage 06',
    plannedComponents: [
      'open questions list',
      'answer form area',
      'history section',
      'refresh/revision action bar',
    ],
  },
  {
    key: 'assessments',
    label: 'Assessments',
    segment: 'assessments',
    description: 'Assessment cycle generation, pack tracking, and public-link sharing live here now.',
    nextStageLabel: 'Stage 07',
    plannedComponents: [
      'generate cycle controls',
      'cycle status summary',
      'pack table',
      'link copy actions',
      'completion counters',
    ],
  },
  {
    key: 'matrix',
    label: 'Matrix',
    segment: 'matrix',
    description: 'Evidence matrix review, role-match inspection, and employee gap slices live here now.',
    nextStageLabel: 'Stage 08',
    plannedComponents: [
      'evidence status cards',
      'role-match review panel',
      'matrix build action bar',
      'heatmap section',
      'risks section',
      'employee detail drawer',
    ],
  },
  {
    key: 'plans',
    label: 'Plans',
    segment: 'plans',
    description: 'Plan generation, readable team and PDP review, and downloadable outputs live here now.',
    nextStageLabel: 'Stage 09',
    plannedComponents: [
      'generate plans action bar',
      'team summary card',
      'team actions section',
      'individual plan list',
      'artifact download list',
    ],
  },
]

const WORKSPACE_NAV_ITEM_BY_KEY = new Map(WORKSPACE_NAV_ITEMS.map((item) => [item.key, item]))
const WORKSPACE_PAGE_KEY_BY_SEGMENT = new Map(
  WORKSPACE_NAV_ITEMS.filter((item) => item.segment).map((item) => [item.segment, item.key]),
)

export function getWorkspaceNavItem(pageKey: WorkspacePageKey): WorkspaceNavItem {
  return WORKSPACE_NAV_ITEM_BY_KEY.get(pageKey) ?? WORKSPACE_NAV_ITEMS[0]
}

export function getWorkspacePageKeyBySegment(segment: string): WorkspacePageKey | null {
  return WORKSPACE_PAGE_KEY_BY_SEGMENT.get(segment) ?? null
}

export type WorkspacePathOptions = {
  contextSlug?: string | null
}

export function readWorkspaceContextSlug(search: string) {
  const params = new URLSearchParams(search)
  const value = (params.get('context') || '').trim()
  return value || null
}

export function buildWorkspacePath(
  workspaceSlug: string,
  pageKey: WorkspacePageKey = 'overview',
  options: WorkspacePathOptions = {},
) {
  const item = getWorkspaceNavItem(pageKey)
  const pathname = item.segment
    ? `/workspaces/${encodeURIComponent(workspaceSlug)}/${item.segment}`
    : `/workspaces/${encodeURIComponent(workspaceSlug)}`
  const contextSlug = (options.contextSlug || '').trim()
  if (!contextSlug) {
    return pathname
  }
  const params = new URLSearchParams()
  params.set('context', contextSlug)
  return `${pathname}?${params.toString()}`
}

export function buildAssessmentPath(packUuid: string) {
  return `/assessment/${encodeURIComponent(packUuid)}`
}

export function buildWorkspaceSlugCandidate(companyName: string) {
  const normalized = companyName
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, '-')
    .replace(/^-+|-+$/g, '')

  return normalized || 'company'
}

export function getWorkspacePageKeyForWorkflowStage(stageKey: string): WorkspacePageKey {
  switch (stageKey) {
    case 'context':
      return 'profile'
    case 'roadmap_analysis':
      return 'blueprint'
    case 'sources':
      return 'sources'
    case 'parse':
    case 'evidence':
      return 'parse'
    case 'blueprint':
      return 'blueprint'
    case 'clarifications':
      return 'clarifications'
    case 'assessments':
      return 'assessments'
    case 'matrix':
      return 'matrix'
    case 'plans':
      return 'plans'
    default:
      return 'overview'
  }
}

export function getWorkflowStatusLabel(status: string) {
  return WORKFLOW_STATUS_LABELS[status as WorkflowStatusValue] ?? humanizeToken(status)
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
