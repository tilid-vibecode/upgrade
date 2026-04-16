import { useEffect, useState } from 'react'

import { useNavigationBlocker } from '../app/navigation'
import {
  buildAssessmentDraftFingerprint,
  buildAssessmentFormDraft,
  buildAssessmentQuestionPrompt,
  buildAssessmentSubmitRequest,
  buildPromptCopy,
  buildPromptHeading,
  createEmptyHiddenSkillDraft,
  getAssessmentPackStateLabel,
  isAssessmentPackEditable,
  isAssessmentPackLocked,
  listIncompleteTargetedQuestions,
  type AssessmentFormDraft,
} from '../shared/assessmentFlow'
import { getApiErrorMessage, getApiErrorMessages, isApiError } from '../shared/api'
import { formatDateTime } from '../shared/formatters'
import {
  getPublicAssessmentPack,
  openPublicAssessmentPack,
  submitPublicAssessmentPack,
  type PublicAssessmentPackResponse,
} from '../shared/prototypeApi'
import { requestGlobalConfirmation } from '../shared/ui/ConfirmationDialog'
import ErrorState from '../shared/ui/ErrorState'
import LoadingState from '../shared/ui/LoadingState'
import StatusChip from '../shared/ui/StatusChip'

type PublicAssessmentPageProps = {
  packUuid: string
}

type BannerTone = 'info' | 'success' | 'warn' | 'error'

type BannerState = {
  tone: BannerTone
  title: string
  messages: string[]
}

const TERMINAL_OR_INVALID_PACK_STATUSES = new Set(['submitted', 'completed', 'superseded'])
const LEVEL_OPTIONS = ['0', '1', '2', '3', '4', '5']
const CONFIDENCE_OPTIONS = [
  { value: '0.20', label: '20%' },
  { value: '0.40', label: '40%' },
  { value: '0.60', label: '60%' },
  { value: '0.80', label: '80%' },
  { value: '1.00', label: '100%' },
]
const INTEREST_SIGNAL_OPTIONS = [
  { value: '', label: 'Not specified' },
  { value: 'low', label: 'Low' },
  { value: 'medium', label: 'Medium' },
  { value: 'high', label: 'High' },
]

