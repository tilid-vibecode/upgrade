import type { PrototypeClarificationQuestion, PrototypeSkillBlueprintRun } from './prototypeApi'

export type BlueprintDetailTabKey = 'overview' | 'roadmap' | 'roles' | 'skills' | 'assessment' | 'changes'

export const BLUEPRINT_DETAIL_TABS: Array<{ key: BlueprintDetailTabKey; label: string }> = [
  { key: 'overview', label: 'Overview' },
  { key: 'roadmap', label: 'Roadmap' },
  { key: 'roles', label: 'Roles' },
  { key: 'skills', label: 'Skills and gaps' },
  { key: 'assessment', label: 'Assessment plan' },
  { key: 'changes', label: 'Change log' },
]

function readString(value: unknown) {
  return typeof value === 'string' ? value : ''
}

function readBoolean(value: unknown) {
  return typeof value === 'boolean' ? value : false
}

function slugifyToken(value: string) {
  return value
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, '-')
    .replace(/^-+|-+$/g, '')
}

export function getClarificationId(question: PrototypeClarificationQuestion | Record<string, unknown>) {
  return readString('question_key' in question ? question.question_key : question.id) || readString(question.uuid)
}

export function getClarificationText(question: PrototypeClarificationQuestion | Record<string, unknown>) {
  return readString('question_text' in question ? question.question_text : question.question) || 'Untitled clarification'
}

export function getClarificationOpenCount(run: PrototypeSkillBlueprintRun | null) {
  if (run === null) {
    return 0
  }

  const summary = run.review_summary?.clarification_summary
  if (summary && typeof summary === 'object' && summary !== null) {
    const open = (summary as Record<string, unknown>).open
    if (typeof open === 'number' && Number.isFinite(open)) {
      return open
    }
  }

  return run.clarification_questions.filter((item) => readString(item.status) === 'open').length
}

export function isBlueprintPublished(run: PrototypeSkillBlueprintRun) {
  return run.is_published
}

export function isBlueprintMutable(run: PrototypeSkillBlueprintRun) {
  return !isBlueprintPublished(run) && run.status !== 'approved'
}

export function canReviewBlueprint(run: PrototypeSkillBlueprintRun) {
  return !isBlueprintPublished(run) && run.status !== 'approved' && run.status !== 'running' && run.status !== 'failed'
}

export function canApproveBlueprint(run: PrototypeSkillBlueprintRun) {
  return !isBlueprintPublished(run) && run.status === 'reviewed' && !run.approval_blocked
}

export function canPublishBlueprint(run: PrototypeSkillBlueprintRun) {
  return !isBlueprintPublished(run) && run.status === 'approved' && !run.approval_blocked
}

export function canRefreshBlueprintFromClarifications(run: PrototypeSkillBlueprintRun) {
  if (isBlueprintPublished(run)) {
    return false
  }

  return run.clarification_questions.some((item) => {
    const status = readString(item.status)
    return status === 'answered' || status === 'accepted' || status === 'rejected'
  })
}

export function canStartBlueprintRevision(run: PrototypeSkillBlueprintRun) {
  return run.status === 'approved' || run.is_published
}

export function getBlueprintRunBadges(run: PrototypeSkillBlueprintRun) {
  const badges: string[] = []

  if (run.default_for_workspace) {
    badges.push('Effective')
  }
  if (run.latest_for_workspace) {
    badges.push('Latest')
  }
  if (run.latest_review_ready_for_workspace) {
    badges.push('Latest review-ready')
  }
  if (run.latest_published_for_workspace) {
    badges.push('Latest published')
  }
  if (run.is_published) {
    badges.push('Published')
  }
  if (run.generation_mode) {
    badges.push(`Mode: ${run.generation_mode}`)
  }

  return badges
}

export function getBlueprintRunSummaryLabel(run: PrototypeSkillBlueprintRun) {
  if (run.default_for_workspace && run.latest_for_workspace) {
    return 'The effective downstream blueprint is also the newest run.'
  }

  if (run.default_for_workspace) {
    return 'This run is currently driving downstream stages.'
  }

  if (run.latest_for_workspace) {
    return 'This is the newest working run in the workspace.'
  }

  return 'Historical blueprint run.'
}

export function getRoleCandidateKey(roleCandidate: Record<string, unknown>) {
  const directKey = readString(roleCandidate.role_key)
  if (directKey) {
    return directKey
  }

  const family = readString(roleCandidate.canonical_role_family)
  const seniority = readString(roleCandidate.seniority)
  const roleName = readString(roleCandidate.role_name)
  return slugifyToken([family, seniority, roleName].filter(Boolean).join('-')) || 'role'
}

export function getRoleCandidateName(roleCandidate: Record<string, unknown>) {
  return readString(roleCandidate.role_name) || 'Untitled role'
}

export function getRoleCandidateSkillCount(roleCandidate: Record<string, unknown>) {
  const skills = Array.isArray(roleCandidate.skills) ? roleCandidate.skills : []
  return skills.length
}

export function getRoleCandidateInitiativeCount(roleCandidate: Record<string, unknown>) {
  const initiatives = Array.isArray(roleCandidate.related_initiatives) ? roleCandidate.related_initiatives : []
  return initiatives.length
}

export function getQuestionChangedTargetModel(question: PrototypeClarificationQuestion | Record<string, unknown>) {
  return readBoolean('changed_target_model' in question ? question.changed_target_model : question.changed_target_model)
}
