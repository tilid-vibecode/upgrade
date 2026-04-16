import { AppLink } from '../app/navigation'
import { useWorkspaceShell } from '../app/WorkspaceLayout'
import { formatPercent, formatShortId } from '../shared/formatters'
import {
  getWorkspaceNavItem,
  getWorkspacePageKeyForWorkflowStage,
  humanizeToken,
  type WorkspacePageKey,
} from '../shared/workflow'
import StatusChip from '../shared/ui/StatusChip'

type BlockerItem = {
  stageKey: string
  stageLabel: string
  text: string
  stageIndex: number
}

function buildPrioritizedBlockers(
  currentStageKey: string,
  nextStageKey: string,
  stages: Array<{ key: string; label: string; blockers: string[] }>,
) {
  const stagePriority = new Map<string, number>()
  if (currentStageKey) {
    stagePriority.set(currentStageKey, 0)
  }
  if (nextStageKey && !stagePriority.has(nextStageKey)) {
    stagePriority.set(nextStageKey, 1)
  }

  return stages
    .flatMap((stage, stageIndex): BlockerItem[] =>
      stage.blockers.map((text) => ({
        stageKey: stage.key,
        stageLabel: stage.label,
        text,
        stageIndex,
      })),
    )
    .sort((left, right) => {
      const leftPriority = stagePriority.get(left.stageKey) ?? left.stageIndex + 2
      const rightPriority = stagePriority.get(right.stageKey) ?? right.stageIndex + 2
      return leftPriority - rightPriority
    })
}

function buildRouteSuggestions(
  focusStageKey: string,
  blockerStageKeys: string[],
  includeContextsRoute: boolean,
  summary: {
    latest_team_plan_uuid: string | null
    latest_matrix_run_uuid: string | null
    latest_assessment_cycle_uuid: string | null
    current_published_blueprint_run_uuid: string | null
    latest_blueprint_run_uuid: string | null
  },
) {
  const suggestions: WorkspacePageKey[] = []
  const pushPageKey = (pageKey: WorkspacePageKey) => {
    if (!suggestions.includes(pageKey)) {
      suggestions.push(pageKey)
    }
  }

  if (focusStageKey) {
    pushPageKey(getWorkspacePageKeyForWorkflowStage(focusStageKey))
  }

  blockerStageKeys.forEach((stageKey) => {
    if (suggestions.length < 3) {
      pushPageKey(getWorkspacePageKeyForWorkflowStage(stageKey))
    }
  })

  if (includeContextsRoute && suggestions.length < 3) {
    pushPageKey('contexts')
  }

  if (suggestions.length === 0) {
    if (summary.latest_team_plan_uuid) {
      pushPageKey('plans')
    } else if (summary.latest_matrix_run_uuid) {
      pushPageKey('matrix')
    } else if (summary.latest_assessment_cycle_uuid) {
      pushPageKey('assessments')
    } else if (summary.current_published_blueprint_run_uuid || summary.latest_blueprint_run_uuid) {
      pushPageKey('blueprint')
    } else {
      pushPageKey('profile')
    }
  }

  return suggestions.slice(0, 3)
}

