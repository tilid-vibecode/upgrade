import AppLayout from './AppLayout'
import WorkspaceLayout from './WorkspaceLayout'
import { usePathname } from './navigation'
import HealthPage from '../pages/HealthPage'
import LegacyRedirectPage from '../pages/LegacyRedirectPage'
import NotFoundPage from '../pages/NotFoundPage'
import PublicAssessmentPage from '../pages/PublicAssessmentPage'
import WorkspaceBlueprintPage from '../pages/WorkspaceBlueprintPage'
import WorkspaceClarificationsPage from '../pages/WorkspaceClarificationsPage'
import WorkspaceContextsPage from '../pages/WorkspaceContextsPage'
import WorkspaceOverviewPage from '../pages/WorkspaceOverviewPage'
import WorkspaceParsePage from '../pages/WorkspaceParsePage'
import WorkspaceProfilePage from '../pages/WorkspaceProfilePage'
import WorkspaceAssessmentsPage from '../pages/WorkspaceAssessmentsPage'
import WorkspaceMatrixPage from '../pages/WorkspaceMatrixPage'
import WorkspacePlansPage from '../pages/WorkspacePlansPage'
import WorkspaceSourcesPage from '../pages/WorkspaceSourcesPage'
import WorkspacesHomePage from '../pages/WorkspacesHomePage'
import { getWorkspacePageKeyBySegment, type WorkspacePageKey } from '../shared/workflow'

type ResolvedRoute =
  | { kind: 'home' }
  | { kind: 'health' }
  | { kind: 'redirect'; to: string }
  | { kind: 'assessment'; packUuid: string }
  | { kind: 'workspace'; workspaceSlug: string; pageKey: WorkspacePageKey }
  | { kind: 'not_found' }

function safeDecode(value: string) {
  try {
    return decodeURIComponent(value)
  } catch {
    return value
  }
}

function resolveRoute(pathname: string): ResolvedRoute {
  const segments = pathname.split('/').filter(Boolean)

  if (segments.length === 0) {
    return { kind: 'home' }
  }

  if (segments.length === 1 && segments[0] === 'health') {
    return { kind: 'health' }
  }

  if (segments.length === 1 && (segments[0] === 'company-upload' || segments[0] === 'questionnaire')) {
    return { kind: 'redirect', to: '/' }
  }

  if (segments.length === 2 && segments[0] === 'assessment' && segments[1]) {
    return {
      kind: 'assessment',
      packUuid: safeDecode(segments[1]),
    }
  }

  if (segments[0] === 'workspaces' && segments[1]) {
    const workspaceSlug = safeDecode(segments[1])

    if (segments.length === 2) {
      return {
        kind: 'workspace',
        workspaceSlug,
        pageKey: 'overview',
      }
    }

    if (segments.length === 3) {
      const pageKey = getWorkspacePageKeyBySegment(segments[2])
      if (pageKey !== null) {
        return {
          kind: 'workspace',
          workspaceSlug,
          pageKey,
        }
      }
    }
  }

  return { kind: 'not_found' }
}

function renderWorkspacePage(pageKey: WorkspacePageKey) {
  if (pageKey === 'overview') {
    return <WorkspaceOverviewPage />
  }

  if (pageKey === 'profile') {
    return <WorkspaceProfilePage />
  }

  if (pageKey === 'contexts') {
    return <WorkspaceContextsPage />
  }

  if (pageKey === 'sources') {
    return <WorkspaceSourcesPage />
  }

  if (pageKey === 'parse') {
    return <WorkspaceParsePage />
  }

  if (pageKey === 'blueprint') {
    return <WorkspaceBlueprintPage />
  }

  if (pageKey === 'clarifications') {
    return <WorkspaceClarificationsPage />
  }

  if (pageKey === 'assessments') {
    return <WorkspaceAssessmentsPage />
  }

  if (pageKey === 'matrix') {
    return <WorkspaceMatrixPage />
  }

  if (pageKey === 'plans') {
    return <WorkspacePlansPage />
  }

  return <NotFoundPage />
}

export default function AppRouter() {
  const pathname = usePathname()
  const route = resolveRoute(pathname)

  if (route.kind === 'assessment') {
    return <PublicAssessmentPage key={route.packUuid} packUuid={route.packUuid} />
  }

  if (route.kind === 'redirect') {
    return (
      <AppLayout>
        <LegacyRedirectPage to={route.to} />
      </AppLayout>
    )
  }

  if (route.kind === 'health') {
    return (
      <AppLayout>
        <HealthPage />
      </AppLayout>
    )
  }

  if (route.kind === 'workspace') {
    return (
      <AppLayout>
        <WorkspaceLayout key={route.workspaceSlug} workspaceSlug={route.workspaceSlug} activePage={route.pageKey}>
          {renderWorkspacePage(route.pageKey)}
        </WorkspaceLayout>
      </AppLayout>
    )
  }

  if (route.kind === 'home') {
    return (
      <AppLayout>
        <WorkspacesHomePage />
      </AppLayout>
    )
  }

  return (
    <AppLayout>
      <NotFoundPage />
    </AppLayout>
  )
}
