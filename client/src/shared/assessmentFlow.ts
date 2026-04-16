import {
  buildAssessmentPath,
  humanizeToken,
} from './workflow'
import type {
  PrototypeAssessmentAspirationAnswer,
  PrototypeAssessmentHiddenSkillAnswer,
  PrototypeAssessmentPack,
  PrototypeAssessmentPackSubmitRequest,
  PrototypeAssessmentQuestionnairePayload,
  PrototypeAssessmentTargetedAnswer,
  PrototypeAssessmentTargetedQuestion,
} from './prototypeApi'

export type AssessmentTargetedDraft = PrototypeAssessmentTargetedQuestion & {
  self_rated_level: string
  answer_confidence: string
  example_text: string
  notes: string
}

export type AssessmentHiddenSkillDraft = {
  id: string
  skill_name_en: string
  skill_name_ru: string
  self_rated_level: string
  answer_confidence: string
  example_text: string
}

export type AssessmentAspirationDraft = {
  target_role_family: string
  notes: string
  interest_signal: string
}

export type AssessmentFormDraft = {
  schema_version: string
  targeted_answers: AssessmentTargetedDraft[]
  hidden_skills: AssessmentHiddenSkillDraft[]
  aspiration: AssessmentAspirationDraft
  confidence_statement: string
}

function readString(value: unknown) {
  return typeof value === 'string' ? value : ''
}

function clampNumber(value: unknown, minimum: number, maximum: number, fallback: number) {
  if (typeof value !== 'number' || Number.isNaN(value)) {
    return fallback
  }

  return Math.min(maximum, Math.max(minimum, value))
}

function formatLevelValue(value: unknown, fallback = 0) {
  return String(clampNumber(value, 0, 5, fallback))
}

function formatConfidenceValue(value: unknown, fallback = 0.6) {
  return clampNumber(value, 0, 1, fallback).toFixed(2)
}

function buildHiddenSkillDraftId() {
  if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') {
    return crypto.randomUUID()
  }

  return `hidden-skill-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`
}

export function createEmptyHiddenSkillDraft(): AssessmentHiddenSkillDraft {
  return {
    id: buildHiddenSkillDraftId(),
    skill_name_en: '',
    skill_name_ru: '',
    self_rated_level: '3',
    answer_confidence: '0.60',
    example_text: '',
  }
}

export function isAssessmentPackLocked(status: string) {
  return ['submitted', 'completed', 'superseded'].includes(status)
}

export function isAssessmentPackEditable(status: string) {
  return !isAssessmentPackLocked(status)
}

export function isAssessmentCycleActive(status: string) {
  return status === 'generated' || status === 'running'
}

export function buildAssessmentQuestionPrompt(question: PrototypeAssessmentTargetedQuestion) {
  const prompt = readString(question.prompt).trim()
  if (prompt) {
    return prompt
  }

  const skillName = readString(question.skill_name_en).trim() || readString(question.skill_key).trim() || 'this skill'
  return `How would you describe your current strength in ${skillName}?`
}

export function buildPromptHeading(
  promptBlock: PrototypeAssessmentQuestionnairePayload['hidden_skills_prompt'] | PrototypeAssessmentQuestionnairePayload['aspiration_prompt'],
  fallback: string,
) {
  return readString(promptBlock?.prompt_title).trim() || fallback
}

export function buildPromptCopy(
  promptBlock: PrototypeAssessmentQuestionnairePayload['hidden_skills_prompt'] | PrototypeAssessmentQuestionnairePayload['aspiration_prompt'],
  fallback: string,
) {
  return readString(promptBlock?.prompt).trim() || fallback
}