export default function WorkspaceOverviewPage() {
  const { workspace, workflow, activePlanningContext, buildScopedWorkspacePath } = useWorkspaceShell()
  const currentStage = workflow.stages.find((stage) => stage.key === workflow.summary.current_stage_key) ?? null
  const nextStage = workflow.stages.find((stage) => stage.key === workflow.summary.next_stage_key) ?? null
  const publicationStage = workflow.stages.find((stage) => stage.key === 'clarifications') ?? null
  const focusStage =
    currentStage && currentStage.status !== 'completed'
      ? currentStage
      : nextStage ?? workflow.stages.find((stage) => stage.status !== 'completed') ?? null
  const prioritizedBlockers = buildPrioritizedBlockers(
    workflow.summary.current_stage_key,
    workflow.summary.next_stage_key,
    workflow.stages,
  )
  const recommendedPageKey = getWorkspacePageKeyForWorkflowStage(focusStage?.key || workflow.summary.next_stage_key)
  const blockerStageKeys = prioritizedBlockers
    .map((blocker) => blocker.stageKey)
    .filter((pageKey, index, items) => items.indexOf(pageKey) === index)
  const routeSuggestions = buildRouteSuggestions(
    focusStage?.key || workflow.summary.next_stage_key,
    blockerStageKeys,
    true,
    workflow.summary,
  )

  return (
    <div className="page-stack">
      <section className="summary-grid">
        <article className="summary-card">
          <span className="summary-label">Scope</span>
          <strong>{activePlanningContext?.name || 'Legacy workspace'}</strong>
          <p>{activePlanningContext ? `${humanizeToken(activePlanningContext.kind)} context` : 'No planning context selected'}</p>
        </article>
      </section>

      <section className="overview-primary-grid">
        <article className="stage-status-card">
          <div className="workspace-card-head">
            <div>
              <span className="section-tag">Next action</span>
              <h4>{focusStage?.label || 'Workflow complete'}</h4>
            </div>
            <StatusChip status={focusStage?.status || 'completed'} />
          </div>
          <p>
            {focusStage?.recommended_action || 'No immediate action is required. The workflow is currently complete.'}
          </p>
          <div className="hero-actions">
            <AppLink
              className="primary-button link-button"
              to={buildScopedWorkspacePath(recommendedPageKey)}
            >
              Open {getWorkspaceNavItem(recommendedPageKey).label}
            </AppLink>
          </div>
        </article>

        <article className="stage-status-card">
          <div className="workspace-card-head">
            <div>
              <span className="section-tag">Top blockers</span>
              <h4>{workflow.summary.total_blocker_count > 0 ? `${workflow.summary.total_blocker_count} blocker(s)` : 'No blockers reported'}</h4>
            </div>
            <StatusChip status={workflow.summary.total_blocker_count > 0 ? 'blocked' : 'completed'} />
          </div>

          {prioritizedBlockers.length > 0 ? (
            <div className="blocker-stack">
              {prioritizedBlockers.slice(0, 6).map((blocker) => (
                <article key={`${blocker.stageKey}:${blocker.text}`} className="blocker-item">
                  <span className="quiet-pill">{blocker.stageLabel}</span>
                  <p>{blocker.text}</p>
                </article>
              ))}
            </div>
          ) : (
            <p>No blockers are currently stopping the next stage.</p>
          )}
        </article>
      </section>

      <section className="board-panel">
        <div className="panel-heading">
          <span className="section-tag">Latest workflow artifacts</span>
          <h3>Current downstream run references</h3>
          <p>The overview keeps these surfaced so operators can tell how far the workspace has progressed.</p>
        </div>

        <div className="summary-grid">
          <article className="summary-card">
            <span className="summary-label">Blueprint publication</span>
            <div className="status-row">
              <strong>{formatShortId(workflow.summary.current_published_blueprint_run_uuid || workflow.summary.latest_blueprint_run_uuid)}</strong>
              <StatusChip status={publicationStage?.status || (workflow.summary.blueprint_published ? 'completed' : 'not_started')} />
            </div>
            <p>{publicationStage?.recommended_action || (workflow.summary.blueprint_published ? 'A published blueprint is available.' : 'No published blueprint yet.')}</p>
          </article>

          <article className="summary-card">
            <span className="summary-label">Latest blueprint run</span>
            <div className="status-row">
              <strong>{formatShortId(workflow.summary.latest_blueprint_run_uuid)}</strong>
              <StatusChip status={workflow.summary.latest_blueprint_status || 'not_started'} />
            </div>
            <p>{workflow.summary.latest_blueprint_status ? humanizeToken(workflow.summary.latest_blueprint_status) : 'No blueprint run has been created yet.'}</p>
          </article>

          <article className="summary-card">
            <span className="summary-label">Assessment cycle</span>
            <div className="status-row">
              <strong>{formatShortId(workflow.summary.latest_assessment_cycle_uuid)}</strong>
              <StatusChip status={workflow.summary.latest_assessment_status || 'not_started'} />
            </div>
            <p>{formatPercent(workflow.summary.assessment_completion_rate)} completion</p>
          </article>

          <article className="summary-card">
            <span className="summary-label">Evidence matrix</span>
            <div className="status-row">
              <strong>{formatShortId(workflow.summary.latest_matrix_run_uuid)}</strong>
              <StatusChip status={workflow.summary.latest_matrix_status || 'not_started'} />
            </div>
            <p>{workflow.summary.latest_matrix_status ? humanizeToken(workflow.summary.latest_matrix_status) : 'No matrix run has been created yet.'}</p>
          </article>

          <article className="summary-card">
            <span className="summary-label">Team plans</span>
            <div className="status-row">
              <strong>{formatShortId(workflow.summary.latest_team_plan_uuid)}</strong>
              <StatusChip status={workflow.summary.latest_plan_status || 'not_started'} />
            </div>
            <p>{workflow.summary.latest_plan_status ? humanizeToken(workflow.summary.latest_plan_status) : 'No team plan batch has been created yet.'}</p>
          </article>

          <article className="summary-card">
            <span className="summary-label">Workspace updated</span>
            <strong>{humanizeToken(workspace.status)}</strong>
            <p>{workspace.updated_at ? `Latest shell payload updated at ${new Date(workspace.updated_at).toLocaleString()}.` : 'No update timestamp available.'}</p>
          </article>
        </div>
      </section>

      <section className="board-panel">
        <div className="panel-heading">
          <span className="section-tag">Next routes</span>
          <h3>Keep moving without leaving the shell</h3>
          <p>
            These routes are derived from the current focus stage first, then from the active blocker set if more guidance is needed.
          </p>
        </div>

        <div className="hero-actions">
          {routeSuggestions.map((pageKey, index) => (
              <AppLink
                key={pageKey}
                className={index === 0 ? 'primary-button link-button' : 'secondary-button link-button'}
                to={buildScopedWorkspacePath(pageKey)}
              >
                Open {getWorkspaceNavItem(pageKey).label}
              </AppLink>
          ))}
        </div>
      </section>
    </div>
  )
}
