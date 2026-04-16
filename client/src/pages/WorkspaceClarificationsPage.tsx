import { useCallback, useEffect, useMemo, useRef, useState } from 'react'

import { AppLink } from '../app/navigation'
import { useWorkspaceShell } from '../app/WorkspaceLayout'
import {
  canRefreshBlueprintFromClarifications,
  canStartBlueprintRevision,
  getClarificationId,
  getClarificationText,
} from '../shared/blueprintReview'
import { CollapsibleHero } from '../shared/ui/CollapsibleHero'
import { getApiErrorMessage, getApiErrorMessages, isApiError } from '../shared/api'
import { formatDateTime, formatShortId } from '../shared/formatters'
import {
  answerPrototypeBlueprintClarifications,
  getPrototypeBlueprintRun,
  getPrototypeClarificationHistory,
  getPrototypeCurrentBlueprintRun,
  getPrototypeLatestBlueprintRun,
  getPrototypeLatestClarificationCycle,
  getPrototypeOpenClarifications,
  refreshPrototypeBlueprintFromClarifications,
  startPrototypeBlueprintRevision,
  type PrototypeClarificationCycle,
  type PrototypeClarificationQuestion,
  type PrototypeClarificationQuestionListResponse,
  type PrototypeSkillBlueprintRun,
} from '../shared/prototypeApi'
import { humanizeToken } from '../shared/workflow'
import { requestGlobalConfirmation } from '../shared/ui/ConfirmationDialog'
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

type AnswerDraft = {
  questionUuid: string
  clarificationId: string
  answerText: string
  status: string
  statusNote: string
  changedTargetModel: boolean
}

function requestOptional<T>(request: Promise<T>) {
  return request.catch((error: unknown) => {
    if (isApiError(error) && error.status === 404) {
      return null
    }
    throw error
  })
}

function stringifyJson(value: unknown) {
  try {
    return JSON.stringify(value, null, 2)
  } catch {
    return String(value)
  }
}