export function buildAssessmentFormDraft(pack: PrototypeAssessmentPack): AssessmentFormDraft {
  const questionnaire = pack.questionnaire_payload || {}
  const responsePayload = pack.response_payload || {}
  const targetedAnswerById = new Map<string, PrototypeAssessmentTargetedAnswer>()

  for (const answer of responsePayload.targeted_answers || []) {
    if (answer.question_id) {
      targetedAnswerById.set(answer.question_id, answer)
    }
  }

  return {
    schema_version:
      readString(responsePayload.schema_version).trim() ||
      readString(questionnaire.schema_version).trim() ||
      readString(pack.questionnaire_version).trim() ||
      'stage7-v1',
    targeted_answers: (questionnaire.targeted_questions || []).map((question) => {
      const saved = targetedAnswerById.get(question.question_id)

      return {
        ...question,
        self_rated_level: formatLevelValue(saved?.self_rated_level, 0),
        answer_confidence: formatConfidenceValue(saved?.answer_confidence, 0.6),
        example_text: readString(saved?.example_text),
        notes: readString(saved?.notes),
      }
    }),
    hidden_skills: (responsePayload.hidden_skills || []).map((item) => ({
      id: buildHiddenSkillDraftId(),
      skill_name_en: readString(item.skill_name_en),
      skill_name_ru: readString(item.skill_name_ru),
      self_rated_level: formatLevelValue(item.self_rated_level, 3),
      answer_confidence: formatConfidenceValue(item.answer_confidence, 0.6),
      example_text: readString(item.example_text),
    })),
    aspiration: {
      target_role_family: readString(responsePayload.aspiration?.target_role_family),
      notes: readString(responsePayload.aspiration?.notes),
      interest_signal: readString(responsePayload.aspiration?.interest_signal),
    },
    confidence_statement: readString(responsePayload.confidence_statement),
  }
}

export function buildAssessmentSubmitRequest(
  draft: AssessmentFormDraft,
  finalSubmit: boolean,
): PrototypeAssessmentPackSubmitRequest {
  return {
    final_submit: finalSubmit,
    targeted_answers: draft.targeted_answers.map((question) => ({
      question_id: readString(question.question_id).trim(),
      skill_key: readString(question.skill_key).trim(),
      self_rated_level: clampNumber(Number(question.self_rated_level), 0, 5, 0),
      answer_confidence: clampNumber(Number(question.answer_confidence), 0, 1, 0.6),
      example_text: readString(question.example_text).trim(),
      notes: readString(question.notes).trim(),
    })),
    hidden_skills: draft.hidden_skills
      .filter((item) => readString(item.skill_name_en).trim().length > 0)
      .map((item): PrototypeAssessmentHiddenSkillAnswer => ({
        skill_name_en: readString(item.skill_name_en).trim(),
        skill_name_ru: readString(item.skill_name_ru).trim(),
        self_rated_level: clampNumber(Number(item.self_rated_level), 0, 5, 3),
        answer_confidence: clampNumber(Number(item.answer_confidence), 0, 1, 0.6),
        example_text: readString(item.example_text).trim(),
      })),
    aspiration: {
      target_role_family: readString(draft.aspiration.target_role_family).trim(),
      notes: readString(draft.aspiration.notes).trim(),
      interest_signal: readString(draft.aspiration.interest_signal).trim(),
    } satisfies PrototypeAssessmentAspirationAnswer,
    confidence_statement: readString(draft.confidence_statement).trim(),
  }
}

export function buildAssessmentDraftFingerprint(draft: AssessmentFormDraft) {
  return JSON.stringify(buildAssessmentSubmitRequest(draft, false))
}

export function isTargetedDraftMeaningfullyAnswered(question: AssessmentTargetedDraft) {
  return (
    readString(question.example_text).trim().length > 0 ||
    readString(question.notes).trim().length > 0 ||
    readString(question.self_rated_level).trim() !== '0' ||
    readString(question.answer_confidence).trim() !== '0.60'
  )
}

export function listIncompleteTargetedQuestions(draft: AssessmentFormDraft) {
  return draft.targeted_answers
    .filter((question) => !isTargetedDraftMeaningfullyAnswered(question))
    .map((question) => readString(question.skill_name_en).trim() || readString(question.skill_key).trim() || question.question_id)
}

export function buildPublicAssessmentUrl(packUuid: string) {
  const path = buildAssessmentPath(packUuid)
  return `${window.location.origin}${path}`
}

export async function copyTextToClipboard(text: string) {
  if (navigator.clipboard && window.isSecureContext) {
    await navigator.clipboard.writeText(text)
    return
  }

  const input = document.createElement('textarea')
  input.value = text
  input.setAttribute('readonly', 'true')
  input.style.position = 'absolute'
  input.style.opacity = '0'
  input.style.pointerEvents = 'none'
  document.body.append(input)
  input.select()
  const copied = typeof document.execCommand === 'function' && document.execCommand('copy')
  input.remove()

  if (!copied) {
    throw new Error('Clipboard copy is not available in this browser context.')
  }
}

export function getAssessmentPackStateLabel(status: string) {
  if (status === 'generated') {
    return 'Ready to start'
  }

  if (status === 'opened') {
    return 'In progress'
  }

  if (status === 'submitted' || status === 'completed') {
    return 'Submitted'
  }

  if (status === 'superseded') {
    return 'Superseded'
  }

  return humanizeToken(status || 'unknown')
}
