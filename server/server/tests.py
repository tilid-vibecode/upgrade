from fastapi.routing import APIRoute
from django.test import SimpleTestCase

from company_intake.fastapi_views import company_intake_router
from company_intake.prototype_fastapi_views import prototype_workspace_router
from development_plans.prototype_fastapi_views import prototype_development_plans_router
from employee_assessment.prototype_fastapi_views import prototype_employee_assessment_router
from evidence_matrix.prototype_fastapi_views import prototype_evidence_matrix_router
from media_storage.fastapi_views import media_router
from media_storage.prototype_fastapi_views import prototype_media_router
from org_context.prototype_fastapi_views import prototype_org_context_router, prototype_planning_context_router
from server.fastapi_main import app
from server.health import health_router
from skill_blueprint.prototype_fastapi_views import prototype_skill_blueprint_router


class FastAPIRouteRegistrationTests(SimpleTestCase):
    def test_intended_app_router_routes_are_exposed_under_api_v1(self):
        source_routers = [
            health_router,
            company_intake_router,
            prototype_workspace_router,
            media_router,
            prototype_media_router,
            prototype_org_context_router,
            prototype_planning_context_router,
            prototype_skill_blueprint_router,
            prototype_employee_assessment_router,
            prototype_evidence_matrix_router,
            prototype_development_plans_router,
        ]

        expected_routes = {
            self._route_signature(route, prefix='/api/v1')
            for router in source_routers
            for route in router.routes
            if isinstance(route, APIRoute)
        }
        actual_routes = {
            self._route_signature(route)
            for route in app.routes
            if isinstance(route, APIRoute)
        }

        self.assertFalse(
            expected_routes - actual_routes,
            msg=f'Missing FastAPI routes: {sorted(expected_routes - actual_routes)}',
        )

    @staticmethod
    def _route_signature(route: APIRoute, *, prefix: str = '') -> tuple[str, tuple[str, ...]]:
        methods = tuple(sorted(method for method in route.methods if method not in {'HEAD', 'OPTIONS'}))
        return f'{prefix}{route.path}', methods