function shouldOpenPack(pack: PublicAssessmentPackResponse) {
  return !TERMINAL_OR_INVALID_PACK_STATUSES.has(pack.status)
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

function buildSavedFingerprint(pack: PublicAssessmentPackResponse | null) {
  if (pack === null || !isAssessmentPackEditable(pack.status)) {
    return null
  }

  return buildAssessmentDraftFingerprint(buildAssessmentFormDraft(pack))
}

export default function PublicAssessmentPage({ packUuid }: PublicAssessmentPageProps) {
  const [pack, setPack] = useState<PublicAssessmentPackResponse | null>(null)
  const [draft, setDraft] = useState<AssessmentFormDraft | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [errorStatus, setErrorStatus] = useState<number | null>(null)
  const [warning, setWarning] = useState<string | null>(null)
  const [banner, setBanner] = useState<BannerState | null>(null)
  const [loading, setLoading] = useState(true)
  const [busyAction, setBusyAction] = useState<'save' | 'submit' | null>(null)
  const [reloadToken, setReloadToken] = useState(0)

  const savedFingerprint = buildSavedFingerprint(pack)
  const currentFingerprint = draft ? buildAssessmentDraftFingerprint(draft) : null
  const hasDirtyDraft =
    busyAction === null &&
    savedFingerprint !== null &&
    currentFingerprint !== null &&
    savedFingerprint !== currentFingerprint

  useNavigationBlocker(
    hasDirtyDraft,
    'You have unsaved assessment answers. Leave this page without saving your draft?',
  )

  useEffect(() => {
    let cancelled = false

    async function loadPack() {
      setLoading(true)
      setError(null)
      setErrorStatus(null)
      setWarning(null)
      setBanner(null)
      setPack(null)
      setDraft(null)

      try {
        const response = await getPublicAssessmentPack(packUuid)
        let resolvedPack = response

        if (shouldOpenPack(response) && response.opened_at === null) {
          try {
            resolvedPack = await openPublicAssessmentPack(packUuid)
          } catch (openError) {
            if (isApiError(openError) && openError.status === 400) {
              try {
                resolvedPack = await getPublicAssessmentPack(packUuid)
              } catch {
                resolvedPack = response
              }
            } else {
              resolvedPack = response
            }

            if (!cancelled && resolvedPack.status === response.status) {
              setWarning(
                openError instanceof Error
                  ? `${openError.message} The pack itself still loaded successfully.`
                  : 'The pack loaded, but first-open tracking could not be recorded.',
              )
            } else if (!cancelled && resolvedPack.status !== response.status) {
              setWarning('The pack state changed while opening this link. The latest backend state is shown below.')
            }
          }
        }

        if (!cancelled) {
          setPack(resolvedPack)
          setDraft(isAssessmentPackEditable(resolvedPack.status) ? buildAssessmentFormDraft(resolvedPack) : null)
        }
      } catch (requestError) {
        if (!cancelled) {
          setPack(null)
          setDraft(null)
          setErrorStatus(isApiError(requestError) ? requestError.status : null)
          setError(getApiErrorMessage(requestError, 'Failed to resolve assessment pack.'))
        }
      } finally {
        if (!cancelled) {
          setLoading(false)
        }
      }
    }

    void loadPack()

    return () => {
      cancelled = true
    }
  }, [packUuid, reloadToken])

  const refresh = () => setReloadToken((value) => value + 1)

  function updateTargetedAnswer(index: number, field: 'self_rated_level' | 'answer_confidence' | 'example_text' | 'notes', value: string) {
    setDraft((currentValue) => {
      if (currentValue === null) {
        return currentValue
      }

      return {
        ...currentValue,
        targeted_answers: currentValue.targeted_answers.map((item, itemIndex) =>
          itemIndex === index
            ? {
                ...item,
                [field]: value,
              }
            : item,
        ),
      }
    })
  }

  function updateHiddenSkill(id: string, field: 'skill_name_en' | 'skill_name_ru' | 'self_rated_level' | 'answer_confidence' | 'example_text', value: string) {
    setDraft((currentValue) => {
      if (currentValue === null) {
        return currentValue
      }

      return {
        ...currentValue,
        hidden_skills: currentValue.hidden_skills.map((item) =>
          item.id === id
            ? {
                ...item,
                [field]: value,
              }
            : item,
        ),
      }
    })
  }

  function removeHiddenSkill(id: string) {
    setDraft((currentValue) => {
      if (currentValue === null) {
        return currentValue
      }

      return {
        ...currentValue,
        hidden_skills: currentValue.hidden_skills.filter((item) => item.id !== id),
      }
    })
  }

  function addHiddenSkill() {
    setDraft((currentValue) => {
      if (currentValue === null) {
        return currentValue
      }

      return {
        ...currentValue,
        hidden_skills: [...currentValue.hidden_skills, createEmptyHiddenSkillDraft()],
      }
    })
  }

  function updateAspiration(field: 'target_role_family' | 'notes' | 'interest_signal', value: string) {
    setDraft((currentValue) => {
      if (currentValue === null) {
        return currentValue
      }

      return {
        ...currentValue,
        aspiration: {
          ...currentValue.aspiration,
          [field]: value,
        },
      }
    })
  }

  async function resolveLatestPackState() {
    try {
      return await getPublicAssessmentPack(packUuid)
    } catch {
      return null
    }
  }

  async function handleSaveDraft() {
    if (draft === null) {
      return
    }

    setBusyAction('save')
    setBanner(null)

    try {
      const updatedPack = await submitPublicAssessmentPack(
        packUuid,
        buildAssessmentSubmitRequest(draft, false),
      )

      setPack(updatedPack)
      setDraft(isAssessmentPackEditable(updatedPack.status) ? buildAssessmentFormDraft(updatedPack) : null)
      setBanner({
        tone: 'success',
        title: 'Draft saved.',
        messages: ['You can reopen this link later and continue from the saved draft.'],
      })
    } catch (requestError) {
      const latestPack =
        isApiError(requestError) && requestError.status === 400
          ? await resolveLatestPackState()
          : null

      if (latestPack && latestPack.status !== pack?.status) {
        setPack(latestPack)
        setDraft(isAssessmentPackEditable(latestPack.status) ? buildAssessmentFormDraft(latestPack) : null)
        setBanner({
          tone: 'warn',
          title: 'The pack state changed while you were saving.',
          messages: [`The latest backend state is now ${getAssessmentPackStateLabel(latestPack.status).toLowerCase()}.`],
        })
      } else {
      setBanner({
        tone: 'error',
        title: 'Draft save failed.',
        messages: getApiErrorMessages(requestError),
      })
      }
    } finally {
      setBusyAction(null)
    }
  }

  async function handleSubmitFinal() {
    if (draft === null) {
      return
    }

    const incompleteQuestions = listIncompleteTargetedQuestions(draft)
    if (incompleteQuestions.length > 0) {
      setBanner({
        tone: 'error',
        title: 'Finish each targeted question before final submit.',
        messages: incompleteQuestions.slice(0, 5).map((item) => `${item} still looks unanswered.`),
      })
      return
    }

    const confirmed = await requestGlobalConfirmation({
      title: 'Submit final answers?',
      description: 'After final submission this assessment pack becomes locked and can no longer be edited.',
      confirmLabel: 'Submit final answers',
      cancelLabel: 'Keep editing',
      tone: 'danger',
    })

    if (!confirmed) {
      return
    }

    setBusyAction('submit')
    setBanner(null)

    try {
      const updatedPack = await submitPublicAssessmentPack(
        packUuid,
        buildAssessmentSubmitRequest(draft, true),
      )

      setPack(updatedPack)
      setDraft(null)
      setBanner({
        tone: 'success',
        title: 'Assessment submitted.',
        messages: ['Your answers were recorded successfully. This link is now locked.'],
      })
    } catch (requestError) {
      const latestPack =
        isApiError(requestError) && requestError.status === 400
          ? await resolveLatestPackState()
          : null

      if (latestPack && latestPack.status !== pack?.status) {
        setPack(latestPack)
        setDraft(isAssessmentPackEditable(latestPack.status) ? buildAssessmentFormDraft(latestPack) : null)
        setBanner({
          tone: 'warn',
          title: 'The pack state changed while you were submitting.',
          messages: [`The latest backend state is now ${getAssessmentPackStateLabel(latestPack.status).toLowerCase()}.`],
        })
      } else {
      setBanner({
        tone: 'error',
        title: 'Final submission failed.',
        messages: getApiErrorMessages(requestError),
      })
      }
    } finally {
      setBusyAction(null)
    }
  }

  if (loading && pack === null) {
    return (
      <div className="public-page-shell">
        <LoadingState
          title="Loading assessment link"
          description="Resolving the public assessment pack and its current state."
        />
      </div>
    )
  }

  if (error || pack === null) {
    return (
      <div className="public-page-shell">
        <ErrorState
          title={errorStatus === 404 ? 'Assessment link unavailable' : 'Assessment link failed to load'}
          description={error || 'The pack could not be loaded from the backend.'}
          onRetry={refresh}
        />
      </div>
    )
  }

  const questionnaire = pack.questionnaire_payload
  const targetedQuestions = draft?.targeted_answers || []
  const hiddenSkills = draft?.hidden_skills || []
  const isLocked = isAssessmentPackLocked(pack.status)
  const introduction =
    questionnaire.introduction?.trim() ||
    'Please answer based on your recent work. Short, practical answers are enough.'
  const hiddenSkillsHeading = buildPromptHeading(questionnaire.hidden_skills_prompt, 'Hidden skills')
  const hiddenSkillsCopy = buildPromptCopy(
    questionnaire.hidden_skills_prompt,
    'Share any meaningful skills or adjacent strengths that were not obvious from your CV or current profile.',
  )
  const aspirationHeading = buildPromptHeading(questionnaire.aspiration_prompt, 'Aspirations')
  const aspirationCopy = buildPromptCopy(
    questionnaire.aspiration_prompt,
    'Share any role-family or growth directions that interest you, even if they are not part of your current title.',
  )
  const closingPrompt =
    questionnaire.closing_prompt?.trim() ||
    'Add any final context that would help interpret your answers.'
  const editableStateLabel = getAssessmentPackStateLabel(pack.status)
  const editableStateCopy =
    pack.status === 'opened'
      ? 'Your pack is already in progress. You can continue, save another draft, or submit once you are ready.'
      : 'This pack is ready to start. You can answer now, save a draft deliberately, and submit once.'

  if (pack.status === 'superseded') {
    return (
      <div className="public-page-shell">
        <section className="hero-panel compact-hero public-hero">
          <div className="hero-copy">
            <span className="section-tag">Assessment link</span>
            <h2>Superseded link</h2>
            <p>This assessment pack was replaced by a newer cycle and can no longer be used.</p>
          </div>
          <div className="hero-actions">
            <StatusChip status={pack.status} />
          </div>
        </section>

        {renderBanner(banner)}

        <section className="board-panel">
          <div className="summary-grid">
            <article className="summary-card">
              <span className="summary-label">Employee</span>
              <strong>{pack.employee_name}</strong>
              <p>Pack UUID: {pack.uuid}</p>
            </article>
            <article className="summary-card">
              <span className="summary-label">Current state</span>
              <strong>{getAssessmentPackStateLabel(pack.status)}</strong>
              <p>A newer assessment link must be shared instead of this one.</p>
            </article>
          </div>
        </section>
      </div>
    )
  }

  if (isLocked) {
    return (
      <div className="public-page-shell">
        <section className="hero-panel compact-hero public-hero">
          <div className="hero-copy">
            <span className="section-tag">Assessment submitted</span>
            <h2>Thank you, {pack.employee_name}</h2>
            <p>Your answers have already been recorded. This link is now read-only.</p>
          </div>
          <div className="hero-actions">
            <StatusChip status={pack.status} />
          </div>
        </section>

        {renderBanner(banner)}

        <section className="board-panel">
          <div className="summary-grid">
            <article className="summary-card">
              <span className="summary-label">Submitted at</span>
              <strong>{formatDateTime(pack.submitted_at)}</strong>
              <p>Final submission locks the pack and persists self-assessment evidence.</p>
            </article>
            <article className="summary-card">
              <span className="summary-label">Opened at</span>
              <strong>{formatDateTime(pack.opened_at)}</strong>
              <p>This direct link remains available as a read-only confirmation state.</p>
            </article>
          </div>
        </section>
      </div>
    )
  }

  return (
    <div className="public-page-shell public-assessment-stack">
      <section className="hero-panel compact-hero public-hero">
        <div className="hero-copy">
          <span className="section-tag">{editableStateLabel}</span>
          <h2>{pack.title || `Assessment for ${pack.employee_name}`}</h2>
          <p>{editableStateCopy}</p>
        </div>
        <div className="hero-actions">
          <StatusChip status={pack.status} />
        </div>
      </section>

      {warning ? (
        <section className="inline-banner inline-banner-warn">
          <strong>Open tracking did not complete cleanly</strong>
          <span>{warning}</span>
        </section>
      ) : null}

      {renderBanner(banner)}

      <section className="summary-grid">
        <article className="summary-card">
          <span className="summary-label">Current state</span>
          <strong>{editableStateLabel}</strong>
          <p>{introduction}</p>
        </article>
        <article className="summary-card">
          <span className="summary-label">Employee</span>
          <strong>{pack.employee_name}</strong>
          <p>Pack UUID: {pack.uuid}</p>
        </article>
        <article className="summary-card">
          <span className="summary-label">Opened at</span>
          <strong>{formatDateTime(pack.opened_at)}</strong>
          <p>Draft saves keep this assessment in progress until final submission.</p>
        </article>
        <article className="summary-card">
          <span className="summary-label">Questionnaire version</span>
          <strong>{pack.questionnaire_version || questionnaire.schema_version || 'stage7-v1'}</strong>
          <p>{targetedQuestions.length} targeted question(s) plus hidden-skills and aspiration prompts.</p>
        </article>
      </section>

      <section className="board-panel">
        <div className="panel-heading">
          <span className="section-tag">Targeted questions</span>
          <h3>Answer the skill-specific questions first</h3>
          <p>Short concrete examples are enough. Use the confidence field to reflect how sure you feel about each answer.</p>
        </div>

        {targetedQuestions.length === 0 ? (
          <ErrorState
            title="No targeted questions were found"
            description="This pack resolved, but the questionnaire payload did not include targeted questions."
            compact
          />
        ) : (
          <div className="assessment-question-list">
            {targetedQuestions.map((question, index) => (
              <article key={question.question_id} className="review-card assessment-question-card">
                <div className="review-card-head">
                  <div>
                    <span className="summary-label">Question {index + 1}</span>
                    <h4>{question.skill_name_en || question.skill_key}</h4>
                  </div>
                  <StatusChip status="ready" />
                </div>

                <p>{buildAssessmentQuestionPrompt(question)}</p>
                {question.why_asked ? <p className="form-helper-copy">Why this was asked: {question.why_asked}</p> : null}

                <div className="assessment-answer-grid">
                  <label className="field-label">
                    <span>Current level</span>
                    <select
                      className="select-input"
                      value={question.self_rated_level}
                      onChange={(event) => updateTargetedAnswer(index, 'self_rated_level', event.target.value)}
                      disabled={busyAction !== null}
                    >
                      {LEVEL_OPTIONS.map((value) => (
                        <option key={value} value={value}>
                          {value} / 5
                        </option>
                      ))}
                    </select>
                  </label>

                  <label className="field-label">
                    <span>Confidence</span>
                    <select
                      className="select-input"
                      value={question.answer_confidence}
                      onChange={(event) => updateTargetedAnswer(index, 'answer_confidence', event.target.value)}
                      disabled={busyAction !== null}
                    >
                      {CONFIDENCE_OPTIONS.map((option) => (
                        <option key={option.value} value={option.value}>
                          {option.label}
                        </option>
                      ))}
                    </select>
                  </label>

                  <label className="field-label field-span-full">
                    <span>Example from recent work</span>
                    <textarea
                      className="textarea-input"
                      value={question.example_text}
                      onChange={(event) => updateTargetedAnswer(index, 'example_text', event.target.value)}
                      placeholder={question.optional_example_prompt || 'Share one concise example from recent work.'}
                      disabled={busyAction !== null}
                    />
                  </label>

                  <label className="field-label field-span-full">
                    <span>Optional note</span>
                    <textarea
                      className="textarea-input textarea-input-compact"
                      value={question.notes}
                      onChange={(event) => updateTargetedAnswer(index, 'notes', event.target.value)}
                      placeholder="Add any nuance, constraint, or context that helps interpret the answer."
                      disabled={busyAction !== null}
                    />
                  </label>
                </div>
              </article>
            ))}
          </div>
        )}
      </section>

      <section className="board-panel">
        <div className="panel-heading">
          <span className="section-tag">{hiddenSkillsHeading}</span>
          <h3>Capture strengths that may not be visible elsewhere</h3>
          <p>{hiddenSkillsCopy}</p>
        </div>

        <div className="assessment-hidden-skill-list">
          {hiddenSkills.length === 0 ? (
            <article className="review-card">
              <p>No hidden-skill rows yet. Add one only if something meaningful is missing from the targeted questions.</p>
            </article>
          ) : (
            hiddenSkills.map((item, index) => (
              <article key={item.id} className="review-card assessment-hidden-skill-card">
                <div className="review-card-head">
                  <div>
                    <span className="summary-label">Hidden skill {index + 1}</span>
                    <h4>{item.skill_name_en || 'Untitled skill'}</h4>
                  </div>
                  <button
                    className="secondary-button source-action-button is-danger"
                    onClick={() => removeHiddenSkill(item.id)}
                    disabled={busyAction !== null}
                  >
                    Remove
                  </button>
                </div>

                <div className="assessment-answer-grid">
                  <label className="field-label">
                    <span>Skill name (EN)</span>
                    <input
                      className="text-input"
                      value={item.skill_name_en}
                      onChange={(event) => updateHiddenSkill(item.id, 'skill_name_en', event.target.value)}
                      placeholder="Example: Roadmapping"
                      disabled={busyAction !== null}
                    />
                  </label>

                  <label className="field-label">
                    <span>Skill name (RU, optional)</span>
                    <input
                      className="text-input"
                      value={item.skill_name_ru}
                      onChange={(event) => updateHiddenSkill(item.id, 'skill_name_ru', event.target.value)}
                      placeholder="Optional translated skill name"
                      disabled={busyAction !== null}
                    />
                  </label>

                  <label className="field-label">
                    <span>Current level</span>
                    <select
                      className="select-input"
                      value={item.self_rated_level}
                      onChange={(event) => updateHiddenSkill(item.id, 'self_rated_level', event.target.value)}
                      disabled={busyAction !== null}
                    >
                      {LEVEL_OPTIONS.map((value) => (
                        <option key={value} value={value}>
                          {value} / 5
                        </option>
                      ))}
                    </select>
                  </label>

                  <label className="field-label">
                    <span>Confidence</span>
                    <select
                      className="select-input"
                      value={item.answer_confidence}
                      onChange={(event) => updateHiddenSkill(item.id, 'answer_confidence', event.target.value)}
                      disabled={busyAction !== null}
                    >
                      {CONFIDENCE_OPTIONS.map((option) => (
                        <option key={option.value} value={option.value}>
                          {option.label}
                        </option>
                      ))}
                    </select>
                  </label>

                  <label className="field-label field-span-full">
                    <span>Example from recent work</span>
                    <textarea
                      className="textarea-input textarea-input-compact"
                      value={item.example_text}
                      onChange={(event) => updateHiddenSkill(item.id, 'example_text', event.target.value)}
                      placeholder="Describe where this hidden or adjacent skill showed up."
                      disabled={busyAction !== null}
                    />
                  </label>
                </div>
              </article>
            ))
          )}
        </div>

        <div className="form-actions">
          <button className="secondary-button" onClick={addHiddenSkill} disabled={busyAction !== null}>
            Add hidden skill
          </button>
        </div>
      </section>

      <section className="board-panel">
        <div className="panel-heading">
          <span className="section-tag">{aspirationHeading}</span>
          <h3>Share your direction of travel</h3>
          <p>{aspirationCopy}</p>
        </div>

        <div className="assessment-answer-grid">
          <label className="field-label">
            <span>Target role family</span>
            <input
              className="text-input"
              value={draft?.aspiration.target_role_family || ''}
              onChange={(event) => updateAspiration('target_role_family', event.target.value)}
              placeholder="Example: platform_sre_engineer"
              disabled={busyAction !== null}
            />
          </label>

          <label className="field-label">
            <span>Interest signal</span>
            <select
              className="select-input"
              value={draft?.aspiration.interest_signal || ''}
              onChange={(event) => updateAspiration('interest_signal', event.target.value)}
              disabled={busyAction !== null}
            >
              {INTEREST_SIGNAL_OPTIONS.map((option) => (
                <option key={option.value || 'none'} value={option.value}>
                  {option.label}
                </option>
              ))}
            </select>
          </label>

          <label className="field-label field-span-full">
            <span>Aspiration notes</span>
            <textarea
              className="textarea-input"
              value={draft?.aspiration.notes || ''}
              onChange={(event) => updateAspiration('notes', event.target.value)}
              placeholder="What role directions or capability growth interest you most right now?"
              disabled={busyAction !== null}
            />
          </label>
        </div>
      </section>

      <section className="board-panel">
        <div className="panel-heading">
          <span className="section-tag">Final context</span>
          <h3>Anything else that helps interpret your answers?</h3>
          <p>{closingPrompt}</p>
        </div>

        <label className="field-label">
          <span>Confidence statement</span>
          <textarea
            className="textarea-input textarea-input-compact"
            value={draft?.confidence_statement || ''}
            onChange={(event) =>
              setDraft((currentValue) =>
                currentValue === null
                  ? currentValue
                  : {
                      ...currentValue,
                      confidence_statement: event.target.value,
                    },
              )
            }
            placeholder="Optional: note any uncertainty, recency limits, or context for the answers above."
            disabled={busyAction !== null}
          />
        </label>
      </section>

      <section className="board-panel">
        <div className="panel-heading">
          <span className="section-tag">Actions</span>
          <h3>Save draft or submit final answers</h3>
          <p>Draft save keeps the link editable. Final submission locks the pack and records the self-assessment.</p>
        </div>

        <div className="form-actions">
          <button className="secondary-button" onClick={() => void handleSaveDraft()} disabled={busyAction !== null}>
            {busyAction === 'save' ? 'Saving draft...' : 'Save draft'}
          </button>
          <button className="primary-button" onClick={() => void handleSubmitFinal()} disabled={busyAction !== null}>
            {busyAction === 'submit' ? 'Submitting final...' : 'Submit final'}
          </button>
        </div>
      </section>
    </div>
  )
}