function normalizeOptionalString(value: string) {
  const normalized = value.trim()
  return normalized || undefined
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

function JsonBlock({ value, compact = false }: { value: unknown; compact?: boolean }) {
  return <pre className={compact ? 'code-block code-block-compact' : 'code-block'}>{stringifyJson(value)}</pre>
}

function buildAnswerDraft(question: PrototypeClarificationQuestion): AnswerDraft {
  return {
    questionUuid: question.uuid,
    clarificationId: getClarificationId(question),
    answerText: question.answer_text || '',
    status: question.status || 'open',
    statusNote: question.status_note || '',
    changedTargetModel: question.changed_target_model,
  }
}

function buildHistoryGroups(history: PrototypeClarificationQuestionListResponse | null) {
  if (!history) {
    return []
  }

  const groups = new Map<string, PrototypeClarificationQuestion[]>()

  history.questions.forEach((question) => {
    const key = question.cycle_uuid || question.blueprint_uuid || 'history'
    const items = groups.get(key) ?? []
    items.push(question)
    groups.set(key, items)
  })

  return Array.from(groups.entries())
    .map(([groupKey, questions]) => ({
      groupKey,
      questions: questions.sort((left, right) => right.updated_at.localeCompare(left.updated_at)),
      blueprintUuid: questions[0]?.blueprint_uuid || null,
      cycleUuid: questions[0]?.cycle_uuid || null,
    }))
    .sort((left, right) => {
      const leftDate = left.questions[0]?.updated_at || ''
      const rightDate = right.questions[0]?.updated_at || ''
      return rightDate.localeCompare(leftDate)
    })
}

function countStatuses(questions: PrototypeClarificationQuestion[]) {
  return questions.reduce<Record<string, number>>((accumulator, question) => {
    const key = question.status || 'unknown'
    accumulator[key] = (accumulator[key] ?? 0) + 1
    return accumulator
  }, {})
}

function isDraftChanged(question: PrototypeClarificationQuestion, draft: AnswerDraft) {
  return (
    draft.answerText.trim() !== (question.answer_text || '').trim() ||
    draft.status !== (question.status || 'open') ||
    draft.statusNote.trim() !== (question.status_note || '').trim() ||
    draft.changedTargetModel !== question.changed_target_model
  )
}

export default function WorkspaceClarificationsPage() {
  const {
    workspace,
    workflow,
    activePlanningContext,
    planningContextOptions,
    refreshShell,
    buildScopedWorkspacePath,
  } = useWorkspaceShell()
  const clarificationStage = workflow.stages.find((stage) => stage.key === 'clarifications') ?? null
  const isArchivedContext = activePlanningContext?.status === 'archived'

  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [banner, setBanner] = useState<BannerState | null>(null)

  const [openQuestions, setOpenQuestions] = useState<PrototypeClarificationQuestionListResponse | null>(null)
  const [history, setHistory] = useState<PrototypeClarificationQuestionListResponse | null>(null)
  const [latestCycle, setLatestCycle] = useState<PrototypeClarificationCycle | null>(null)
  const [activeRun, setActiveRun] = useState<PrototypeSkillBlueprintRun | null>(null)
  const [effectiveRun, setEffectiveRun] = useState<PrototypeSkillBlueprintRun | null>(null)
  const [latestRun, setLatestRun] = useState<PrototypeSkillBlueprintRun | null>(null)

  const [operatorNameDraft, setOperatorNameDraft] = useState('')
  const [actionNoteDraft, setActionNoteDraft] = useState('')
  const [answerDrafts, setAnswerDrafts] = useState<Record<string, AnswerDraft>>({})
  const actionBarRef = useRef<HTMLElement | null>(null)
  const operatorNameInputRef = useRef<HTMLInputElement | null>(null)
  const pageLoadRequestIdRef = useRef(0)

  const loadClarificationsPage = useCallback(
    async (preferredActiveRunUuid?: string | null) => {
      const requestId = pageLoadRequestIdRef.current + 1
      pageLoadRequestIdRef.current = requestId
      setLoading(true)
      setError(null)

      try {
        const [openResponse, historyResponse, latestCycleResponse, effectiveResponse, latestResponse] = await Promise.all([
          getPrototypeOpenClarifications(workspace.slug, planningContextOptions),
          getPrototypeClarificationHistory(workspace.slug, planningContextOptions),
          requestOptional(getPrototypeLatestClarificationCycle(workspace.slug, planningContextOptions)),
          requestOptional(getPrototypeCurrentBlueprintRun(workspace.slug, planningContextOptions)),
          requestOptional(getPrototypeLatestBlueprintRun(workspace.slug, planningContextOptions)),
        ])

        if (pageLoadRequestIdRef.current !== requestId) {
          return
        }

        const activeBlueprintUuid =
          openResponse.blueprint_uuid ||
          latestCycleResponse?.blueprint_uuid ||
          (
            preferredActiveRunUuid &&
            (
              preferredActiveRunUuid === effectiveResponse?.uuid ||
              preferredActiveRunUuid === latestResponse?.uuid
            )
              ? preferredActiveRunUuid
              : null
          ) ||
          effectiveResponse?.uuid ||
          latestResponse?.uuid ||
          null

        const activeRunResponse = activeBlueprintUuid
          ? await requestOptional(getPrototypeBlueprintRun(workspace.slug, activeBlueprintUuid))
          : null

        if (pageLoadRequestIdRef.current !== requestId) {
          return
        }

        setOpenQuestions(openResponse)
        setHistory(historyResponse)
        setLatestCycle(latestCycleResponse)
        setActiveRun(activeRunResponse)
        setEffectiveRun(effectiveResponse)
        setLatestRun(latestResponse)
      } catch (loadError) {
        if (pageLoadRequestIdRef.current === requestId) {
          setError(getApiErrorMessage(loadError, 'Failed to load clarification review data.'))
        }
      } finally {
        if (pageLoadRequestIdRef.current === requestId) {
          setLoading(false)
        }
      }
    },
    [planningContextOptions, workspace.slug],
  )

  useEffect(() => {
    loadClarificationsPage().catch(() => undefined)
  }, [loadClarificationsPage])

  useEffect(() => {
    setAnswerDrafts((currentValue) =>
      (openQuestions?.questions ?? []).reduce<Record<string, AnswerDraft>>((accumulator, question) => {
        const key = question.uuid || getClarificationId(question)
        accumulator[key] = currentValue[key] ?? buildAnswerDraft(question)
        return accumulator
      }, {}),
    )
  }, [openQuestions])

  const openQuestionCount = openQuestions?.questions.length ?? 0
  const historyGroups = useMemo(() => buildHistoryGroups(history), [history])
  const activeDiffersFromEffective = activeRun !== null && effectiveRun !== null && activeRun.uuid !== effectiveRun.uuid
  const unsavedDraftCount = useMemo(() => {
    if (!openQuestions || openQuestions.questions.length === 0) {
      return 0
    }

    return openQuestions.questions.reduce((count, question) => {
      const key = question.uuid || getClarificationId(question)
      const draft = answerDrafts[key]
      if (!draft || !isDraftChanged(question, draft)) {
        return count
      }
      return count + 1
    }, 0)
  }, [answerDrafts, openQuestions])

  function revealClarificationActionArea(options?: { focusOperatorName?: boolean }) {
    actionBarRef.current?.scrollIntoView({ behavior: 'smooth', block: 'center' })
    if (options?.focusOperatorName) {
      window.setTimeout(() => {
        operatorNameInputRef.current?.focus()
        operatorNameInputRef.current?.select()
      }, 0)
    }
  }

  function requireOperatorName(actionLabel: string) {
    const normalized = operatorNameDraft.trim()
    if (normalized) {
      return normalized
    }

    setBanner({
      tone: 'warn',
      title: `${actionLabel} needs an operator name.`,
      messages: ['Enter the operator name in the clarification controls before submitting an answer batch or running a clarification action.'],
    })
    revealClarificationActionArea({ focusOperatorName: true })
    return null
  }

  async function handleSubmitAnswers() {
    if (!activeRun) {
      setBanner({
        tone: 'warn',
        title: 'No active clarification run is selected.',
        messages: ['Refresh the clarification page and try again so the operator actions can bind to the current blueprint run.'],
      })
      revealClarificationActionArea()
      return
    }

    if (!openQuestions || openQuestions.questions.length === 0) {
      setBanner({
        tone: 'warn',
        title: 'There are no open clarification questions to save.',
        messages: ['The active queue is already empty. Return to Blueprint if you are ready to refresh or review the run.'],
      })
      revealClarificationActionArea()
      return
    }

    const operatorName = requireOperatorName('Clarification answer submission')
    if (!operatorName) {
      return
    }

    const changedItems = openQuestions.questions
      .map((question) => {
        const key = question.uuid || getClarificationId(question)
        const draft = answerDrafts[key]
        if (!draft || !isDraftChanged(question, draft)) {
          return null
        }
        return {
          question_uuid: draft.questionUuid || undefined,
          clarification_id: draft.clarificationId || undefined,
          answer_text: draft.answerText.trim(),
          status: draft.status || undefined,
          status_note: draft.statusNote.trim(),
          changed_target_model: draft.changedTargetModel,
        }
      })
      .filter((item): item is NonNullable<typeof item> => item !== null)

    if (changedItems.length === 0) {
      setBanner({
        tone: 'warn',
        title: 'No clarification updates selected.',
        messages: ['Change at least one question before submitting the batch answer request.'],
      })
      revealClarificationActionArea()
      return
    }

    setBusy(true)
    setBanner(null)

    try {
      const updatedRun = await answerPrototypeBlueprintClarifications(workspace.slug, activeRun.uuid, {
        operator_name: operatorName,
        items: changedItems,
      })
      await loadClarificationsPage(updatedRun.uuid)
      refreshShell()
      setBanner({
        tone: 'success',
        title: `Saved ${changedItems.length} clarification update${changedItems.length === 1 ? '' : 's'}.`,
        messages: [
          'The answers were saved to the clarification queue.',
          'Saving does not rebuild the blueprint yet. Use "Refresh from clarifications" on Blueprint or in the action bar here when you are ready to recalculate the draft.',
          'If a question is fully resolved, change its status to Accepted or Obsolete. Answered items still count as open clarification work.',
        ],
      })
      revealClarificationActionArea()
    } catch (submitError) {
      setBanner({
        tone: 'error',
        title: 'Clarification answer submission failed.',
        messages: getApiErrorMessages(submitError).length > 0
          ? getApiErrorMessages(submitError)
          : ['The backend rejected the clarification update batch.'],
      })
      revealClarificationActionArea()
    } finally {
      setBusy(false)
    }
  }

  async function handleRefreshFromClarifications() {
    if (!activeRun) {
      return
    }

    const operatorName = requireOperatorName('Clarification refresh')
    if (!operatorName) {
      return
    }

    setBusy(true)
    setBanner(null)

    try {
      const refreshedRun = await refreshPrototypeBlueprintFromClarifications(workspace.slug, activeRun.uuid, {
        operator_name: operatorName,
        refresh_note: normalizeOptionalString(actionNoteDraft),
        skip_employee_matching: false,
      })
      await loadClarificationsPage(refreshedRun.uuid)
      refreshShell()
      setBanner({
        tone: 'success',
        title: `Refreshed ${refreshedRun.title || 'the active run'} from clarifications.`,
        messages: ['Return to Blueprint to review the refreshed run before approval or publication.'],
      })
    } catch (refreshError) {
      setBanner({
        tone: 'error',
        title: 'Refresh from clarifications failed.',
        messages: getApiErrorMessages(refreshError).length > 0
          ? getApiErrorMessages(refreshError)
          : ['The backend did not accept the clarification refresh request.'],
      })
    } finally {
      setBusy(false)
    }
  }

  async function handleStartRevision() {
    if (!activeRun) {
      return
    }

    const operatorName = requireOperatorName('Revision start')
    if (!operatorName) {
      return
    }

    if (!(await requestGlobalConfirmation({
      title: 'Start a new revision from clarifications?',
      description: 'This creates a new mutable draft from the active clarification run while keeping the released baseline unchanged.',
      confirmLabel: 'Start revision',
      cancelLabel: 'Keep current run',
      tone: 'warn',
    }))) {
      return
    }

    setBusy(true)
    setBanner(null)

    try {
      const revisedRun = await startPrototypeBlueprintRevision(workspace.slug, activeRun.uuid, {
        operator_name: operatorName,
        revision_reason: normalizeOptionalString(actionNoteDraft),
        skip_employee_matching: true,
      })
      await loadClarificationsPage(revisedRun.uuid)
      refreshShell()
      setBanner({
        tone: 'success',
        title: `Started revision ${revisedRun.title || 'from the active run'}.`,
        messages: ['A new mutable draft is now available. Continue answering clarifications against the active queue or switch back to Blueprint to inspect the revision.'],
      })
    } catch (revisionError) {
      setBanner({
        tone: 'error',
        title: 'Start revision failed.',
        messages: getApiErrorMessages(revisionError).length > 0
          ? getApiErrorMessages(revisionError)
          : ['The backend did not accept the revision request.'],
      })
    } finally {
      setBusy(false)
    }
  }

  if (loading && !openQuestions && !history) {
    return (
      <LoadingState
        title="Loading clarifications"
        description="Fetching the active clarification queue, history, and current blueprint references."
      />
    )
  }

  if (error && !openQuestions && !history) {
    return (
      <ErrorState
        title="Clarifications failed to load"
        description={error}
        onRetry={() => loadClarificationsPage().catch(() => undefined)}
      />
    )
  }

  return (
    <div className="page-stack">
      <CollapsibleHero
        tag="Stage 06"
        title="Clarification queue and history"
        statusSlot={<StatusChip status={activeRun?.status || 'not_started'} />}
      >
        <div className="hero-copy">
          <p>
            {clarificationStage?.recommended_action ||
              'Resolve open blueprint questions here, then return to the blueprint route for review and release actions.'}
          </p>
          <div className="hero-actions">
            <AppLink className="primary-button link-button" to={buildScopedWorkspacePath('blueprint')}>
              Back to blueprint
            </AppLink>
            <button className="secondary-button" onClick={() => loadClarificationsPage().catch(() => undefined)} disabled={busy}>
              Refresh clarifications
            </button>
          </div>
        </div>

        <div className="blueprint-hero-rail">
          <article className="route-badge">
            <span className="summary-label">Active run</span>
            <strong>{formatShortId(activeRun?.uuid)}</strong>
            <StatusChip status={activeRun?.status || 'not_started'} />
          </article>
          <article className="route-badge">
            <span className="summary-label">Open questions</span>
            <strong>{String(openQuestionCount)}</strong>
            <StatusChip status={openQuestionCount > 0 ? 'action_required' : 'completed'} />
          </article>
        </div>
      </CollapsibleHero>

      {activePlanningContext ? (
        <section className="inline-banner inline-banner-info">
          <strong>Scoped clarification history</strong>
          <span>
            Open questions, clarification history, and the effective blueprint selector are currently scoped to {activePlanningContext.name}.
          </span>
        </section>
      ) : null}

      {isArchivedContext ? (
        <section className="inline-banner inline-banner-warn">
          <strong>Archived contexts are read-only for clarification progression.</strong>
          <span>History stays visible, but refresh and revision actions are disabled while this context is archived.</span>
        </section>
      ) : null}

      {error ? (
        <ErrorState
          title="Latest clarification refresh failed"
          description={`${error} Showing the most recent successful stage data.`}
          onRetry={() => loadClarificationsPage().catch(() => undefined)}
          compact
        />
      ) : null}

      {renderBanner(banner)}

      {activeDiffersFromEffective ? (
        <section className="inline-banner inline-banner-warn">
          <strong>The active clarification queue belongs to a different run than the effective downstream blueprint.</strong>
          <ul className="inline-detail-list">
            <li>Active clarification run: {formatShortId(activeRun?.uuid)}.</li>
            <li>Effective downstream blueprint: {formatShortId(effectiveRun?.uuid)}.</li>
            <li>Finish the clarification and revision workflow before publishing if you want downstream stages to consume the newer run.</li>
          </ul>
        </section>
      ) : null}

      <section className="summary-grid">
        <article className="summary-card">
          <span className="summary-label">Active clarification run</span>
          <div className="status-row">
            <strong>{activeRun?.title || 'Not available'}</strong>
            <StatusChip status={activeRun?.status || 'not_started'} />
          </div>
          <p>{activeRun ? 'This is the run currently targeted by the open clarification queue.' : 'No active clarification run is visible right now.'}</p>
        </article>
        <article className="summary-card">
          <span className="summary-label">Effective blueprint</span>
          <div className="status-row">
            <strong>{effectiveRun?.title || 'Not available'}</strong>
            <StatusChip status={effectiveRun?.status || 'not_started'} />
          </div>
          <p>{effectiveRun ? 'This run currently drives downstream stages.' : 'No effective blueprint is available yet.'}</p>
        </article>
        <article className="summary-card">
          <span className="summary-label">Latest run</span>
          <div className="status-row">
            <strong>{latestRun?.title || 'Not available'}</strong>
            <StatusChip status={latestRun?.status || 'not_started'} />
          </div>
          <p>{latestRun ? 'The latest working draft may be newer than the effective run.' : 'No blueprint run has been generated yet.'}</p>
        </article>
        <article className="summary-card">
          <span className="summary-label">Latest clarification cycle</span>
          <div className="status-row">
            <strong>{latestCycle?.title || 'Not available'}</strong>
            <StatusChip status={latestCycle?.status || 'not_started'} />
          </div>
          <p>{latestCycle ? `Updated ${formatDateTime(latestCycle.updated_at)}` : 'No clarification cycle history is available yet.'}</p>
        </article>
      </section>

      <section className="blueprint-stage-grid">
        <article className="board-panel">
          <div className="panel-heading">
            <span className="section-tag">Open queue</span>
            <h3>Batch answer and resolution</h3>
            <p>Answer and resolve clarification questions here. Release actions stay on the blueprint route to keep the operator flow clear.</p>
          </div>

          {latestCycle ? (
            <section className="review-card">
              <div className="review-card-head">
                <div>
                  <span className="section-tag">Cycle summary</span>
                  <h4>{latestCycle.title}</h4>
                </div>
                <StatusChip status={latestCycle.status} />
              </div>
              <JsonBlock value={latestCycle.summary} compact />
            </section>
          ) : null}

          <section className="review-card" ref={actionBarRef}>
            <div className="review-card-head">
              <div>
                <span className="section-tag">Clarification action bar</span>
                <h4>Run-local actions</h4>
              </div>
              <StatusChip status={activeRun?.status || 'not_started'} />
            </div>

            <div className="profile-form-grid">
              <label className="field-label">
                <span>Operator name</span>
                <input
                  ref={operatorNameInputRef}
                  className="text-input"
                  value={operatorNameDraft}
                  onChange={(event) => setOperatorNameDraft(event.target.value)}
                  placeholder="Required for answer batches and clarification actions"
                  disabled={busy || isArchivedContext}
                />
              </label>
              <label className="field-label field-span-full">
                <span>Action note</span>
                <textarea
                  className="textarea-input textarea-input-compact"
                  value={actionNoteDraft}
                  onChange={(event) => setActionNoteDraft(event.target.value)}
                  placeholder="Optional refresh note or revision reason"
                  disabled={busy || isArchivedContext}
                />
              </label>
            </div>

            <div className="form-actions">
              {activeRun && canRefreshBlueprintFromClarifications(activeRun) ? (
                <button className="secondary-button" onClick={() => handleRefreshFromClarifications().catch(() => undefined)} disabled={busy || isArchivedContext}>
                  {busy ? 'Working...' : 'Refresh from clarifications'}
                </button>
              ) : null}
              {activeRun && canStartBlueprintRevision(activeRun) ? (
                <button className="secondary-button" onClick={() => handleStartRevision().catch(() => undefined)} disabled={busy || isArchivedContext}>
                  {busy ? 'Working...' : 'Start revision'}
                </button>
              ) : null}
              <AppLink className="secondary-button link-button" to={buildScopedWorkspacePath('blueprint')}>
                Back to blueprint
              </AppLink>
            </div>
          </section>

          {openQuestionCount > 0 ? (
            <div className="detail-stack">
              {openQuestions?.questions.map((question) => {
                const draftKey = question.uuid || getClarificationId(question)
                const draft = answerDrafts[draftKey] ?? buildAnswerDraft(question)
                const evidenceRefs = Array.isArray(question.evidence_refs) ? question.evidence_refs : []
                const hasChanged = isDraftChanged(question, draft)

                return (
                  <article key={draftKey} className="review-card">
                    <div className="review-card-head">
                      <div>
                        <span className="section-tag">Clarification</span>
                        <h4>{getClarificationText(question)}</h4>
                      </div>
                      <StatusChip status={question.status || 'open'} />
                    </div>

                    <div className="review-pill-row">
                      {[
                        question.priority ? `Priority: ${humanizeToken(question.priority)}` : '',
                        question.scope ? `Scope: ${humanizeToken(question.scope)}` : '',
                        question.intended_respondent_type
                          ? `Respondent: ${humanizeToken(question.intended_respondent_type)}`
                          : '',
                        ...question.impacted_roles.map((item) => `Role: ${item}`),
                        ...question.impacted_initiatives.map((item) => `Initiative: ${item}`),
                      ]
                        .filter(Boolean)
                        .map((token) => (
                          <span key={token} className="quiet-pill">
                            {token}
                          </span>
                        ))}
                      {hasChanged ? <span className="quiet-pill is-blocker">Unsaved changes</span> : null}
                    </div>

                    {question.rationale ? <p>{question.rationale}</p> : null}

                    <div className="profile-form-grid">
                      <label className="field-label field-span-full">
                        <span>Answer</span>
                        <textarea
                          className="textarea-input textarea-input-compact"
                          value={draft.answerText}
                          onChange={(event) =>
                            setAnswerDrafts((currentValue) => ({
                              ...currentValue,
                              [draftKey]: {
                                ...draft,
                                answerText: event.target.value,
                              },
                            }))
                          }
                          placeholder="Record the operator answer that should influence the next blueprint refresh."
                          disabled={busy || isArchivedContext}
                        />
                      </label>
                    <label className="field-label">
                      <span>Resolution status</span>
                      <select
                        className="select-input"
                          value={draft.status}
                          onChange={(event) =>
                            setAnswerDrafts((currentValue) => ({
                              ...currentValue,
                              [draftKey]: {
                                ...draft,
                                status: event.target.value,
                              },
                            }))
                          }
                          disabled={busy || isArchivedContext}
                        >
                          <option value="open">Keep open</option>
                          <option value="answered">Answered</option>
                          <option value="accepted">Accepted</option>
                          <option value="rejected">Rejected</option>
                          <option value="obsolete">Obsolete</option>
                        </select>
                        <span className="form-helper-copy">
                          `Answered` keeps the clarification open. Use `Accepted` or `Obsolete` to close it once the answer is good enough.
                        </span>
                    </label>
                      <label className="field-label">
                        <span>Status note</span>
                        <input
                          className="text-input"
                          value={draft.statusNote}
                          onChange={(event) =>
                            setAnswerDrafts((currentValue) => ({
                              ...currentValue,
                              [draftKey]: {
                                ...draft,
                                statusNote: event.target.value,
                              },
                            }))
                          }
                          placeholder="Optional resolution note"
                          disabled={busy || isArchivedContext}
                        />
                      </label>
                    </div>

                    <label className="checkbox-row">
                      <input
                        type="checkbox"
                        checked={draft.changedTargetModel}
                        onChange={(event) =>
                          setAnswerDrafts((currentValue) => ({
                            ...currentValue,
                            [draftKey]: {
                              ...draft,
                              changedTargetModel: event.target.checked,
                            },
                          }))
                        }
                        disabled={busy || isArchivedContext}
                      />
                      <span>Answer changed the target blueprint model</span>
                    </label>

                    {evidenceRefs.length > 0 ? (
                      <details className="detail-disclosure">
                        <summary>View evidence references</summary>
                        <JsonBlock value={evidenceRefs} compact />
                      </details>
                    ) : null}
                  </article>
                )
              })}

              <div className="form-actions">
                <button className="primary-button" onClick={() => handleSubmitAnswers().catch(() => undefined)} disabled={busy || isArchivedContext}>
                  {busy ? 'Saving answers...' : 'Save clarification answers'}
                </button>
                {activeRun ? (
                  <button className="secondary-button" onClick={() => handleRefreshFromClarifications().catch(() => undefined)} disabled={busy || isArchivedContext}>
                    {busy ? 'Working...' : 'Refresh blueprint from saved answers'}
                  </button>
                ) : null}
                <span className="form-helper-copy">
                  {operatorNameDraft.trim()
                    ? unsavedDraftCount > 0
                      ? `Ready to save ${unsavedDraftCount} unsaved clarification update${unsavedDraftCount === 1 ? '' : 's'}.`
                      : 'No unsaved clarification changes yet.'
                    : 'Enter the operator name above before saving clarification answers.'}
                </span>
                <span className="form-helper-copy">
                  Save records the answers. Refresh rebuilds the blueprint draft from the answered clarification set.
                </span>
              </div>
            </div>
          ) : (
            <EmptyState
              title="No open clarifications"
              description="The active run does not currently expose any open clarification questions. Use the history section below to audit prior resolution decisions."
              action={
                <AppLink className="secondary-button link-button" to={buildScopedWorkspacePath('blueprint')}>
                  Back to blueprint
                </AppLink>
              }
            />
          )}
        </article>

        <article className="board-panel">
          <div className="panel-heading">
            <span className="section-tag">History</span>
            <h3>Clarification cycles and past decisions</h3>
            <p>History stays visible here so operators can audit previous resolutions without mixing them into the active answer form.</p>
          </div>

          {historyGroups.length > 0 ? (
            <div className="detail-stack">
              {historyGroups.map((group) => {
                const statusCounts = countStatuses(group.questions)
                return (
                  <section key={group.groupKey} className="review-card">
                    <div className="review-card-head">
                      <div>
                        <span className="section-tag">Clarification cycle</span>
                        <h4>{formatShortId(group.cycleUuid || group.groupKey)}</h4>
                      </div>
                      <span className="quiet-pill">{group.questions.length} question(s)</span>
                    </div>
                    <div className="review-pill-row">
                      {Object.entries(statusCounts).map(([status, count]) => (
                        <span key={status} className="quiet-pill">
                          {humanizeToken(status)}: {count}
                        </span>
                      ))}
                      {group.blueprintUuid ? <span className="quiet-pill">Run: {formatShortId(group.blueprintUuid)}</span> : null}
                    </div>
                    <div className="detail-stack">
                      {group.questions.slice(0, 8).map((question) => (
                        <article key={question.uuid} className="blueprint-history-item">
                          <div className="workspace-card-head">
                            <strong>{getClarificationText(question)}</strong>
                            <StatusChip status={question.status || 'unknown'} />
                          </div>
                          <p>
                            {question.answer_text || question.status_note || question.rationale || 'No answer or note was recorded for this history row.'}
                          </p>
                          <div className="review-pill-row">
                            {question.answered_by ? <span className="quiet-pill">Answered by {question.answered_by}</span> : null}
                            {question.answered_at ? <span className="quiet-pill">{formatDateTime(question.answered_at)}</span> : null}
                            {question.changed_target_model ? <span className="quiet-pill is-blocker">Changed target model</span> : null}
                          </div>
                        </article>
                      ))}
                    </div>
                  </section>
                )
              })}
            </div>
          ) : (
            <EmptyState
              title="No clarification history yet"
              description="Clarification history will appear here after the first question cycle is created and answered."
            />
          )}
        </article>
      </section>
    </div>
  )
}
