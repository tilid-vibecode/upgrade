import json
import logging
import re
from copy import deepcopy
from collections import Counter, defaultdict
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urljoin, urldefrag, urlparse

import httpx
from asgiref.sync import sync_to_async
from django.db import transaction
from django.db.models import Case, IntegerField, Value, When

from company_intake.models import IntakeWorkspace, WorkspaceSourceKind, WorkspaceSourceStatus
from company_intake.services import build_planning_context_profile_snapshot, build_workspace_profile_snapshot
from org_context.models import (
    Employee,
    EmployeeCVProfile,
    EmployeeOrgAssignment,
    EmployeeProjectAssignment,
    EmployeeRoleMatch,
    EmployeeSkillEvidence,
    OccupationMapping,
    OrgUnit,
    ParsedSource,
    PlanningContext,
    Project,
    RoadmapAnalysisRun,
    RoleProfile,
    RoleSkillRequirement,
    Skill,
    SkillAlias,
)
from org_context.skill_catalog import (
    ensure_workspace_skill_sync,
    normalize_skill_seed,
    resolve_esco_occupation_sync,
    slugify_key,
)
from org_context.services import extract_html_text, extract_links_from_html
from org_context.vector_indexing import (
    format_retrieved_evidence_digest,
    retrieve_workspace_evidence_sync,
)
from tools.openai.structured_client import StructuredLLMError, call_openai_structured

from .models import (
    BLUEPRINT_REVIEW_READY_STATUSES,
    BlueprintStatus,
    ClarificationCycle,
    ClarificationCycleStatus,
    ClarificationQuestion,
    ClarificationQuestionStatus,
    RoleLibraryEntry,
    RoleLibrarySnapshot,
    RoleLibraryStatus,
    SkillBlueprintRun,
)

logger = logging.getLogger(__name__)

_FALLBACK_ROLE_LIBRARY_BASE_URLS = [
    'https://handbook.gitlab.com/job-description-library/',
    'https://handbook.gitlab.com/job-description-library/engineering/',
    'https://handbook.gitlab.com/job-description-library/product/',
    'https://handbook.gitlab.com/job-description-library/design/',
    'https://handbook.gitlab.com/job-description-library/data/',
    'https://handbook.gitlab.com/job-description-library/marketing/',
    'https://handbook.gitlab.com/job-description-library/sales/',
    'https://handbook.gitlab.com/job-description-library/chief-executive-officer/',
]
ROLE_LIBRARY_SEED_MANIFEST_PATH = Path(__file__).with_name('role_library_seed_manifest.json')
ROLE_LIBRARY_SOURCE_RAW_BASE_URL = 'https://gitlab.com/gitlab-com/content-sites/handbook/-/raw/main/content/'


@lru_cache(maxsize=1)
def get_role_library_seed_manifest() -> dict[str, Any]:
    fallback = {
        'version': 'inline-fallback',
        'provider': 'gitlab_handbook',
        'base_urls': _FALLBACK_ROLE_LIBRARY_BASE_URLS,
    }
    try:
        payload = json.loads(ROLE_LIBRARY_SEED_MANIFEST_PATH.read_text(encoding='utf-8'))
    except FileNotFoundError:
        logger.warning('Role-library seed manifest is missing at %s; using inline fallback seeds.', ROLE_LIBRARY_SEED_MANIFEST_PATH)
        return fallback
    except json.JSONDecodeError as exc:
        logger.warning('Role-library seed manifest could not be parsed: %s; using inline fallback seeds.', exc)
        return fallback

    base_urls = [str(item).strip() for item in payload.get('base_urls', []) if str(item).strip()]
    if not base_urls:
        base_urls = _FALLBACK_ROLE_LIBRARY_BASE_URLS

    return {
        **fallback,
        **payload,
        'base_urls': base_urls,
    }


def get_default_role_library_base_urls() -> list[str]:
    return list(get_role_library_seed_manifest().get('base_urls', _FALLBACK_ROLE_LIBRARY_BASE_URLS))


DEFAULT_ROLE_LIBRARY_BASE_URLS = get_default_role_library_base_urls()
DEFAULT_ROLE_LIBRARY_URLS = DEFAULT_ROLE_LIBRARY_BASE_URLS

_URL_FETCH_TIMEOUT = 45.0
_URL_USER_AGENT = 'UpgradePrototypeBot/0.2 (+https://example.invalid)'
_ROLE_LIBRARY_PATH_MARKERS = ('/job-families/', '/job-description-library/')
_CLOSED_CLARIFICATION_STATUSES = {
    ClarificationQuestionStatus.ACCEPTED,
    ClarificationQuestionStatus.OBSOLETE,
}
_OPEN_CLARIFICATION_STATUSES = {
    ClarificationQuestionStatus.OPEN,
    ClarificationQuestionStatus.ANSWERED,
    ClarificationQuestionStatus.REJECTED,
}
_ACTIONABLE_CLARIFICATION_RUN_STATUSES = (
    BlueprintStatus.DRAFT,
    BlueprintStatus.NEEDS_CLARIFICATION,
    BlueprintStatus.REVIEWED,
    BlueprintStatus.COMPLETED,
)
_MUTABLE_BLUEPRINT_STATUSES = {
    BlueprintStatus.DRAFT,
    BlueprintStatus.RUNNING,
    BlueprintStatus.NEEDS_CLARIFICATION,
    BlueprintStatus.REVIEWED,
    BlueprintStatus.COMPLETED,
}

CANONICAL_ROLE_FAMILIES: dict[str, dict[str, Any]] = {
    'backend_engineer': {
        'label': 'Backend Engineer',
        'department': 'Engineering',
        'match_keywords': ['backend', 'server', 'api'],
        'title_patterns': [r'\bbackend\b', r'\bserver[- ]side\b', r'\bapi\b'],
        'overlay_required_skills': ['Python', 'API Design', 'SQL', 'System Design', 'Testing'],
        'overlay_desirable_skills': ['Docker', 'Kubernetes', 'Cloud Infrastructure'],
        'stakeholder_expectations': ['Cross-functional delivery with product and design'],
        'occupation': {'key': 'software-backend-engineer', 'name_en': 'Backend Software Engineer'},
    },
    'frontend_engineer': {
        'label': 'Frontend Engineer',
        'department': 'Engineering',
        'match_keywords': ['frontend', 'front-end', 'ui'],
        'title_patterns': [r'\bfront[- ]end\b', r'\bfrontend\b', r'\bweb ui\b'],
        'overlay_required_skills': ['JavaScript', 'TypeScript', 'React', 'Testing', 'HTML/CSS'],
        'overlay_desirable_skills': ['Experimentation', 'Product Analytics'],
        'stakeholder_expectations': ['Cross-functional delivery with design and product'],
        'occupation': {'key': 'software-frontend-engineer', 'name_en': 'Frontend Software Engineer'},
    },
    'fullstack_engineer': {
        'label': 'Full-Stack Engineer',
        'department': 'Engineering',
        'match_keywords': ['fullstack', 'full-stack'],
        'title_patterns': [r'\bfull[- ]stack\b', r'\bfullstack\b'],
        'overlay_required_skills': ['JavaScript', 'TypeScript', 'React', 'Python', 'API Design'],
        'overlay_desirable_skills': ['SQL', 'Docker', 'Experimentation'],
        'stakeholder_expectations': ['Cross-functional delivery with product and design'],
        'occupation': {'key': 'software-fullstack-engineer', 'name_en': 'Full-Stack Software Engineer'},
    },
    'mobile_engineer': {
        'label': 'Mobile Engineer',
        'department': 'Engineering',
        'match_keywords': ['mobile', 'ios', 'android'],
        'title_patterns': [r'\bmobile\b', r'\bios\b', r'\bandroid\b'],
        'overlay_required_skills': ['Mobile Development', 'API Design', 'Testing'],
        'overlay_desirable_skills': ['Experimentation', 'Product Analytics'],
        'stakeholder_expectations': ['Mobile release coordination across product and design'],
        'occupation': {'key': 'software-mobile-engineer', 'name_en': 'Mobile Software Engineer'},
    },
    'qa_engineer': {
        'label': 'QA / Test Engineer',
        'department': 'Engineering',
        'match_keywords': ['qa', 'quality', 'test', 'sdet'],
        'title_patterns': [r'\bqa\b', r'\bquality\b', r'\btest(ing)?\b', r'\bsdet\b'],
        'overlay_required_skills': ['Testing', 'Test Automation', 'CI/CD'],
        'overlay_desirable_skills': ['API Design', 'Observability'],
        'stakeholder_expectations': ['Quality ownership across engineering and product'],
        'occupation': {'key': 'quality-assurance-engineer', 'name_en': 'Quality Assurance Engineer'},
    },
    'platform_sre_engineer': {
        'label': 'Platform / SRE Engineer',
        'department': 'Engineering',
        'match_keywords': ['platform', 'sre', 'site-reliability', 'devops', 'infrastructure'],
        'title_patterns': [r'\bsre\b', r'\bsite reliability\b', r'\bplatform\b', r'\bdevops\b', r'\binfrastructure\b'],
        'overlay_required_skills': ['CI/CD', 'Docker', 'Kubernetes', 'Cloud Infrastructure', 'Observability'],
        'overlay_desirable_skills': ['System Design', 'Site Reliability Engineering'],
        'stakeholder_expectations': ['Reliability collaboration across engineering teams'],
        'occupation': {'key': 'platform-site-reliability-engineer', 'name_en': 'Platform / Site Reliability Engineer'},
    },
    'data_ml_engineer': {
        'label': 'Data / ML Engineer',
        'department': 'Data',
        'match_keywords': ['data engineer', 'ml', 'machine-learning', 'ai'],
        'title_patterns': [r'\bdata engineer\b', r'\bml\b', r'\bmachine learning\b', r'\bai\b'],
        'overlay_required_skills': ['Data Pipelines', 'SQL', 'Python'],
        'overlay_desirable_skills': ['Machine Learning', 'Product Analytics'],
        'stakeholder_expectations': ['Data collaboration with product and engineering'],
        'occupation': {'key': 'data-ml-engineer', 'name_en': 'Data / Machine Learning Engineer'},
    },
    'data_product_analyst': {
        'label': 'Data / Product Analyst',
        'department': 'Data',
        'match_keywords': ['analyst', 'analytics', 'product analyst', 'data analyst'],
        'title_patterns': [r'\bproduct analyst\b', r'\bdata analyst\b', r'\banalyst\b', r'\banalytics\b'],
        'overlay_required_skills': ['SQL', 'Product Analytics', 'Experimentation', 'A/B Testing'],
        'overlay_desirable_skills': ['Stakeholder Management'],
        'stakeholder_expectations': ['Insight delivery to product, marketing, and leadership'],
        'occupation': {'key': 'product-data-analyst', 'name_en': 'Product / Data Analyst'},
    },
    'product_manager': {
        'label': 'Product Manager',
        'department': 'Product',
        'match_keywords': ['product manager', 'pm', 'technical product manager'],
        'title_patterns': [r'\bproduct manager\b', r'\btechnical product manager\b', r'\bgrowth product manager\b'],
        'overlay_required_skills': ['Roadmapping', 'Product Discovery', 'Stakeholder Management'],
        'overlay_desirable_skills': ['Experimentation', 'Product Analytics', 'GTM Collaboration'],
        'stakeholder_expectations': ['Cross-functional delivery with engineering, design, and GTM'],
        'occupation': {'key': 'product-manager', 'name_en': 'Product Manager'},
    },
    'product_designer': {
        'label': 'Product Designer',
        'department': 'Design',
        'match_keywords': ['designer', 'ux', 'ui', 'product design'],
        'title_patterns': [r'\bproduct designer\b', r'\bux\b', r'\bui\b', r'\bdesign\b'],
        'overlay_required_skills': ['Figma', 'User Research', 'Product Discovery'],
        'overlay_desirable_skills': ['Experimentation', 'Stakeholder Management'],
        'stakeholder_expectations': ['Design collaboration with product and engineering'],
        'occupation': {'key': 'product-designer', 'name_en': 'Product Designer'},
    },
    'growth_product_marketer': {
        'label': 'Growth / Product Marketer',
        'department': 'Marketing',
        'match_keywords': ['product marketing', 'growth', 'marketer', 'gtm'],
        'title_patterns': [r'\bproduct marketing\b', r'\bgrowth\b', r'\bmarketer\b', r'\bgtm\b'],
        'overlay_required_skills': ['GTM Collaboration', 'Experimentation', 'Stakeholder Management'],
        'overlay_desirable_skills': ['Product Analytics', 'A/B Testing'],
        'stakeholder_expectations': ['Cross-functional delivery with product, sales, and customer teams'],
        'occupation': {'key': 'growth-product-marketer', 'name_en': 'Growth / Product Marketer'},
    },
    'marketing_specialist': {
        'label': 'Marketing / Content Specialist',
        'department': 'Marketing',
        'match_keywords': ['marketing specialist', 'content manager', 'content marketing', 'community manager', 'email marketing'],
        'title_patterns': [
            r'\bmarketing specialist\b',
            r'\bmarkenting specialist\b',
            r'\bmarketing manager\b',
            r'\bcontent manager\b',
            r'\bcontent marketing\b',
            r'\bcommunity manager\b',
            r'\bemail marketing\b',
            r'\bcontent specialist\b',
        ],
        'overlay_required_skills': ['Content Creation', 'Campaign Planning', 'Analytics (Google Analytics)'],
        'overlay_desirable_skills': ['Community Management', 'SEO', 'CRM'],
        'stakeholder_expectations': ['Coordinate content, acquisition, and brand work with product and growth'],
        'occupation': {'key': 'marketing-specialist', 'name_en': 'Marketing Specialist'},
    },
    'business_development_manager': {
        'label': 'Business Development Manager',
        'department': 'Sales',
        'match_keywords': ['business development', 'business developer', 'partnerships', 'partnership manager'],
        'title_patterns': [
            r'\bbusiness development\b',
            r'\bbusiness developer\b',
            r'\bpartnerships?\b',
            r'\bpartnership manager\b',
        ],
        'overlay_required_skills': ['Stakeholder Management', 'Negotiation', 'GTM Collaboration'],
        'overlay_desirable_skills': ['Market Research', 'Sales Discovery'],
        'stakeholder_expectations': ['Cross-functional coordination with sales, product, and leadership'],
        'occupation': {'key': 'ict-business-development-manager', 'name_en': 'ICT Business Development Manager'},
    },
    'sales_manager': {
        'label': 'Sales / Revenue Lead',
        'department': 'Sales',
        'match_keywords': ['sales lead', 'sales manager', 'account executive', 'b2b sales', 'revenue'],
        'title_patterns': [
            r'\bsales lead\b',
            r'\bsales manager\b',
            r'\baccount executive\b',
            r'\bb2b sales\b',
            r'\brevenue\b',
            r'\bsdr\b',
            r'\bbdr\b',
        ],
        'overlay_required_skills': ['Sales Discovery', 'Negotiation', 'Stakeholder Management'],
        'overlay_desirable_skills': ['CRM', 'GTM Collaboration', 'Market Research'],
        'stakeholder_expectations': ['Translate revenue goals into repeatable commercial execution'],
        'occupation': {'key': 'sales-manager', 'name_en': 'Sales Manager'},
    },
    'support_manager': {
        'label': 'Support / Help Desk Manager',
        'department': 'Support',
        'match_keywords': ['support manager', 'technical support', 'customer support', 'help desk', 'service desk', 'support specialist'],
        'title_patterns': [
            r'\bsupport manager\b',
            r'\btechnical support\b',
            r'\bcustomer support\b',
            r'\bhelp ?desk\b',
            r'\bservice desk\b',
            r'\bsupport specialist\b',
        ],
        'overlay_required_skills': ['Stakeholder Management', 'Troubleshooting', 'Process Improvement'],
        'overlay_desirable_skills': ['Knowledge Management', 'Customer Communication'],
        'stakeholder_expectations': ['Own service quality and customer feedback loops'],
        'occupation': {'key': 'ict-help-desk-manager', 'name_en': 'ICT Help Desk Manager'},
    },
    'founding_engineer': {
        'label': 'Founding Engineer',
        'department': 'Engineering',
        'match_keywords': ['founding engineer', 'startup engineer', 'first engineer'],
        'title_patterns': [
            r'\bfounding engineer\b',
            r'\bfounding software engineer\b',
            r'\bfirst engineer\b',
            r'\bstartup engineer\b',
        ],
        'overlay_required_skills': ['System Design', 'API Design', 'Python', 'React'],
        'overlay_desirable_skills': ['Cloud Infrastructure', 'Product Discovery', 'Stakeholder Management'],
        'stakeholder_expectations': ['Operate across product, engineering, and go-to-market ambiguity'],
        'occupation': {'key': 'software-developer', 'name_en': 'Software Developer'},
    },
    'executive_leader': {
        'label': 'Executive Leadership',
        'department': 'Leadership',
        'match_keywords': ['ceo', 'chief executive', 'founder', 'executive'],
        'title_patterns': [
            r'\bceo\b',
            r'\bchief executive\b',
            r'\bfounder\b',
            r'\bco-founder\b',
            r'\bexecutive\b',
        ],
        'overlay_required_skills': ['Strategic Planning', 'Stakeholder Management', 'Decision Making'],
        'overlay_desirable_skills': ['Fundraising', 'Organizational Design'],
        'stakeholder_expectations': ['Set direction, resolve tradeoffs, and align company-level priorities'],
        'occupation': {'key': 'chief-executive-officer', 'name_en': 'Chief Executive Officer'},
    },
    'engineering_manager': {
        'label': 'Engineering Lead / Manager',
        'department': 'Engineering',
        'match_keywords': ['engineering manager', 'engineering lead', 'head of engineering'],
        'title_patterns': [r'\bengineering manager\b', r'\bengineering lead\b', r'\bhead of engineering\b', r'\bvp engineering\b'],
        'overlay_required_skills': ['Stakeholder Management', 'Roadmapping', 'System Design'],
        'overlay_desirable_skills': ['Hiring and Coaching', 'Platform Reliability'],
        'stakeholder_expectations': ['Cross-functional delivery and people leadership'],
        'occupation': {'key': 'engineering-manager', 'name_en': 'Engineering Manager'},
    },
    'uncategorized': {
        'label': 'Uncategorized Role',
        'department': 'Other',
        'match_keywords': [],
        'title_patterns': [],
        'overlay_required_skills': [],
        'overlay_desirable_skills': [],
        'stakeholder_expectations': [],
        'occupation': {'key': 'uncategorized-role', 'name_en': 'Uncategorized Role'},
    },
}

CURATED_ROLE_LIBRARY_SEED_HINTS = [
    {'family': family, 'keywords': config['match_keywords']}
    for family, config in CANONICAL_ROLE_FAMILIES.items()
]

ROLE_FAMILY_HINT_ALIASES: dict[str, str] = {
    'product': 'product_manager',
    'product management': 'product_manager',
    'design': 'product_designer',
    'ux': 'product_designer',
    'ui': 'product_designer',
    'marketing': 'marketing_specialist',
    'growth': 'growth_product_marketer',
    'product marketing': 'growth_product_marketer',
    'content': 'marketing_specialist',
    'content marketing': 'marketing_specialist',
    'community': 'marketing_specialist',
    'community management': 'marketing_specialist',
    'content manager': 'marketing_specialist',
    'community manager': 'marketing_specialist',
    'business development': 'business_development_manager',
    'partnerships': 'business_development_manager',
    'sales': 'sales_manager',
    'revenue': 'sales_manager',
    'go to market': 'sales_manager',
    'gtm': 'sales_manager',
    'support': 'support_manager',
    'technical support': 'support_manager',
    'customer support': 'support_manager',
    'help desk': 'support_manager',
    'service desk': 'support_manager',
    'leadership': 'executive_leader',
    'executive': 'executive_leader',
}

ROLE_LIBRARY_ENTRY_SCHEMA = {
    'type': 'object',
    'additionalProperties': False,
    'properties': {
        'role_name': {'type': 'string'},
        'department': {'type': 'string'},
        'role_family': {'type': 'string'},
        'summary': {'type': 'string'},
        'levels': {
            'type': 'array',
            'items': {
                'type': 'object',
                'additionalProperties': False,
                'properties': {
                    'level_name': {'type': 'string'},
                    'job_level': {'type': 'string'},
                    'responsibilities': {'type': 'array', 'items': {'type': 'string'}},
                    'requirements': {'type': 'array', 'items': {'type': 'string'}},
                    'skills': {'type': 'array', 'items': {'type': 'string'}},
                },
                'required': ['level_name', 'job_level', 'responsibilities', 'requirements', 'skills'],
            },
        },
        'responsibilities': {'type': 'array', 'items': {'type': 'string'}},
        'requirements': {'type': 'array', 'items': {'type': 'string'}},
        'skills': {'type': 'array', 'items': {'type': 'string'}},
        'required_skills': {'type': 'array', 'items': {'type': 'string'}},
        'desirable_skills': {'type': 'array', 'items': {'type': 'string'}},
        'seniority_signals': {'type': 'array', 'items': {'type': 'string'}},
        'stakeholder_expectations': {'type': 'array', 'items': {'type': 'string'}},
        'canonical_role_family_hint': {'type': 'string'},
    },
    'required': [
        'role_name',
        'department',
        'role_family',
        'summary',
        'levels',
        'responsibilities',
        'requirements',
        'skills',
        'required_skills',
        'desirable_skills',
        'seniority_signals',
        'stakeholder_expectations',
        'canonical_role_family_hint',
    ],
}

BLUEPRINT_SCHEMA = {
    'type': 'object',
    'additionalProperties': False,
    'properties': {
        'company_context': {
            'type': 'object',
            'additionalProperties': False,
            'properties': {
                'company_name': {'type': 'string'},
                'what_company_does': {'type': 'string'},
                'why_skills_improvement_now': {'type': 'string'},
                'products': {'type': 'array', 'items': {'type': 'string'}},
                'customers': {'type': 'array', 'items': {'type': 'string'}},
                'markets': {'type': 'array', 'items': {'type': 'string'}},
                'locations': {'type': 'array', 'items': {'type': 'string'}},
                'current_tech_stack': {'type': 'array', 'items': {'type': 'string'}},
                'planned_tech_stack': {'type': 'array', 'items': {'type': 'string'}},
                'missing_information': {'type': 'array', 'items': {'type': 'string'}},
            },
            'required': [
                'company_name', 'what_company_does', 'why_skills_improvement_now',
                'products', 'customers', 'markets', 'locations', 'current_tech_stack',
                'planned_tech_stack', 'missing_information',
            ],
        },
        'roadmap_context': {
            'type': 'array',
            'items': {
                'type': 'object',
                'additionalProperties': False,
                'properties': {
                    'initiative_id': {'type': 'string'},
                    'title': {'type': 'string'},
                    'category': {'type': 'string'},
                    'summary': {'type': 'string'},
                    'time_horizon': {'type': 'string'},
                    'desired_market_outcome': {'type': 'string'},
                    'target_customer_segments': {'type': 'array', 'items': {'type': 'string'}},
                    'tech_stack': {'type': 'array', 'items': {'type': 'string'}},
                    'success_metrics': {'type': 'array', 'items': {'type': 'string'}},
                    'product_implications': {'type': 'array', 'items': {'type': 'string'}},
                    'market_implications': {'type': 'array', 'items': {'type': 'string'}},
                    'functions_required': {'type': 'array', 'items': {'type': 'string'}},
                    'confidence': {'type': 'number'},
                    'ambiguities': {'type': 'array', 'items': {'type': 'string'}},
                    'criticality': {'type': 'string'},
                },
                'required': [
                    'initiative_id', 'title', 'category', 'summary', 'time_horizon',
                    'desired_market_outcome', 'target_customer_segments', 'tech_stack', 'success_metrics',
                    'product_implications', 'market_implications', 'functions_required',
                    'confidence', 'ambiguities', 'criticality',
                ],
            },
        },
        'role_candidates': {
            'type': 'array',
            'items': {
                'type': 'object',
                'additionalProperties': False,
                'properties': {
                    'role_name': {'type': 'string'},
                    'canonical_role_family': {'type': 'string'},
                    'role_family': {'type': 'string'},
                    'seniority': {'type': 'string'},
                    'headcount_needed': {'type': 'integer'},
                    'related_initiatives': {'type': 'array', 'items': {'type': 'string'}},
                    'rationale': {'type': 'string'},
                    'responsibilities': {'type': 'array', 'items': {'type': 'string'}},
                    'role_already_exists_internally': {'type': 'boolean'},
                    'likely_requires_hiring': {'type': 'boolean'},
                    'confidence': {'type': 'number'},
                    'ambiguity_notes': {'type': 'array', 'items': {'type': 'string'}},
                    'skills': {
                        'type': 'array',
                        'items': {
                            'type': 'object',
                            'additionalProperties': False,
                            'properties': {
                                'skill_name_en': {'type': 'string'},
                                'skill_name_ru': {'type': 'string'},
                                'target_level': {'type': 'integer'},
                                'priority': {'type': 'integer'},
                                'reason': {'type': 'string'},
                                'requirement_type': {'type': 'string'},
                                'criticality': {'type': 'string'},
                                'supported_initiatives': {'type': 'array', 'items': {'type': 'string'}},
                                'confidence': {'type': 'number'},
                            },
                            'required': [
                                'skill_name_en', 'skill_name_ru', 'target_level', 'priority', 'reason',
                                'requirement_type', 'criticality', 'supported_initiatives', 'confidence',
                            ],
                        },
                    },
                },
                'required': [
                    'role_name', 'canonical_role_family', 'role_family', 'seniority', 'headcount_needed',
                    'related_initiatives', 'rationale', 'responsibilities', 'skills',
                    'role_already_exists_internally', 'likely_requires_hiring', 'confidence', 'ambiguity_notes',
                ],
            },
        },
        'clarification_questions': {
            'type': 'array',
            'items': {
                'type': 'object',
                'additionalProperties': False,
                'properties': {
                    'question': {'type': 'string'},
                    'scope': {'type': 'string'},
                    'priority': {'type': 'string'},
                    'why_it_matters': {'type': 'string'},
                    'impacted_roles': {'type': 'array', 'items': {'type': 'string'}},
                    'impacted_initiatives': {'type': 'array', 'items': {'type': 'string'}},
                },
                'required': [
                    'question', 'scope', 'priority', 'why_it_matters',
                    'impacted_roles', 'impacted_initiatives',
                ],
            },
        },
        'automation_candidates': {
            'type': 'array',
            'items': {
                'type': 'object',
                'additionalProperties': False,
                'properties': {
                    'activity': {'type': 'string'},
                    'reason': {'type': 'string'},
                    'affected_roles': {'type': 'array', 'items': {'type': 'string'}},
                },
                'required': ['activity', 'reason', 'affected_roles'],
            },
        },
        'occupation_map': {
            'type': 'array',
            'items': {
                'type': 'object',
                'additionalProperties': False,
                'properties': {
                    'role_name': {'type': 'string'},
                    'reference_role': {'type': 'string'},
                    'reference_url': {'type': 'string'},
                    'match_reason': {'type': 'string'},
                    'match_score': {'type': 'integer'},
                },
                'required': ['role_name', 'reference_role', 'reference_url', 'match_reason', 'match_score'],
            },
        },
        'assessment_plan': {
            'type': 'object',
            'additionalProperties': False,
            'properties': {
                'global_notes': {'type': 'string'},
                'question_themes': {'type': 'array', 'items': {'type': 'string'}},
                'per_employee_question_count': {'type': 'integer'},
            },
            'required': ['global_notes', 'question_themes', 'per_employee_question_count'],
        },
    },
    'required': [
        'company_context', 'roadmap_context', 'role_candidates', 'clarification_questions',
        'automation_candidates', 'occupation_map', 'assessment_plan',
    ],
}

ROLE_MATCH_SCHEMA = {
    'type': 'object',
    'additionalProperties': False,
    'properties': {
        'matches': {
            'type': 'array',
            'items': {
                'type': 'object',
                'additionalProperties': False,
                'properties': {
                    'role_name': {'type': 'string'},
                    'seniority': {'type': 'string'},
                    'fit_score': {'type': 'integer'},
                    'reason': {'type': 'string'},
                    'related_initiatives': {'type': 'array', 'items': {'type': 'string'}},
                },
                'required': ['role_name', 'seniority', 'fit_score', 'reason', 'related_initiatives'],
            },
        },
    },
    'required': ['matches'],
}

def _dedupe_strings(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        text = re.sub(r'\s+', ' ', str(value or '').strip())
        if not text:
            continue
        key = text.casefold()
        if key in seen:
            continue
        seen.add(key)
        result.append(text)
    return result


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _coerce_int(value: Any, *, default: int = 0, minimum: int = 0, maximum: Optional[int] = None) -> int:
    try:
        normalized = int(value)
    except (TypeError, ValueError):
        normalized = default
    normalized = max(minimum, normalized)
    if maximum is not None:
        normalized = min(maximum, normalized)
    return normalized


def _coerce_confidence(value: Any, *, default: float = 0.6) -> float:
    try:
        normalized = float(value)
    except (TypeError, ValueError):
        normalized = default
    if normalized > 1:
        normalized = normalized / 100.0
    return max(0.0, min(1.0, normalized))


def _normalize_role_fit_score(value: Any) -> float:
    try:
        normalized = float(value)
    except (TypeError, ValueError):
        normalized = 0.0
    if normalized > 1:
        normalized = normalized / 100.0
    return round(max(0.0, min(1.0, normalized)), 2)


def _normalize_requirement_type(value: str) -> str:
    normalized = str(value or '').strip().lower().replace('-', '_').replace(' ', '_')
    if normalized in {'core', 'required'}:
        return 'core'
    if normalized in {'adjacent', 'optional', 'nice_to_have'}:
        return 'adjacent'
    if normalized in {'org_specific', 'company_specific'}:
        return 'org_specific'
    return 'core'


def _merge_requirement_type(base_value: str, incoming_value: str) -> str:
    priority = {'adjacent': 1, 'org_specific': 2, 'core': 3}
    base = _normalize_requirement_type(base_value)
    incoming = _normalize_requirement_type(incoming_value)
    return incoming if priority[incoming] >= priority[base] else base


def _normalize_criticality(value: str, *, priority: int = 0) -> str:
    normalized = str(value or '').strip().lower()
    if normalized in {'critical', 'high'}:
        return 'high'
    if normalized in {'low', 'minor'}:
        return 'low'
    if priority >= 4:
        return 'high'
    if priority <= 1:
        return 'low'
    return 'medium'


def _merge_reason_text(values: list[str]) -> str:
    return ' | '.join(_dedupe_strings(values))[:2000]


def _normalize_family_hint_key(value: str) -> str:
    normalized = re.sub(r'[-_]+', ' ', str(value or '').strip().lower())
    normalized = re.sub(r'[^a-z0-9\s]+', ' ', normalized)
    return re.sub(r'\s+', ' ', normalized).strip()


def _resolve_family_hint_alias(*values: str) -> str:
    for value in values:
        normalized = _normalize_family_hint_key(value)
        if not normalized:
            continue
        canonical_key = normalized.replace(' ', '_')
        if canonical_key in CANONICAL_ROLE_FAMILIES:
            return canonical_key
        if normalized in CANONICAL_ROLE_FAMILIES:
            return normalized
        aliased = ROLE_FAMILY_HINT_ALIASES.get(normalized, '')
        if aliased:
            return aliased
    return ''


def _normalize_role_seniority(title: str, extracted_signals: Optional[list[str]] = None) -> str:
    text = ' '.join([title or '', *list(extracted_signals or [])]).lower()
    rules = [
        ('director', [r'\bdirector\b', r'\bhead\b', r'\bvp\b', r'\bvice president\b']),
        ('manager', [r'\bengineering manager\b', r'\bpeople manager\b', r'\bteam manager\b']),
        ('lead', [r'\blead\b', r'\bprincipal\b', r'\bstaff\b']),
        ('senior', [r'\bsenior\b', r'\bsr\b']),
        ('junior', [r'\bjunior\b', r'\bassociate\b', r'\bentry\b']),
    ]
    for label, patterns in rules:
        if any(re.search(pattern, text) for pattern in patterns):
            return label
    return 'mid'


def normalize_external_role_title(
    *,
    role_name: str,
    role_family_hint: str = '',
    department: str = '',
    page_url: str = '',
) -> dict[str, Any]:
    text = ' '.join([role_name or '', role_family_hint or '', department or '', page_url or '']).lower()
    for family, config in CANONICAL_ROLE_FAMILIES.items():
        if any(re.search(pattern, text) for pattern in config.get('title_patterns', [])):
            return {
                'canonical_family': family,
                'canonical_label': config['label'],
                'normalized_department': config['department'],
            }

    hinted_family = _resolve_family_hint_alias(role_family_hint, department)
    if hinted_family:
        config = CANONICAL_ROLE_FAMILIES[hinted_family]
        return {
            'canonical_family': hinted_family,
            'canonical_label': config['label'],
            'normalized_department': config['department'],
        }

    family = ''
    if 'founding' in text and 'engineer' in text:
        family = 'founding_engineer'
    elif 'ceo' in text or 'chief executive' in text or ('founder' in text and 'engineer' not in text):
        family = 'executive_leader'
    elif 'sales' in text or 'account executive' in text or 'revenue' in text:
        family = 'sales_manager'
    elif 'business development' in text or 'business developer' in text or 'partnership' in text:
        family = 'business_development_manager'
    elif 'support manager' in text or 'technical support' in text or 'help desk' in text or 'service desk' in text:
        family = 'support_manager'
    elif 'community' in text or 'content' in text or 'marketing specialist' in text or 'markenting specialist' in text:
        family = 'marketing_specialist'
    elif 'product' in text and 'marketing' in text:
        family = 'growth_product_marketer'
    elif 'marketing' in text or 'content' in text or 'community' in text:
        family = 'marketing_specialist'
    elif 'product' in text:
        family = 'product_manager'
    elif 'design' in text:
        family = 'product_designer'
    elif 'data' in text or 'analytics' in text:
        family = 'data_product_analyst'
    elif 'growth' in text:
        family = 'growth_product_marketer'
    else:
        if _normalize_family_hint_key(role_family_hint) != 'uncategorized':
            logger.warning(
                'Could not match role title to any canonical family, defaulting to uncategorized: '
                'role_name=%r, role_family_hint=%r, department=%r',
                role_name,
                role_family_hint,
                department,
            )
        family = 'uncategorized'

    config = CANONICAL_ROLE_FAMILIES[family]
    return {
        'canonical_family': family,
        'canonical_label': config['label'],
        'normalized_department': config['department'],
    }


def _select_curated_role_library_urls(
    base_urls: list[str],
    discovered_urls: list[str],
    *,
    max_pages: int,
) -> tuple[list[str], dict[str, Any]]:
    normalized_base_urls = [normalize_url(url) for url in base_urls if url]
    normalized_discovered = [normalize_url(url) for url in discovered_urls if url]

    selected: list[str] = []
    seen: set[str] = set()

    def _add(url: str) -> None:
        normalized = normalize_url(url)
        if normalized and normalized not in seen and len(selected) < max_pages:
            seen.add(normalized)
            selected.append(normalized)

    for url in normalized_base_urls:
        _add(url)

    matched_seed_urls: list[str] = []
    matched_families: set[str] = set()
    for seed in CURATED_ROLE_LIBRARY_SEED_HINTS:
        for url in normalized_discovered:
            lowered = url.lower()
            if any(keyword in lowered for keyword in seed['keywords']):
                _add(url)
                if normalize_url(url) in selected:
                    matched_seed_urls.append(normalize_url(url))
                    matched_families.add(seed['family'])
                break

    for url in normalized_discovered:
        if len(selected) >= max_pages:
            break
        _add(url)

    return selected, {
        'seed_urls_used': selected[:],
        'matched_seed_urls': _dedupe_strings(matched_seed_urls),
        'matched_families': sorted(matched_families),
        'base_urls': normalized_base_urls,
        'discovered_count': len(_dedupe_strings(normalized_discovered)),
        'selected_count': len(selected),
    }


def _normalize_role_library_public_url(url: str) -> str:
    return normalize_url(url)


def _swap_role_library_path_marker(path: str) -> str:
    if '/job-description-library/' in path:
        return path.replace('/job-description-library/', '/job-families/')
    if '/job-families/' in path:
        return path.replace('/job-families/', '/job-description-library/')
    return path


def _build_role_library_url_candidates(url: str) -> list[str]:
    normalized = normalize_url(url)
    if not normalized:
        return []
    parsed = urlparse(normalized)
    if parsed.netloc != 'handbook.gitlab.com':
        return [normalized]

    path = parsed.path or ''
    candidates = [normalized]
    if '/job-description-library/' in path:
        candidates.append(normalize_url(parsed._replace(path=_swap_role_library_path_marker(path)).geturl()))
    elif '/job-families/' in path:
        candidates.append(normalize_url(parsed._replace(path=_swap_role_library_path_marker(path)).geturl()))
    return _dedupe_strings(candidates)


def _build_role_library_markdown_candidates(url: str) -> list[str]:
    normalized = normalize_url(url)
    if not normalized:
        return []

    parsed = urlparse(normalized)
    if parsed.netloc != 'handbook.gitlab.com':
        return []

    content_paths: list[str] = []
    for public_candidate in _build_role_library_url_candidates(normalized):
        public_path = urlparse(public_candidate).path.strip('/')
        if not public_path:
            continue
        content_paths.append(public_path)
        swapped = _swap_role_library_path_marker(public_path)
        if swapped != public_path:
            content_paths.append(swapped)

    candidates: list[str] = []
    for content_path in _dedupe_strings(content_paths):
        raw_base = f'{ROLE_LIBRARY_SOURCE_RAW_BASE_URL}{content_path}'
        candidates.append(f'{raw_base}.md')
        candidates.append(f'{raw_base}/_index.md')
    return _dedupe_strings(candidates)


def _is_role_library_page_url(url: str) -> bool:
    normalized = normalize_url(url).lower()
    return any(marker in normalized for marker in _ROLE_LIBRARY_PATH_MARKERS)


def _merge_role_overlay(extracted: dict[str, Any], *, page_url: str, page_title: str) -> dict[str, Any]:
    normalized_role = normalize_external_role_title(
        role_name=extracted.get('role_name', '') or page_title,
        role_family_hint=extracted.get('canonical_role_family_hint', '') or extracted.get('role_family', ''),
        department=extracted.get('department', ''),
        page_url=page_url,
    )
    family_key = normalized_role['canonical_family']
    family_config = CANONICAL_ROLE_FAMILIES[family_key]

    required_skills = _dedupe_strings([
        *extracted.get('required_skills', []),
        *extracted.get('skills', []),
        *family_config.get('overlay_required_skills', []),
    ])
    desirable_skills = _dedupe_strings([
        *extracted.get('desirable_skills', []),
        *family_config.get('overlay_desirable_skills', []),
    ])
    responsibilities = _dedupe_strings(extracted.get('responsibilities', []))
    requirements = _dedupe_strings(extracted.get('requirements', []))
    stakeholder_expectations = _dedupe_strings([
        *extracted.get('stakeholder_expectations', []),
        *family_config.get('stakeholder_expectations', []),
    ])
    seniority_signals = _dedupe_strings(extracted.get('seniority_signals', []))
    canonical_seniority = _normalize_role_seniority(
        extracted.get('role_name', '') or page_title,
        seniority_signals,
    )
    normalized_required = [normalize_skill_seed(item) for item in required_skills]
    normalized_desirable = [normalize_skill_seed(item) for item in desirable_skills]
    normalized_skills = normalized_required + normalized_desirable
    combined_skill_names = _dedupe_strings([item['display_name_en'] for item in normalized_skills])

    return {
        'role_name': extracted.get('role_name', '') or page_title or page_url,
        'department': extracted.get('department', '') or family_config['department'],
        'role_family': family_key,
        'summary': extracted.get('summary', ''),
        'levels': extracted.get('levels', []),
        'responsibilities': responsibilities,
        'requirements': requirements,
        'skills': combined_skill_names,
        'metadata': {
            'page_title': page_title,
            'external_role_family': extracted.get('role_family', ''),
            'canonical_role_family': family_key,
            'canonical_role_label': family_config['label'],
            'canonical_seniority': canonical_seniority,
            'required_skills': [item['display_name_en'] for item in normalized_required],
            'desirable_skills': [item['display_name_en'] for item in normalized_desirable],
            'normalized_skills': normalized_skills,
            'seniority_signals': seniority_signals,
            'stakeholder_expectations': stakeholder_expectations,
            'occupation_reference': family_config.get('occupation', {}),
            'overlay_applied': True,
        },
    }


def _seed_skill_aliases_from_snapshot_sync(snapshot_pk) -> dict[str, Any]:
    snapshot = RoleLibrarySnapshot.objects.select_related('workspace').get(pk=snapshot_pk)
    workspace = snapshot.workspace
    family_counts: Counter[str] = Counter()
    unique_skill_keys: set[str] = set()
    alias_count = 0

    # Pre-load existing skill metadata to avoid an extra query per skill inside the loop.
    existing_skill_metadata: dict[str, dict] = {
        row['canonical_key']: row['metadata'] or {}
        for row in Skill.objects.filter(workspace=workspace).values('canonical_key', 'metadata')
    }

    for entry in RoleLibraryEntry.objects.filter(snapshot=snapshot).order_by('role_name'):
        entry_metadata = dict(entry.metadata or {})
        canonical_family = entry.role_family or entry_metadata.get('canonical_role_family', '')
        if canonical_family:
            family_counts[canonical_family] += 1

        for skill_payload in entry_metadata.get('normalized_skills', []):
            canonical_key = skill_payload['canonical_key']
            unique_skill_keys.add(canonical_key)
            skill = ensure_workspace_skill_sync(
                workspace,
                normalized_skill=skill_payload,
                preferred_display_name_ru=skill_payload.get('display_name_ru', ''),
                aliases=skill_payload.get('aliases', []),
                created_source='role_library_seed',
                promote_aliases=True,
            )
            merged_metadata = {
                **existing_skill_metadata.get(skill.canonical_key, {}),
                **(skill.metadata or {}),
                'seeded_from_role_library_snapshot_uuid': str(snapshot.uuid),
            }
            Skill.objects.filter(pk=skill.pk).update(metadata=merged_metadata)
            skill.metadata = merged_metadata
            # Update the cache so subsequent iterations see fresh metadata.
            existing_skill_metadata[skill.canonical_key] = merged_metadata
            alias_count += len(skill_payload.get('aliases', []))

    missing_role_families = [
        family
        for family in CANONICAL_ROLE_FAMILIES
        if family not in family_counts
    ]
    quality_flags: list[str] = []
    if missing_role_families:
        quality_flags.append('Some curated role families were not present in this snapshot.')
    if not unique_skill_keys:
        quality_flags.append('No normalized skills were seeded from role-library entries.')

    return {
        'canonical_family_counts': dict(family_counts),
        'normalized_skill_count': len(unique_skill_keys),
        'alias_count': alias_count,
        'missing_role_families': missing_role_families,
        'quality_flags': quality_flags,
    }


def _build_role_library_digest(entries: list[RoleLibraryEntry], *, max_chars: int = 20000) -> str:
    grouped: dict[str, dict[str, Any]] = {}
    for entry in entries:
        metadata = dict(entry.metadata or {})
        family_key = entry.role_family or metadata.get('canonical_role_family') or 'uncategorized'
        family_group = grouped.setdefault(
            family_key,
            {
                'label': CANONICAL_ROLE_FAMILIES.get(family_key, {}).get('label', family_key.replace('_', ' ').title()),
                'titles': [],
                'required_skills': [],
                'desirable_skills': [],
                'stakeholder_expectations': [],
                'occupation': {},
            },
        )
        family_group['titles'].append(entry.role_name)
        family_group['required_skills'].extend(metadata.get('required_skills', []))
        family_group['desirable_skills'].extend(metadata.get('desirable_skills', []))
        family_group['stakeholder_expectations'].extend(metadata.get('stakeholder_expectations', []))
        if not family_group['occupation']:
            family_group['occupation'] = metadata.get('occupation_reference', {})

    lines: list[str] = []
    for family_key in sorted(grouped):
        item = grouped[family_key]
        lines.append(
            f"- {item['label']} ({family_key}) | titles={', '.join(_dedupe_strings(item['titles'])[:4])} "
            f"| required_skills={', '.join(_dedupe_strings(item['required_skills'])[:8])} "
            f"| desirable_skills={', '.join(_dedupe_strings(item['desirable_skills'])[:6])} "
            f"| stakeholder_expectations={', '.join(_dedupe_strings(item['stakeholder_expectations'])[:4])} "
            f"| occupation={item['occupation'].get('name_en', '')}"
        )
    return '\n'.join(lines)[:max_chars]


def _summarize_role_library_entries(entries: list[RoleLibraryEntry]) -> dict[str, Any]:
    family_counts: Counter[str] = Counter()
    normalized_skill_count = 0
    for entry in entries:
        metadata = dict(entry.metadata or {})
        family_key = entry.role_family or metadata.get('canonical_role_family') or ''
        if family_key:
            family_counts[family_key] += 1
        normalized_skill_count += len(metadata.get('normalized_skills', []))

    return {
        'entry_count': len(entries),
        'canonical_family_counts': dict(family_counts),
        'normalized_skill_count': normalized_skill_count,
        'families_present': sorted(family_counts),
    }


async def sync_role_library_for_workspace(
    workspace: IntakeWorkspace,
    *,
    base_urls: Optional[list[str]] = None,
    max_pages: int = 40,
) -> RoleLibrarySnapshot:
    seed_manifest = get_role_library_seed_manifest()
    manifest_base_urls = seed_manifest.get('base_urls', DEFAULT_ROLE_LIBRARY_BASE_URLS)
    urls = _dedupe_strings(
        [
            _normalize_role_library_public_url(item)
            for item in (base_urls or manifest_base_urls)
            if item
        ]
    )
    snapshot = await sync_to_async(RoleLibrarySnapshot.objects.create)(
        workspace=workspace,
        provider='gitlab_handbook',
        status=RoleLibraryStatus.RUNNING,
        base_urls=urls,
    )

    try:
        discovered_urls, discovery_failures = await discover_role_library_urls(
            urls,
            max_pages=max(max_pages * 3, 60),
        )
        selected_urls, discovery_summary = _select_curated_role_library_urls(
            urls,
            discovered_urls,
            max_pages=max_pages,
        )
        if not base_urls:
            discovery_summary['seed_manifest_version'] = seed_manifest.get('version', '')
            discovery_summary['seed_manifest_path'] = str(ROLE_LIBRARY_SEED_MANIFEST_PATH)
        discovery_summary['seed_fetch_failures'] = discovery_failures
        entry_count = 0
        skipped_urls: list[str] = []
        for page_url in selected_urls:
            page_text = await fetch_page_text(page_url)
            if len(page_text['text'].strip()) < 400:
                skipped_urls.append(page_url)
                continue
            extracted = await extract_role_library_entry_with_llm(
                page_url=page_url,
                page_title=page_text['title'],
                text=page_text['text'],
            )
            await sync_to_async(_upsert_role_library_entry_sync)(
                snapshot.pk,
                page_url,
                page_text,
                extracted,
            )
            entry_count += 1

        taxonomy_summary = await sync_to_async(_seed_skill_aliases_from_snapshot_sync)(snapshot.pk)
        await sync_to_async(_complete_role_library_snapshot_sync)(
            snapshot.pk,
            selected_urls,
            entry_count,
            discovery_summary,
            taxonomy_summary,
            skipped_urls,
        )
    except Exception as exc:
        logger.exception('Role library sync failed for workspace %s', workspace.slug)
        await sync_to_async(_fail_role_library_snapshot_sync)(snapshot.pk, str(exc))

    return await sync_to_async(RoleLibrarySnapshot.objects.get)(pk=snapshot.pk)


async def discover_role_library_urls(
    base_urls: list[str],
    *,
    max_pages: int = 40,
) -> tuple[list[str], list[dict[str, str]]]:
    discovered: list[str] = []
    seen: set[str] = set()
    failures: list[dict[str, str]] = []
    for base_url in base_urls:
        normalized_base_url = _normalize_role_library_public_url(base_url)
        try:
            html = await fetch_raw_html(normalized_base_url)
        except Exception as exc:
            try:
                await fetch_role_library_markdown(normalized_base_url)
            except Exception:
                failures.append(
                    {
                        'base_url': normalized_base_url,
                        'error': str(exc),
                    }
                )
                logger.warning('Role-library seed fetch failed for %s: %s', normalized_base_url, exc)
                continue
            if normalized_base_url not in seen:
                seen.add(normalized_base_url)
                discovered.append(normalized_base_url)
            if len(discovered) >= max_pages:
                return discovered, failures
            logger.info('Role-library seed %s was recovered through raw markdown fallback.', normalized_base_url)
            continue
        urls = [normalize_url(normalized_base_url)]
        for href in extract_links_from_html(html):
            absolute = _normalize_role_library_public_url(urljoin(normalized_base_url, href))
            if not absolute.startswith(normalize_url(normalized_base_url)):
                continue
            if not _is_role_library_page_url(absolute):
                continue
            if absolute.endswith(('.png', '.jpg', '.jpeg', '.svg', '.pdf')):
                continue
            urls.append(absolute)

        for url in urls:
            if url not in seen:
                seen.add(url)
                discovered.append(url)
            if len(discovered) >= max_pages:
                return discovered, failures

    if not discovered and failures:
        failure_preview = '; '.join(f"{item['base_url']}: {item['error']}" for item in failures[:3])
        raise ValueError(f'No public role-library pages could be discovered. {failure_preview}')

    return discovered[:max_pages], failures


async def fetch_role_library_markdown(url: str) -> str:
    headers = {'User-Agent': _URL_USER_AGENT}
    async with httpx.AsyncClient(timeout=_URL_FETCH_TIMEOUT, follow_redirects=True, headers=headers) as client:
        last_error: Exception | None = None
        for candidate_url in _build_role_library_markdown_candidates(url):
            try:
                response = await client.get(candidate_url)
                final_host = urlparse(str(response.url)).netloc
                if final_host and final_host != 'gitlab.com':
                    raise ValueError(
                        f'Role-library source request for "{candidate_url}" redirected to "{response.url}", which is not the public GitLab source.'
                    )
                response.raise_for_status()
                return response.text
            except Exception as exc:
                last_error = exc
                continue
        if last_error is not None:
            raise last_error
        raise ValueError(f'No valid role-library markdown candidates were built for "{url}".')


def _extract_role_library_markdown_text(markdown: str, *, url: str) -> dict[str, str]:
    normalized = markdown.replace('\r\n', '\n').replace('\r', '\n')
    frontmatter = ''
    frontmatter_match = re.match(r'^---\n(.*?)\n---\n*', normalized, flags=re.S)
    if frontmatter_match:
        frontmatter = frontmatter_match.group(1)
        normalized = normalized[frontmatter_match.end():]

    title = ''
    if frontmatter:
        title_match = re.search(r'^\s*title:\s*(.+?)\s*$', frontmatter, flags=re.M)
        if title_match:
            title = title_match.group(1).strip().strip('"')
    if not title:
        heading_match = re.search(r'^\s*#\s+(.+?)\s*$', normalized, flags=re.M)
        if heading_match:
            title = heading_match.group(1).strip()

    text = re.sub(r'<!--.*?-->', ' ', normalized, flags=re.S)
    text = re.sub(r'\[([^\]]+)\]\([^)]+\)', r'\1', text)
    text = re.sub(r'`{1,3}', '', text)
    text = re.sub(r'^\s{0,3}#{1,6}\s*', '', text, flags=re.M)
    text = re.sub(r'^\s*[-*+]\s+', '- ', text, flags=re.M)
    text = re.sub(r'\n{3,}', '\n\n', text)
    return {
        'url': url,
        'title': title,
        'text': text[:20000].strip(),
        'source_format': 'markdown',
    }


async def fetch_raw_html(url: str) -> str:
    headers = {'User-Agent': _URL_USER_AGENT}
    async with httpx.AsyncClient(timeout=_URL_FETCH_TIMEOUT, follow_redirects=True, headers=headers) as client:
        last_error: Exception | None = None
        for candidate_url in _build_role_library_url_candidates(url):
            try:
                response = await client.get(candidate_url)
                final_host = urlparse(str(response.url)).netloc
                if final_host and final_host != 'handbook.gitlab.com':
                    raise ValueError(
                        f'Public handbook request for "{candidate_url}" redirected to "{response.url}", which is not a public handbook page.'
                    )
                response.raise_for_status()
                return response.text
            except Exception as exc:
                last_error = exc
                continue
        if last_error is not None:
            raise last_error
        raise ValueError(f'No valid role-library URL candidates were built for "{url}".')


async def fetch_page_text(url: str) -> dict:
    html_payload: dict[str, Any] | None = None
    html_error: Exception | None = None
    try:
        html = await fetch_raw_html(url)
        extracted = extract_html_text(html, url=url)
        html_payload = {
            'url': url,
            'title': (extracted.metadata or {}).get('title', ''),
            'text': extracted.text[:20000].strip(),
            'source_format': 'html',
        }
    except Exception as exc:
        html_error = exc

    markdown_payload: dict[str, Any] | None = None
    markdown_error: Exception | None = None
    if not html_payload or len((html_payload.get('text') or '').strip()) < 400:
        try:
            markdown = await fetch_role_library_markdown(url)
            markdown_payload = _extract_role_library_markdown_text(markdown, url=url)
        except Exception as exc:
            markdown_error = exc

    if markdown_payload and len((markdown_payload.get('text') or '').strip()) >= len((html_payload or {}).get('text') or ''):
        return markdown_payload
    if html_payload:
        return html_payload
    if markdown_error is not None:
        raise markdown_error
    if html_error is not None:
        raise html_error
    raise ValueError(f'Could not extract any role-library content from "{url}".')


async def extract_role_library_entry_with_llm(*, page_url: str, page_title: str, text: str) -> dict:
    system_prompt = (
        'You are extracting structured role-library data from a job description or role family page.\n\n'

        '## Your task\n'
        'Parse the page text below into a structured role profile. Use only information '
        'present in the text — do not invent requirements or skills.\n\n'

        '## Extraction rules\n'
        '- role_name: The specific job title as stated on the page.\n'
        '- department: The organizational department (Engineering, Product, Design, Data, Marketing, etc.).\n'
        '- role_family: The broader family this role belongs to (e.g., "Engineering" for both '
        'Backend Engineer and Frontend Engineer).\n'
        '- summary: One concise sentence describing what this role does.\n'
        '- responsibilities: Extract as concise bullet-like strings. Keep each under 15 words. '
        'Focus on what the person DOES, not what they ARE.\n'
        '- requirements: Hard requirements (experience, education, certifications). '
        'Separate from skills.\n'
        '- required_skills: Technical and professional skills that are explicitly required '
        'or strongly implied. Use concise English skill names (e.g., "Python", "API Design", '
        '"Product Analytics").\n'
        '- desirable_skills: Skills mentioned as "nice to have", "preferred", or "bonus". '
        'Keep separate from required.\n'
        '- seniority_signals: Extract any mentions of level, years of experience, leadership '
        'scope, or seniority indicators (e.g., "Senior", "Staff", "5+ years", "team lead").\n'
        '- stakeholder_expectations: Extract any mentions of cross-functional collaboration, '
        'communication, stakeholder management, or team interaction patterns.\n'
        '- canonical_role_family_hint: If you can infer a canonical family like "backend_engineer", '
        '"product_manager", "qa_engineer", etc., provide it. Otherwise leave empty.\n'
        '- levels: If the page describes multiple seniority levels (Junior, Mid, Senior, Staff), '
        'extract each with its own responsibilities, requirements, and skills.\n\n'

        '## Quality rules\n'
        '- If the page is a family overview (not a specific JD), extract general '
        'responsibilities and skills that apply across the family.\n'
        '- Prefer 5-10 required skills and 3-5 desirable skills. Do not list 20+ skills.\n'
        '- Keep strings concise: responsibilities and requirements under 15 words each.\n'
        '- Do not split one skill into synonyms (e.g., do not list both "React" and "React.js").'
    )
    user_prompt = (
        f'Page URL: {page_url}\n'
        f'Page title: {page_title}\n\n'
        f'Page text:\n{text[:18000]}'
    )
    result = await call_openai_structured(
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        schema_name='gitlab_role_library_entry',
        schema=ROLE_LIBRARY_ENTRY_SCHEMA,
        temperature=0.1,
        max_tokens=3500,
    )
    return result.parsed


async def generate_skill_blueprint(
    workspace: IntakeWorkspace,
    *,
    planning_context: PlanningContext | None = None,
    role_library_snapshot: Optional[RoleLibrarySnapshot] = None,
) -> SkillBlueprintRun:
    if role_library_snapshot is None:
        role_library_snapshot = await sync_to_async(
            lambda: RoleLibrarySnapshot.objects.filter(workspace=workspace, status=RoleLibraryStatus.COMPLETED)
            .order_by('-updated_at')
            .first()
        )()
        if role_library_snapshot is None:
            role_library_snapshot = await sync_role_library_for_workspace(workspace)

    run = await sync_to_async(SkillBlueprintRun.objects.create)(
        workspace=workspace,
        planning_context=planning_context,
        title='First-layer blueprint',
        status=BlueprintStatus.RUNNING,
        role_library_snapshot=role_library_snapshot,
        roadmap_analysis_id=None,
        generation_mode='generation',
        change_log=[
            {
                'event': 'generated',
                'at': _utc_now_iso(),
                'actor': 'system',
                'note': 'Blueprint generation started.',
            }
        ],
    )

    try:
        blueprint_inputs = await sync_to_async(_build_blueprint_inputs_sync)(
            workspace.pk,
            role_library_snapshot.pk,
            planning_context_pk=getattr(planning_context, 'pk', None),
        )
        await sync_to_async(
            lambda: SkillBlueprintRun.objects.filter(pk=run.pk).update(
                roadmap_analysis_id=blueprint_inputs.get('roadmap_analysis_pk')
            )
        )()
        input_snapshot = _build_input_snapshot(
            role_library_snapshot=role_library_snapshot,
            blueprint_inputs=blueprint_inputs,
            generation_mode='generation',
        )
        await sync_to_async(_record_blueprint_run_inputs_sync)(
            run.pk,
            blueprint_inputs['source_summary'],
            input_snapshot,
        )
        llm_payload = await _extract_blueprint_with_llm(workspace, blueprint_inputs)
        normalized_payload = await sync_to_async(_normalize_blueprint_payload_sync)(
            workspace.pk,
            llm_payload,
            workspace_profile_snapshot=blueprint_inputs['workspace_profile'],
        )
        normalized = await sync_to_async(_persist_blueprint_payload_sync)(run.pk, normalized_payload)
        employee_matches = await match_employees_to_roles(
            workspace,
            normalized_payload['role_candidates'],
            blueprint_run_uuid=run.uuid,
            planning_context=planning_context,
        )
        gap_summary, redundancy_summary = await sync_to_async(_compute_role_gap_summaries_sync)(
            workspace.pk,
            normalized_payload['role_candidates'],
            run.uuid,
        )
        coverage_analysis = await sync_to_async(_compute_coverage_analysis_sync)(
            workspace.pk,
            str(run.uuid),
            blueprint_inputs.get('roadmap_analysis_uuid'),
        )
        normalized_payload, gap_summary = _merge_coverage_analysis_into_payload(
            normalized_payload,
            gap_summary,
            coverage_analysis,
        )

        await sync_to_async(_finalize_blueprint_run_sync)(
            run.pk,
            normalized_payload,
            normalized['required_skill_set'],
            employee_matches,
            gap_summary,
            redundancy_summary,
        )
    except Exception as exc:
        logger.exception('Blueprint generation failed for workspace %s', workspace.slug)
        await sync_to_async(_fail_blueprint_run_sync)(run.pk, str(exc))

    return await sync_to_async(SkillBlueprintRun.objects.get)(pk=run.pk)


async def _extract_blueprint_with_llm(workspace: IntakeWorkspace, blueprint_inputs: dict) -> dict:
    system_prompt = (
        'You are a workforce-planning analyst building a first-layer skills and roles blueprint '
        'for a software company preparing for its next roadmap cycle.\n\n'

        '## Your task\n'
        'Analyze the roadmap evidence, org structure, and role references provided below. '
        'Produce a structured blueprint that answers these five questions:\n'
        '1. What is this company trying to achieve in the next 6-12 months?\n'
        '2. Which roadmap initiatives are highest priority, and what functions do they require?\n'
        '3. What is the minimum role set needed to execute that roadmap?\n'
        '4. For each role, what specific skills and target levels are required?\n'
        '5. What is still uncertain or missing, and what clarification questions should the operator answer?\n\n'

        '## Constraints — follow these strictly\n'
        '- MINIMAL ROLE SET: Only include roles that the roadmap evidence actually demands. '
        'Do not generate a universal skills matrix. A 50-person company typically needs 4-8 '
        'distinct role families, not 15-20.\n'
        '- ROADMAP-DRIVEN: Every role candidate must link to at least one roadmap initiative. '
        'If an initiative does not require a distinct role, do not invent one.\n'
        '- WORKSTREAM-ALIGNED ROLES: Anchor roles to specific workstreams and delivery tracks, '
        'not only broad initiatives.\n'
        '- CAPABILITY BUNDLE COVERAGE: The roadmap analysis includes capability bundles. Each '
        'bundle should be covered by at least one role or surfaced as a clarification.\n'
        '- USE DELIVERY RISKS: If the roadmap analysis flags delivery risks or concentration '
        'concerns, reflect them in role design or clarification questions.\n'
        '- ENABLING ROLES: When workstreams imply testing, infrastructure, analytics, security, '
        'or platform needs, include enabling roles when the evidence supports them.\n'
        '- SOFTWARE-COMPANY SCOPE: Consider engineering, product, design, data, QA, '
        'and customer-facing product roles. Only include marketing, GTM, or ops roles '
        'if the roadmap evidence explicitly requires them.\n'
        '- EVIDENCE-BASED CONFIDENCE: Set confidence 0.0-1.0 based on how much evidence '
        'supports each claim. 0.9+ means strong explicit evidence. 0.5-0.7 means inferred '
        'from partial signals. Below 0.5 means speculative — add a clarification question instead.\n'
        '- SKILL LEVELS: Use a 1-5 scale where 1=awareness, 2=guided practice, 3=independent, '
        '4=advanced/leading, 5=expert/architect. Most mid-level roles need level 3-4. '
        'Level 5 is rare and should only appear for genuinely expert-level requirements.\n'
        '- REQUIREMENT TYPES: Mark each skill as "core" (essential for the role), '
        '"adjacent" (valuable but not blocking), or "org_specific" (company-specific need).\n'
        '- CRITICALITY: "high" means roadmap delivery is blocked without this skill. '
        '"medium" means execution quality suffers. "low" means nice-to-have.\n'
        '- EXISTING VS HIRING: If org data shows employees with matching titles or '
        'departments, mark role_already_exists_internally=true. Mark likely_requires_hiring=true '
        'only when no internal coverage is plausible.\n'
        '- CLARIFICATION OVER INVENTION: If you cannot determine something from the evidence, '
        'add a clarification_question with why_it_matters explaining what downstream decision '
        'depends on the answer. Never fabricate details.\n'
        '- BILINGUAL SKILLS: Provide skill_name_en in English. Provide skill_name_ru as a '
        'natural Russian translation or transliteration, not a machine-translated literal.\n\n'

        '## Output quality checks\n'
        'Before returning, verify:\n'
        '- Every role has at least 3 skills with rationale.\n'
        '- Every initiative links to at least one role.\n'
        '- No two role candidates have identical canonical_role_family + seniority unless '
        'the headcount evidence justifies it.\n'
        '- Ambiguities are surfaced as clarification questions, not hidden.\n'
        '- The total role count is proportional to company size (roughly 1 role family per 5-8 employees).'
    )
    company_profile = blueprint_inputs['workspace_profile']['company_profile']
    pilot_scope = blueprint_inputs['workspace_profile']['pilot_scope']
    org_summary = blueprint_inputs['org_summary']
    user_prompt = (
        '## Company profile\n'
        f"{json.dumps(company_profile, ensure_ascii=False, indent=2)}\n\n"
        '## Pilot scope\n'
        f"{json.dumps(pilot_scope, ensure_ascii=False, indent=2)}\n\n"
        '## Organization snapshot\n'
        f"- Employee count: {org_summary.get('employee_count', 0)}\n"
        f"- Departments: {', '.join(org_summary.get('org_units', [])) or 'Not provided'}\n"
        f"- Active projects: {', '.join(org_summary.get('projects', [])) or 'Not provided'}\n"
        f"- Sample current titles: {', '.join(org_summary.get('sample_current_titles', [])[:20]) or 'Not provided'}\n\n"
        '## Source availability\n'
        f"{json.dumps(blueprint_inputs['source_summary'].get('counts_by_kind', {}), ensure_ascii=False)}\n\n"
        '## Roadmap analysis (structured)\n'
        f"{blueprint_inputs['roadmap_input'] or 'No roadmap analysis available — add a clarification question about roadmap priorities.'}\n\n"
        '## Strategy evidence\n'
        f"{blueprint_inputs['strategy_evidence_digest'] or 'No strategy evidence available.'}\n\n"
        '## Role reference evidence (job descriptions, existing matrices)\n'
        f"{blueprint_inputs['role_reference_evidence_digest'] or 'No role reference evidence available.'}\n\n"
        '## Supplemental evidence\n'
        f"{blueprint_inputs['supplemental_evidence_digest'] or 'None.'}\n\n"
        '## Additional parsed text (fallback for retrieval gaps)\n'
        f"{blueprint_inputs['evidence_digest'] or 'None.'}\n\n"
        '## External role library reference\n'
        f"{blueprint_inputs['role_library_digest']}\n\n"
        '## Instructions\n'
        'Produce the blueprint now. Remember: minimal role set, roadmap-driven, '
        'evidence-based confidence, and clarification questions for anything uncertain.'
    )
    result = await call_openai_structured(
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        schema_name='workspace_blueprint',
        schema=BLUEPRINT_SCHEMA,
        temperature=0.2,
        max_tokens=5000,
    )
    return result.parsed


async def _refresh_blueprint_from_clarifications_with_llm(
    workspace: IntakeWorkspace,
    base_run: SkillBlueprintRun,
    *,
    blueprint_inputs: dict[str, Any],
    answered_clarifications: list[dict[str, Any]],
    refresh_note: str,
) -> dict:
    system_prompt = (
        'You are refining an existing skills and roles blueprint for a software company '
        'after the operator has answered clarification questions.\n\n'

        '## Your task\n'
        'Apply the answered clarifications to the current blueprint. '
        'Return the full updated structured blueprint.\n\n'

        '## Constraints — follow these strictly\n'
        '- CONSERVATIVE UPDATES: Only change what the clarified answers justify. '
        'Do not reorganize or rewrite sections that are not affected by the new information.\n'
        '- PRESERVE IDENTIFIERS: Keep existing initiative_id values, canonical_role_family values, '
        'and role_key values stable. Only add or remove entries when the clarification explicitly '
        'changes scope.\n'
        '- PRESERVE ROLE SET SIZE: Do not add new roles unless a clarification answer reveals '
        'a function that was genuinely missing. Do not remove roles unless a clarification '
        'explicitly eliminates a capability need.\n'
        '- CONFIDENCE ADJUSTMENTS: If a clarification resolves an ambiguity, raise the confidence '
        'of affected items. If it reveals new uncertainty, add a new clarification question.\n'
        '- SKILL LEVELS AND TYPES: Use the same 1-5 scale and core/adjacent/org_specific types '
        'as the original blueprint.\n'
        '- RESOLVE OLD QUESTIONS: Mark previously open clarification questions as resolved '
        'when the answered clarifications address them. Only include still-open or newly '
        'discovered questions in the output clarification_questions array.\n'
        '- FULL OUTPUT: Return the complete blueprint structure, including unchanged sections. '
        'The output replaces the previous version entirely.\n\n'

        '## Quality checks before returning\n'
        '- Every answered clarification should visibly affect at least one field (confidence, '
        'rationale, role scope, or skill requirement). If an answer has no impact, note why.\n'
        '- The role count should not increase by more than 1-2 unless the clarification '
        'reveals a major scope change.\n'
        '- New clarification questions should only appear if the answers created new ambiguity.'
    )
    current_blueprint_snapshot = {
        'company_context': base_run.company_context,
        'roadmap_context': base_run.roadmap_context,
        'role_candidates': base_run.role_candidates,
        'clarification_questions': base_run.clarification_questions,
        'required_skill_set': base_run.required_skill_set,
        'occupation_map': base_run.occupation_map,
        'assessment_plan': base_run.assessment_plan,
    }
    user_prompt = (
        f'## Operator refresh note\n{refresh_note or "No extra operator note."}\n\n'
        f'## Answered clarifications\n'
        f'{json.dumps(answered_clarifications, ensure_ascii=False, indent=2)}\n\n'
        f'## Current blueprint (base for this refresh)\n'
        f'{json.dumps(current_blueprint_snapshot, ensure_ascii=False, indent=2)}\n\n'
        f'## Latest source summary\n{json.dumps(blueprint_inputs["source_summary"], ensure_ascii=False, indent=2)}\n\n'
        f'## Roadmap analysis\n{blueprint_inputs["roadmap_input"] or "None"}\n\n'
        f'## Strategy evidence\n{blueprint_inputs["strategy_evidence_digest"] or "None"}\n\n'
        f'## Role reference evidence\n{blueprint_inputs["role_reference_evidence_digest"] or "None"}\n\n'
        f'## Additional parsed text (fallback)\n{blueprint_inputs["evidence_digest"] or "None"}\n\n'
        f'## External role library reference\n{blueprint_inputs["role_library_digest"]}\n\n'
        '## Instructions\n'
        'Apply the answered clarifications to update the blueprint conservatively. '
        'Preserve identifiers and role set stability. Return the full updated blueprint.'
    )
    result = await call_openai_structured(
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        schema_name='workspace_blueprint_clarification_refresh',
        schema=BLUEPRINT_SCHEMA,
        temperature=0.1,
        max_tokens=5000,
    )
    return result.parsed


_EMPLOYEE_MATCH_BATCH_SIZE = 5
_ROLE_RERANK_BATCH_SIZE = 3
_SHORTLIST_THRESHOLD = 20


BATCH_ROLE_MATCH_SCHEMA = {
    'type': 'object',
    'additionalProperties': False,
    'properties': {
        'employee_matches': {
            'type': 'array',
            'items': {
                'type': 'object',
                'additionalProperties': False,
                'properties': {
                    'employee_uuid': {'type': 'string'},
                    'matches': ROLE_MATCH_SCHEMA['properties']['matches'],
                },
                'required': ['employee_uuid', 'matches'],
            },
        },
    },
    'required': ['employee_matches'],
}


ROLE_RERANK_SCHEMA = {
    'type': 'object',
    'additionalProperties': False,
    'properties': {
        'role_results': {
            'type': 'array',
            'items': {
                'type': 'object',
                'additionalProperties': False,
                'properties': {
                    'role_uuid': {'type': 'string'},
                    'matches': {
                        'type': 'array',
                        'items': {
                            'type': 'object',
                            'additionalProperties': False,
                            'properties': {
                                'employee_uuid': {'type': 'string'},
                                'fit_score': {'type': 'integer'},
                                'rationale': {'type': 'string'},
                                'related_initiatives': {'type': 'array', 'items': {'type': 'string'}},
                            },
                            'required': ['employee_uuid', 'fit_score', 'rationale'],
                        },
                    },
                },
                'required': ['role_uuid', 'matches'],
            },
        },
    },
    'required': ['role_results'],
}


def _build_role_catalog_from_persisted_sync(workspace_pk, blueprint_run_uuid: str) -> list[dict[str, Any]]:
    workspace = IntakeWorkspace.objects.get(pk=workspace_pk)
    role_profiles = list(
        RoleProfile.objects.filter(workspace=workspace, blueprint_run_id=blueprint_run_uuid)
        .prefetch_related('skill_requirements__skill')
        .order_by('name', 'seniority')
    )
    catalog: list[dict[str, Any]] = []
    for role_profile in role_profiles:
        metadata = dict(role_profile.metadata or {})
        family_config = CANONICAL_ROLE_FAMILIES.get(role_profile.family, {})
        catalog.append(
            {
                'role_uuid': str(role_profile.uuid),
                'name': role_profile.name,
                'role_name': role_profile.name,
                'seniority': role_profile.seniority,
                'family': role_profile.family,
                'department': family_config.get('department', ''),
                'related_initiatives': list(metadata.get('related_initiatives') or []),
                'role_already_exists_internally': bool(metadata.get('role_already_exists_internally')),
                'likely_requires_hiring': bool(metadata.get('likely_requires_hiring')),
                'skill_requirements': [
                    {
                        'skill_name_en': requirement.skill.display_name_en,
                        'target_level': requirement.target_level,
                        'criticality': (requirement.metadata or {}).get('criticality', ''),
                        'requirement_type': (requirement.metadata or {}).get('requirement_type', ''),
                    }
                    for requirement in role_profile.skill_requirements.all()
                ],
            }
        )
    return catalog


def _normalize_title_tokens(title: str) -> set[str]:
    stopwords = {'the', 'a', 'an', 'of', 'and', 'or', 'in', 'at', 'for', 'to', 'with'}
    tokens = re.split(r'[\s\-_/,]+', str(title or '').lower().strip())
    return {token for token in tokens if token and token not in stopwords}


def _jaccard_similarity(left: set[str], right: set[str]) -> float:
    if not left and not right:
        return 0.0
    union = len(left | right)
    return (len(left & right) / union) if union else 0.0


def _seniority_rank(seniority: str) -> int:
    ranks = {
        'intern': 1,
        'junior': 2,
        'mid': 3,
        'mid-level': 3,
        'senior': 4,
        'lead': 5,
        'staff': 5,
        'principal': 6,
        'director': 7,
        'vp': 8,
        'cto': 9,
        'ceo': 9,
    }
    return ranks.get(str(seniority or '').strip().lower(), 0)


def _compute_shortlist_score(employee: dict, role: dict) -> float:
    score = 0.0

    emp_title_tokens = _normalize_title_tokens(employee.get('current_title', ''))
    role_name_tokens = _normalize_title_tokens(role.get('name', ''))
    role_family_tokens = _normalize_title_tokens(role.get('family', ''))
    score += 0.20 * _jaccard_similarity(emp_title_tokens, role_name_tokens | role_family_tokens)

    emp_skill_keys = {
        slugify_key(item.get('skill_name_en', ''))
        for item in employee.get('skills_from_evidence', [])
        if str(item.get('skill_name_en') or '').strip() and item.get('resolution_status') == Skill.ResolutionStatus.RESOLVED
    }
    role_skill_keys = {
        slugify_key(item.get('skill_name_en', ''))
        for item in role.get('skill_requirements', [])
        if str(item.get('skill_name_en') or '').strip()
    }
    skill_overlap = (len(emp_skill_keys & role_skill_keys) / len(role_skill_keys)) if role_skill_keys else 0.0
    score += 0.30 * skill_overlap

    emp_units = {str(unit or '').lower() for unit in employee.get('org_units', []) if str(unit or '').strip()}
    role_department = str(role.get('department') or '').lower().strip()
    if role_department and any(role_department in unit or unit in role_department for unit in emp_units):
        score += 0.10
    elif role_department and emp_units:
        score += 0.05 * max(
            _jaccard_similarity(_normalize_title_tokens(unit), _normalize_title_tokens(role_department))
            for unit in emp_units
        )

    emp_projects = {str(project or '').lower() for project in employee.get('projects', []) if str(project or '').strip()}
    emp_domains: set[str] = set()
    for role_history_item in employee.get('role_history', []):
        emp_domains.update(str(domain or '').lower() for domain in role_history_item.get('domains', []) if str(domain or '').strip())
    role_initiatives_text = ' '.join(role.get('related_initiatives', [])).lower()
    if emp_domains and role_initiatives_text:
        domain_hits = sum(1 for domain in emp_domains if domain in role_initiatives_text)
        score += 0.10 * min(domain_hits / max(len(emp_domains), 1), 1.0)
    elif emp_projects and role_initiatives_text:
        project_hits = sum(1 for project in emp_projects if project in role_initiatives_text)
        score += 0.05 * min(project_hits / max(len(emp_projects), 1), 1.0)

    max_history_score = 0.0
    for history_item in employee.get('role_history', []):
        history_title_tokens = _normalize_title_tokens(history_item.get('role_title', ''))
        max_history_score = max(
            max_history_score,
            _jaccard_similarity(history_title_tokens, role_name_tokens | role_family_tokens),
        )
        history_domains = {str(domain or '').lower() for domain in history_item.get('domains', []) if str(domain or '').strip()}
        if history_domains & role_family_tokens:
            max_history_score = max(max_history_score, 0.5)
    score += 0.20 * max_history_score

    employee_seniority = _seniority_rank(employee.get('seniority', ''))
    role_seniority = _seniority_rank(role.get('seniority', ''))
    if employee_seniority > 0 and role_seniority > 0:
        seniority_diff = abs(employee_seniority - role_seniority)
        seniority_score = max(0.0, 1.0 - seniority_diff * 0.3)
        score += 0.10 * seniority_score

    return round(score, 4)


def _build_deterministic_shortlist(
    employees: list[dict],
    role_profiles: list[dict],
    workspace: IntakeWorkspace,
    *,
    max_candidates_per_role: int = 8,
) -> dict[str, list[dict]]:
    del workspace
    shortlist: dict[str, list[dict]] = {}
    for role in role_profiles:
        scored: list[tuple[float, dict]] = []
        for employee in employees:
            score = _compute_shortlist_score(employee, role)
            scored.append((score, employee))
        scored.sort(key=lambda item: (-item[0], item[1].get('full_name', '')))
        shortlist[str(role['role_uuid'])] = [
            {**employee, 'shortlist_score': score}
            for score, employee in scored[:max_candidates_per_role]
        ]
    return shortlist


def _compress_shortlist_candidate(employee: dict) -> dict[str, Any]:
    resolved_skills = [
        item for item in (employee.get('skills_from_evidence') or [])
        if item.get('resolution_status') == Skill.ResolutionStatus.RESOLVED
    ]
    return {
        'employee_uuid': employee.get('employee_uuid', ''),
        'full_name': employee.get('full_name', ''),
        'current_title': employee.get('current_title', ''),
        'seniority': employee.get('seniority', ''),
        'headline': employee.get('headline', ''),
        'org_units': list(employee.get('org_units') or [])[:4],
        'projects': list(employee.get('projects') or [])[:4],
        'shortlist_score': employee.get('shortlist_score', 0.0),
        'skills_from_evidence': [
            {
                'skill_name_en': item.get('skill_name_en', ''),
                'confidence': item.get('confidence', 0.0),
            }
            for item in resolved_skills[:8]
        ],
        'role_history': [
            {
                'company_name': item.get('company_name', ''),
                'role_title': item.get('role_title', ''),
                'key_achievement': (item.get('key_achievements') or [''])[:1][0],
            }
            for item in (employee.get('role_history') or [])[:3]
        ],
        'achievements': [
            {'summary': item.get('summary', '')}
            for item in (employee.get('achievements') or [])[:3]
        ],
        'domain_experience': [
            str(item.get('domain') or '')
            for item in (employee.get('domain_experience') or [])[:4]
            if str(item.get('domain') or '').strip()
        ],
        'leadership_signals': [
            str(item.get('signal') or '')
            for item in (employee.get('leadership_signals') or [])[:3]
            if str(item.get('signal') or '').strip()
        ],
    }


async def _rerank_candidates_llm(workspace: IntakeWorkspace, batch_input: list[dict]) -> list[dict]:
    del workspace
    system_prompt = (
        'You are reranking shortlisted employees for specific roles.\n\n'
        'Return up to the top 3 matches per role.\n'
        '- 85-100: Strong fit.\n'
        '- 70-84: Good fit.\n'
        '- 50-69: Partial fit.\n'
        '- Below 50: exclude.\n'
        'Base the rationale on specific role history, achievements, domains, skills, and relevant delivery context.\n'
        'Do not use generic praise without concrete evidence.'
    )
    result = await call_openai_structured(
        system_prompt=system_prompt,
        user_prompt=json.dumps(batch_input, ensure_ascii=False, indent=2),
        schema_name='role_candidate_rerank',
        schema=ROLE_RERANK_SCHEMA,
        temperature=0.1,
        max_tokens=1500 + 400 * len(batch_input),
        timeout=300.0,
    )
    return result.parsed.get('role_results', [])


async def _match_employees_batch_legacy(
    workspace: IntakeWorkspace,
    employees: list[dict],
    role_catalog: list[dict],
    *,
    blueprint_run_uuid,
) -> list[dict]:
    matches_by_employee: list[dict] = []
    legacy_catalog = [
        {
            'role_name': role.get('name', ''),
            'seniority': role.get('seniority', ''),
            'role_family': role.get('family', ''),
            'skills': [item.get('skill_name_en', '') for item in role.get('skill_requirements', [])[:8]],
            'related_initiatives': role.get('related_initiatives', []),
            'role_already_exists_internally': bool(role.get('role_already_exists_internally')),
            'likely_requires_hiring': bool(role.get('likely_requires_hiring')),
        }
        for role in role_catalog
    ]
    for batch_start in range(0, len(employees), _EMPLOYEE_MATCH_BATCH_SIZE):
        batch = employees[batch_start:batch_start + _EMPLOYEE_MATCH_BATCH_SIZE]
        batch_profiles = [
            {
                'employee_uuid': employee['employee_uuid'],
                'full_name': employee['full_name'],
                'current_title': employee['current_title'],
                'seniority': employee.get('seniority', ''),
                'headline': employee.get('headline', ''),
                'org_units': employee['org_units'],
                'projects': employee['projects'],
                'skills_from_evidence': [
                    item
                    for item in employee.get('skills_from_evidence', [])
                    if item.get('resolution_status') == Skill.ResolutionStatus.RESOLVED
                ],
                'role_history': employee.get('role_history', []),
                'achievements': employee.get('achievements', []),
                'domain_experience': employee.get('domain_experience', []),
                'leadership_signals': employee.get('leadership_signals', []),
            }
            for employee in batch
        ]
        system_prompt = (
            'You are matching employees to the most plausible target roles for roadmap execution.\n\n'
            'For each employee, return up to 3 role matches sorted by fit score.\n'
            'Use current title, org context, resolved skill evidence, role history, achievements, domain experience, and leadership signals.\n'
            'If an employee has no plausible match above 40, return an empty matches array.'
        )
        user_prompt = (
            f'## Employee profiles (batch of {len(batch_profiles)})\n'
            f'{json.dumps(batch_profiles, ensure_ascii=False, indent=2)}\n\n'
            f'## Target role catalog\n'
            f'{json.dumps(legacy_catalog, ensure_ascii=False, indent=2)}\n'
        )
        result = await call_openai_structured(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            schema_name='batch_employee_role_matches',
            schema=BATCH_ROLE_MATCH_SCHEMA,
            temperature=0.1,
            max_tokens=1200 + 800 * len(batch),
            timeout=300.0,
        )
        result_by_uuid: dict[str, list[dict]] = {}
        for entry in result.parsed.get('employee_matches', []):
            result_by_uuid[entry.get('employee_uuid', '')] = entry.get('matches', [])[:3]
        for employee in batch:
            persisted = await sync_to_async(_persist_employee_role_matches_sync)(
                workspace.pk,
                str(blueprint_run_uuid),
                employee['employee_uuid'],
                result_by_uuid.get(employee['employee_uuid'], []),
            )
            matches_by_employee.append(
                {
                    'employee_uuid': employee['employee_uuid'],
                    'full_name': employee['full_name'],
                    'matches': persisted,
                }
            )
    return matches_by_employee


async def _match_with_shortlist(
    workspace: IntakeWorkspace,
    *,
    employees: list[dict],
    role_catalog: list[dict],
    shortlist: dict[str, list[dict]],
    blueprint_run_uuid,
) -> list[dict]:
    matches_by_employee: defaultdict[str, list[dict]] = defaultdict(list)
    role_lookup = {str(role['role_uuid']): role for role in role_catalog}
    role_items = list(shortlist.items())
    for batch_start in range(0, len(role_items), _ROLE_RERANK_BATCH_SIZE):
        batch = role_items[batch_start:batch_start + _ROLE_RERANK_BATCH_SIZE]
        batch_input = []
        for role_uuid, candidates in batch:
            role = role_lookup.get(role_uuid)
            if role is None:
                continue
            batch_input.append(
                {
                    'role': {
                        'role_uuid': role_uuid,
                        'name': role.get('name', ''),
                        'seniority': role.get('seniority', ''),
                        'family': role.get('family', ''),
                        'department': role.get('department', ''),
                        'related_initiatives': role.get('related_initiatives', []),
                        'skill_requirements': role.get('skill_requirements', [])[:8],
                    },
                    'candidates': [_compress_shortlist_candidate(candidate) for candidate in candidates],
                }
            )
        role_results = await _rerank_candidates_llm(workspace, batch_input)
        for role_result in role_results:
            role_uuid = str(role_result.get('role_uuid') or '').strip()
            role = role_lookup.get(role_uuid)
            if role is None:
                continue
            for match in role_result.get('matches', [])[:3]:
                employee_uuid = str(match.get('employee_uuid') or '').strip()
                if not employee_uuid:
                    continue
                matches_by_employee[employee_uuid].append(
                    {
                        'employee_uuid': employee_uuid,
                        'role_name': role.get('name', ''),
                        'seniority': role.get('seniority', ''),
                        'fit_score': int(match.get('fit_score') or 0),
                        'reason': str(match.get('rationale') or '').strip(),
                        'related_initiatives': list(match.get('related_initiatives') or role.get('related_initiatives', [])),
                    }
                )

    payload: list[dict] = []
    for employee in employees:
        employee_matches = sorted(
            matches_by_employee.get(employee['employee_uuid'], []),
            key=lambda item: (-int(item.get('fit_score') or 0), item.get('role_name', '')),
        )[:3]
        persisted = await sync_to_async(_persist_employee_role_matches_sync)(
            workspace.pk,
            str(blueprint_run_uuid),
            employee['employee_uuid'],
            employee_matches,
        )
        payload.append(
            {
                'employee_uuid': employee['employee_uuid'],
                'full_name': employee['full_name'],
                'matches': persisted,
            }
        )
    return payload


async def match_employees_to_roles(
    workspace: IntakeWorkspace,
    role_candidates: list[dict],
    *,
    blueprint_run_uuid,
    planning_context: PlanningContext | None = None,
) -> list[dict]:
    del role_candidates
    employees = await sync_to_async(_load_employee_matching_inputs_sync)(
        workspace.pk,
        getattr(planning_context, 'pk', None),
    )
    role_catalog = await sync_to_async(_build_role_catalog_from_persisted_sync)(workspace.pk, str(blueprint_run_uuid))
    if not employees or not role_catalog:
        return []

    await sync_to_async(
        lambda: EmployeeRoleMatch.objects.filter(
            workspace=workspace,
            **(
                {'planning_context': planning_context}
                if planning_context is not None
                else {'planning_context__isnull': True}
            ),
            source_kind='blueprint',
            role_profile__blueprint_run_id=blueprint_run_uuid,
        ).delete()
    )()

    if len(employees) < _SHORTLIST_THRESHOLD:
        return await _match_employees_batch_legacy(
            workspace,
            employees,
            role_catalog,
            blueprint_run_uuid=blueprint_run_uuid,
        )

    shortlist = _build_deterministic_shortlist(
        employees,
        role_catalog,
        workspace,
        max_candidates_per_role=8,
    )
    return await _match_with_shortlist(
        workspace,
        employees=employees,
        role_catalog=role_catalog,
        shortlist=shortlist,
        blueprint_run_uuid=blueprint_run_uuid,
    )


async def get_latest_role_library_snapshot(workspace: IntakeWorkspace) -> Optional[RoleLibrarySnapshot]:
    return await sync_to_async(
        lambda: RoleLibrarySnapshot.objects.filter(workspace=workspace).order_by('-updated_at').first()
    )()


def _scope_blueprint_queryset(queryset, planning_context=None):
    if planning_context is not None:
        return queryset.filter(planning_context=planning_context)
    return queryset.filter(planning_context__isnull=True)


async def get_latest_blueprint_run(
    workspace: IntakeWorkspace,
    *,
    planning_context=None,
    statuses: Optional[tuple[str, ...]] = None,
) -> Optional[SkillBlueprintRun]:
    return await sync_to_async(
        lambda: _scope_blueprint_queryset(
            SkillBlueprintRun.objects.filter(
                workspace=workspace,
                **({'status__in': statuses} if statuses else {}),
            ),
            planning_context=planning_context,
        ).select_related('role_library_snapshot', 'derived_from_run', 'clarification_cycle').order_by('-updated_at').first()
    )()


async def get_latest_review_ready_blueprint_run(workspace: IntakeWorkspace, *, planning_context=None) -> Optional[SkillBlueprintRun]:
    return await get_latest_blueprint_run(
        workspace,
        planning_context=planning_context,
        statuses=BLUEPRINT_REVIEW_READY_STATUSES,
    )


async def get_latest_approved_blueprint_run(workspace: IntakeWorkspace, *, planning_context=None) -> Optional[SkillBlueprintRun]:
    return await get_latest_blueprint_run(
        workspace,
        planning_context=planning_context,
        statuses=(BlueprintStatus.APPROVED,),
    )


async def get_latest_published_blueprint_run(workspace: IntakeWorkspace, *, planning_context=None) -> Optional[SkillBlueprintRun]:
    return await sync_to_async(
        lambda: _scope_blueprint_queryset(
            SkillBlueprintRun.objects.filter(workspace=workspace, is_published=True),
            planning_context=planning_context,
        ).select_related('role_library_snapshot', 'derived_from_run').order_by('-published_at', '-updated_at').first()
    )()


async def get_current_published_blueprint_run(workspace: IntakeWorkspace, *, planning_context=None) -> Optional[SkillBlueprintRun]:
    return await get_latest_published_blueprint_run(workspace, planning_context=planning_context)


def _get_effective_blueprint_run_sync(workspace_pk, planning_context_pk=None) -> Optional[SkillBlueprintRun]:
    planning_context_filter = (
        {'planning_context_id': planning_context_pk}
        if planning_context_pk is not None
        else {'planning_context__isnull': True}
    )
    published = (
        SkillBlueprintRun.objects.filter(
            workspace_id=workspace_pk,
            is_published=True,
            **planning_context_filter,
        )
        .select_related('role_library_snapshot', 'derived_from_run')
        .order_by('-published_at', '-updated_at')
        .first()
    )
    if published is not None:
        return published
    return (
        SkillBlueprintRun.objects.filter(
            workspace_id=workspace_pk,
            status__in=BLUEPRINT_REVIEW_READY_STATUSES,
            **planning_context_filter,
        )
        .select_related('role_library_snapshot', 'derived_from_run')
        .order_by('-updated_at')
        .first()
    )


async def get_effective_blueprint_run(workspace: IntakeWorkspace, *, planning_context=None) -> Optional[SkillBlueprintRun]:
    return await sync_to_async(_get_effective_blueprint_run_sync)(
        workspace.pk,
        getattr(planning_context, 'pk', None),
    )


async def get_default_blueprint_run(workspace: IntakeWorkspace, *, planning_context=None) -> Optional[SkillBlueprintRun]:
    # Backward-compatible alias for the current effective blueprint selection:
    # published snapshot first, otherwise latest review-ready run.
    return await get_effective_blueprint_run(workspace, planning_context=planning_context)


async def get_latest_clarification_cycle(
    workspace: IntakeWorkspace,
    *,
    planning_context=None,
) -> Optional[ClarificationCycle]:
    return await sync_to_async(
        lambda: ClarificationCycle.objects.select_related('blueprint_run')
        .filter(
            workspace=workspace,
            **(
                {'blueprint_run__planning_context': planning_context}
                if planning_context is not None
                else {'blueprint_run__planning_context__isnull': True}
            ),
        )
        .order_by('-updated_at')
        .first()
    )()


def _get_active_clarification_run_sync(workspace_pk, planning_context_pk=None) -> Optional[SkillBlueprintRun]:
    planning_context_filter = (
        {'planning_context_id': planning_context_pk}
        if planning_context_pk is not None
        else {'planning_context__isnull': True}
    )
    latest_mutable = (
        SkillBlueprintRun.objects.filter(
            workspace_id=workspace_pk,
            is_published=False,
            status__in=_ACTIONABLE_CLARIFICATION_RUN_STATUSES,
            **planning_context_filter,
        )
        .select_related('role_library_snapshot', 'derived_from_run')
        .order_by('-updated_at')
        .first()
    )
    if latest_mutable is not None:
        return latest_mutable
    return _get_effective_blueprint_run_sync(workspace_pk, planning_context_pk)


async def get_active_clarification_run(workspace: IntakeWorkspace, *, planning_context=None) -> Optional[SkillBlueprintRun]:
    return await sync_to_async(_get_active_clarification_run_sync)(
        workspace.pk,
        getattr(planning_context, 'pk', None),
    )


async def list_open_clarification_questions(
    workspace: IntakeWorkspace,
    *,
    blueprint_run: Optional[SkillBlueprintRun] = None,
    planning_context=None,
) -> list[ClarificationQuestion]:
    active_run = blueprint_run or await get_active_clarification_run(workspace, planning_context=planning_context)
    if active_run is None:
        return []
    return await sync_to_async(list)(
        ClarificationQuestion.objects.select_related('cycle', 'blueprint_run')
        .filter(
            workspace=workspace,
            blueprint_run=active_run,
            status__in=_OPEN_CLARIFICATION_STATUSES,
        )
        .annotate(
            priority_rank=Case(
                When(priority='high', then=Value(0)),
                When(priority='medium', then=Value(1)),
                When(priority='low', then=Value(2)),
                default=Value(3),
                output_field=IntegerField(),
            )
        )
        .order_by('priority_rank', 'created_at')
    )


async def list_clarification_question_history(workspace: IntakeWorkspace, *, planning_context=None) -> list[ClarificationQuestion]:
    return await sync_to_async(list)(
        ClarificationQuestion.objects.select_related('cycle', 'blueprint_run')
        .filter(
            workspace=workspace,
            **(
                {'blueprint_run__planning_context': planning_context}
                if planning_context is not None
                else {'blueprint_run__planning_context__isnull': True}
            ),
        )
        .annotate(
            priority_rank=Case(
                When(priority='high', then=Value(0)),
                When(priority='medium', then=Value(1)),
                When(priority='low', then=Value(2)),
                default=Value(3),
                output_field=IntegerField(),
            )
        )
        .order_by('-cycle__updated_at', 'priority_rank', 'created_at')
    )


async def get_blueprint_run_or_none(
    workspace: IntakeWorkspace,
    blueprint_uuid,
) -> Optional[SkillBlueprintRun]:
    return await sync_to_async(
        lambda: SkillBlueprintRun.objects.select_related('role_library_snapshot', 'derived_from_run', 'clarification_cycle').filter(
            workspace=workspace,
            uuid=blueprint_uuid,
        ).first()
    )()


async def list_blueprint_runs(workspace: IntakeWorkspace, *, planning_context=None) -> list[SkillBlueprintRun]:
    return await sync_to_async(list)(
        _scope_blueprint_queryset(
            SkillBlueprintRun.objects.select_related('role_library_snapshot', 'derived_from_run', 'clarification_cycle')
            .filter(workspace=workspace),
            planning_context=planning_context,
        )
        .order_by('-updated_at')
    )


async def build_role_library_snapshot_response(snapshot: RoleLibrarySnapshot) -> dict:
    entry_count = await sync_to_async(snapshot.entries.count)()
    summary = dict(snapshot.summary or {})
    return {
        'uuid': snapshot.uuid,
        'provider': snapshot.provider,
        'status': snapshot.status,
        'base_urls': snapshot.base_urls,
        'discovery_payload': snapshot.discovery_payload,
        'summary': summary,
        'canonical_family_counts': summary.get('canonical_family_counts', {}),
        'normalized_skill_count': int(summary.get('normalized_skill_count') or 0),
        'alias_count': int(summary.get('alias_count') or 0),
        'seed_urls_used': summary.get('seed_urls_used', []),
        'seed_manifest_version': summary.get('seed_manifest_version', ''),
        'quality_flags': summary.get('quality_flags', []),
        'missing_role_families': summary.get('missing_role_families', []),
        'error_message': snapshot.error_message,
        'entry_count': entry_count,
        'created_at': snapshot.created_at,
        'updated_at': snapshot.updated_at,
    }


def _resolve_workspace_latest_uuids(workspace_id, planning_context_pk=None) -> tuple:
    """Resolve latest/default run UUID markers for a workspace.

    Callers that need to build responses for multiple runs in a single request
    should call this once and pass the result via the ``latest_uuids`` kwarg to
    ``build_blueprint_response`` to avoid redundant queries.
    """
    planning_context_filter = (
        {'planning_context_id': planning_context_pk}
        if planning_context_pk is not None
        else {'planning_context__isnull': True}
    )
    latest_uuid = (
        SkillBlueprintRun.objects.filter(workspace_id=workspace_id, **planning_context_filter)
        .order_by('-updated_at')
        .values_list('uuid', flat=True)
        .first()
    )
    latest_review_ready_uuid = (
        SkillBlueprintRun.objects.filter(
            workspace_id=workspace_id,
            status__in=BLUEPRINT_REVIEW_READY_STATUSES,
            **planning_context_filter,
        )
        .order_by('-updated_at')
        .values_list('uuid', flat=True)
        .first()
    )
    latest_approved_uuid = (
        SkillBlueprintRun.objects.filter(
            workspace_id=workspace_id,
            status=BlueprintStatus.APPROVED,
            **planning_context_filter,
        )
        .order_by('-updated_at')
        .values_list('uuid', flat=True)
        .first()
    )
    latest_published_uuid = (
        SkillBlueprintRun.objects.filter(
            workspace_id=workspace_id,
            is_published=True,
            **planning_context_filter,
        )
        .order_by('-published_at', '-updated_at')
        .values_list('uuid', flat=True)
        .first()
    )
    return (
        latest_uuid,
        latest_review_ready_uuid,
        latest_approved_uuid,
        latest_published_uuid,
        latest_published_uuid or latest_review_ready_uuid,
    )


async def build_blueprint_response(
    run: SkillBlueprintRun,
    *,
    latest_uuids: tuple | None = None,
) -> dict:
    if latest_uuids is not None:
        latest_uuid, latest_review_ready_uuid, latest_approved_uuid, latest_published_uuid, default_uuid = latest_uuids
    else:
        latest_uuid, latest_review_ready_uuid, latest_approved_uuid, latest_published_uuid, default_uuid = await sync_to_async(
            _resolve_workspace_latest_uuids
        )(run.workspace_id, run.planning_context_id)
    try:
        clarification_cycle = await sync_to_async(lambda: run.clarification_cycle)()
    except ClarificationCycle.DoesNotExist:
        clarification_cycle = None
    if clarification_cycle is None:
        clarification_cycle_payload = await sync_to_async(
            lambda: ClarificationCycle.objects.filter(blueprint_run=run)
            .values('uuid', 'status', 'summary')
            .first()
        )()
    else:
        clarification_cycle_payload = {
            'uuid': clarification_cycle.uuid,
            'status': clarification_cycle.status,
            'summary': dict(clarification_cycle.summary or {}),
        }
    review_summary = dict(run.review_summary or {})
    if not review_summary:
        review_summary = _build_blueprint_review_summary(
            roadmap_context=list(run.roadmap_context or []),
            role_candidates=list(run.role_candidates or []),
            clarification_questions=list(run.clarification_questions or []),
            required_skill_set=list(run.required_skill_set or []),
            employee_matches=list(run.employee_role_matches or []),
        )
    return {
        'uuid': run.uuid,
        'title': run.title,
        'status': run.status,
        'role_library_snapshot_uuid': run.role_library_snapshot_id,
        'derived_from_run_uuid': run.derived_from_run_id,
        'roadmap_analysis_uuid': run.roadmap_analysis_id,
        'planning_context_uuid': run.planning_context_id,
        'generation_mode': run.generation_mode,
        'source_summary': run.source_summary,
        'input_snapshot': run.input_snapshot,
        'company_context': run.company_context,
        'roadmap_context': run.roadmap_context,
        'role_candidates': run.role_candidates,
        'clarification_questions': run.clarification_questions,
        'employee_role_matches': run.employee_role_matches,
        'required_skill_set': run.required_skill_set,
        'automation_candidates': run.automation_candidates,
        'occupation_map': run.occupation_map,
        'gap_summary': run.gap_summary,
        'redundancy_summary': run.redundancy_summary,
        'assessment_plan': run.assessment_plan,
        'review_summary': review_summary,
        'change_log': run.change_log,
        'reviewed_by': run.reviewed_by,
        'review_notes': run.review_notes,
        'reviewed_at': run.reviewed_at,
        'approved_by': run.approved_by,
        'approval_notes': run.approval_notes,
        'approved_at': run.approved_at,
        'is_published': bool(run.is_published),
        'published_by': run.published_by,
        'published_notes': run.published_notes,
        'published_at': run.published_at,
        'clarification_cycle_uuid': clarification_cycle_payload['uuid'] if clarification_cycle_payload else None,
        'clarification_cycle_status': (clarification_cycle_payload or {}).get('status', ''),
        'clarification_cycle_summary': (clarification_cycle_payload or {}).get('summary', {}),
        'approval_blocked': bool((review_summary.get('clarification_summary') or {}).get('open', 0)),
        'latest_for_workspace': run.uuid == latest_uuid,
        'latest_review_ready_for_workspace': run.uuid == latest_review_ready_uuid,
        'latest_approved_for_workspace': run.uuid == latest_approved_uuid,
        'latest_published_for_workspace': run.uuid == latest_published_uuid,
        'default_for_workspace': run.uuid == default_uuid,
        'created_at': run.created_at,
        'updated_at': run.updated_at,
    }


def build_clarification_question_response(question: ClarificationQuestion) -> dict[str, Any]:
    return {
        'uuid': question.uuid,
        'cycle_uuid': question.cycle_id,
        'blueprint_uuid': question.blueprint_run_id,
        'question_key': question.question_key,
        'question_text': question.question_text,
        'scope': question.scope,
        'priority': question.priority,
        'intended_respondent_type': question.intended_respondent_type,
        'rationale': question.rationale,
        'evidence_refs': list(question.evidence_refs or []),
        'impacted_roles': list(question.impacted_roles or []),
        'impacted_initiatives': list(question.impacted_initiatives or []),
        'status': question.status,
        'answer_text': question.answer_text,
        'answered_by': question.answered_by,
        'answered_at': question.answered_at,
        'status_note': question.status_note,
        'changed_target_model': bool(question.changed_target_model),
        'effect_metadata': dict(question.effect_metadata or {}),
        'created_at': question.created_at,
        'updated_at': question.updated_at,
    }


async def build_clarification_cycle_response(cycle: ClarificationCycle) -> dict[str, Any]:
    questions = await sync_to_async(list)(
        cycle.questions.order_by('created_at', 'question_key')
    )
    return {
        'uuid': cycle.uuid,
        'blueprint_uuid': cycle.blueprint_run_id,
        'title': cycle.title,
        'status': cycle.status,
        'summary': dict(cycle.summary or {}),
        'questions': [build_clarification_question_response(question) for question in questions],
        'created_at': cycle.created_at,
        'updated_at': cycle.updated_at,
    }


def _assert_blueprint_run_mutable(run: SkillBlueprintRun, *, action: str) -> None:
    if run.is_published or run.status not in _MUTABLE_BLUEPRINT_STATUSES:
        raise ValueError(
            f'Approved or published blueprints are immutable. Start a revision before you {action}.'
        )


async def patch_blueprint_run(
    base_run: SkillBlueprintRun,
    *,
    patch_payload: dict[str, Any],
    skip_employee_matching: bool = False,
    allow_published_base: bool = False,
) -> SkillBlueprintRun:
    if base_run.is_published and not allow_published_base:
        raise ValueError(
            'Published blueprints are immutable. Start a revision instead of patching them directly.'
        )
    workspace = await sync_to_async(IntakeWorkspace.objects.get)(pk=base_run.workspace_id)
    role_library_snapshot = None
    if base_run.role_library_snapshot_id:
        role_library_snapshot = await sync_to_async(
            lambda: RoleLibrarySnapshot.objects.filter(pk=base_run.role_library_snapshot_id).first()
        )()
    new_title = str(patch_payload.get('title') or base_run.title).strip() or base_run.title
    patch_reason = str(patch_payload.get('patch_reason') or '').strip()
    operator_name = str(patch_payload.get('operator_name') or '').strip() or 'operator'
    review_notes = str(patch_payload.get('review_notes') or '').strip()

    run = await sync_to_async(SkillBlueprintRun.objects.create)(
        workspace=workspace,
        planning_context=base_run.planning_context,
        title=new_title,
        status=BlueprintStatus.RUNNING,
        role_library_snapshot=role_library_snapshot,
        derived_from_run=base_run,
        roadmap_analysis_id=base_run.roadmap_analysis_id,
        generation_mode='patch',
        source_summary=deepcopy(base_run.source_summary or {}),
        input_snapshot={
            **deepcopy(base_run.input_snapshot or {}),
            'generation_mode': 'patch',
            'derived_from_run_uuid': str(base_run.uuid),
        },
        change_log=[
            *(base_run.change_log or []),
            {
                'event': 'patch',
                'at': _utc_now_iso(),
                'actor': operator_name,
                'note': patch_reason or 'Blueprint patched manually.',
            },
        ],
        review_notes=review_notes,
    )

    try:
        merged_payload = await sync_to_async(_build_patched_blueprint_payload_sync)(
            base_run.pk,
            patch_payload,
        )
        normalized_payload = await sync_to_async(_normalize_blueprint_payload_sync)(
            workspace.pk,
            merged_payload,
        )
        normalized = await sync_to_async(_persist_blueprint_payload_sync)(run.pk, normalized_payload)
        if skip_employee_matching:
            # Carry forward employee matches from the base run into the new
            # run-scoped persisted projection instead of re-running LLM
            # matching.
            employee_matches = await sync_to_async(_clone_employee_role_matches_sync)(
                workspace.pk,
                str(run.uuid),
                list(base_run.employee_role_matches or []),
            )
        else:
            employee_matches = await match_employees_to_roles(
                workspace,
                normalized_payload['role_candidates'],
                blueprint_run_uuid=run.uuid,
                planning_context=base_run.planning_context,
            )
        gap_summary, redundancy_summary = await sync_to_async(_compute_role_gap_summaries_sync)(
            workspace.pk,
            normalized_payload['role_candidates'],
            run.uuid,
        )
        coverage_analysis = await sync_to_async(_compute_coverage_analysis_sync)(
            workspace.pk,
            str(run.uuid),
            str(base_run.roadmap_analysis_id) if base_run.roadmap_analysis_id else None,
        )
        normalized_payload, gap_summary = _merge_coverage_analysis_into_payload(
            normalized_payload,
            gap_summary,
            coverage_analysis,
        )
        await sync_to_async(_finalize_blueprint_run_sync)(
            run.pk,
            normalized_payload,
            normalized['required_skill_set'],
            employee_matches,
            gap_summary,
            redundancy_summary,
        )
    except Exception as exc:
        logger.exception('Blueprint patch failed for workspace %s', workspace.slug)
        await sync_to_async(_fail_blueprint_run_sync)(run.pk, str(exc))

    return await sync_to_async(SkillBlueprintRun.objects.get)(pk=run.pk)


async def start_blueprint_revision(
    base_run: SkillBlueprintRun,
    *,
    operator_name: str,
    revision_reason: str,
    skip_employee_matching: bool = True,
) -> SkillBlueprintRun:
    return await patch_blueprint_run(
        base_run,
        patch_payload={
            'patch_reason': revision_reason or 'Start a new revision from the selected blueprint.',
            'operator_name': operator_name,
        },
        skip_employee_matching=skip_employee_matching,
        allow_published_base=True,
    )


async def review_blueprint_run(
    run: SkillBlueprintRun,
    *,
    reviewer_name: str,
    review_notes: str,
    clarification_updates: list[dict[str, Any]],
) -> SkillBlueprintRun:
    await sync_to_async(_assert_blueprint_run_mutable)(run, action='review it')
    await sync_to_async(_apply_blueprint_review_sync)(
        run.pk,
        reviewer_name=reviewer_name,
        review_notes=review_notes,
        clarification_updates=clarification_updates,
        approve=False,
    )
    return await sync_to_async(SkillBlueprintRun.objects.get)(pk=run.pk)


async def approve_blueprint_run(
    run: SkillBlueprintRun,
    *,
    approver_name: str,
    approval_notes: str,
    clarification_updates: list[dict[str, Any]],
) -> SkillBlueprintRun:
    await sync_to_async(_assert_blueprint_run_mutable)(run, action='approve it')
    await sync_to_async(_apply_blueprint_review_sync)(
        run.pk,
        reviewer_name=approver_name,
        review_notes=approval_notes,
        clarification_updates=clarification_updates,
        approve=True,
    )
    return await sync_to_async(SkillBlueprintRun.objects.get)(pk=run.pk)


async def publish_blueprint_run(
    run: SkillBlueprintRun,
    *,
    publisher_name: str,
    publish_notes: str,
) -> SkillBlueprintRun:
    await sync_to_async(_publish_blueprint_run_sync)(
        run.pk,
        publisher_name=publisher_name,
        publish_notes=publish_notes,
    )
    return await sync_to_async(SkillBlueprintRun.objects.get)(pk=run.pk)


async def answer_blueprint_clarifications(
    run: SkillBlueprintRun,
    *,
    operator_name: str,
    answer_items: list[dict[str, Any]],
) -> SkillBlueprintRun:
    await sync_to_async(_assert_blueprint_run_mutable)(run, action='answer clarification questions')
    await sync_to_async(_apply_clarification_answers_sync)(
        run.pk,
        operator_name=operator_name,
        answer_items=answer_items,
    )
    return await sync_to_async(SkillBlueprintRun.objects.get)(pk=run.pk)


async def refresh_blueprint_from_clarifications(
    base_run: SkillBlueprintRun,
    *,
    operator_name: str,
    refresh_note: str,
    skip_employee_matching: bool = False,
) -> SkillBlueprintRun:
    workspace = await sync_to_async(IntakeWorkspace.objects.get)(pk=base_run.workspace_id)
    role_library_snapshot = None
    if base_run.role_library_snapshot_id:
        role_library_snapshot = await sync_to_async(
            lambda: RoleLibrarySnapshot.objects.filter(pk=base_run.role_library_snapshot_id).first()
        )()
    if role_library_snapshot is None:
        role_library_snapshot = await get_latest_role_library_snapshot(workspace)
        if role_library_snapshot is None:
            role_library_snapshot = await sync_role_library_for_workspace(workspace)

    answered_clarifications = await sync_to_async(_load_answered_clarifications_sync)(base_run.pk)
    if not answered_clarifications:
        raise ValueError('At least one answered clarification is required before refreshing the blueprint.')

    run = await sync_to_async(SkillBlueprintRun.objects.create)(
        workspace=workspace,
        planning_context=base_run.planning_context,
        title=base_run.title,
        status=BlueprintStatus.RUNNING,
        role_library_snapshot=role_library_snapshot,
        derived_from_run=base_run,
        roadmap_analysis_id=base_run.roadmap_analysis_id,
        generation_mode='clarification_refresh',
        source_summary=deepcopy(base_run.source_summary or {}),
        change_log=[
            *(base_run.change_log or []),
            {
                'event': 'clarification_refresh',
                'at': _utc_now_iso(),
                'actor': operator_name or 'operator',
                'note': refresh_note or 'Blueprint refreshed from answered clarifications.',
            },
        ],
    )

    try:
        blueprint_inputs = await sync_to_async(_build_blueprint_inputs_sync)(
            workspace.pk,
            role_library_snapshot.pk,
            planning_context_pk=base_run.planning_context_id,
        )
        if base_run.roadmap_analysis_id:
            inherited_roadmap = await sync_to_async(
                lambda: RoadmapAnalysisRun.objects.filter(pk=base_run.roadmap_analysis_id).first()
            )()
            if inherited_roadmap is not None:
                blueprint_inputs = {
                    **blueprint_inputs,
                    'roadmap_input': _build_structured_roadmap_input(inherited_roadmap),
                    'roadmap_analysis_uuid': str(inherited_roadmap.uuid),
                    'roadmap_analysis_pk': inherited_roadmap.pk,
                    'roadmap_input_mode': 'structured',
                }
        input_snapshot = _build_input_snapshot(
            role_library_snapshot=role_library_snapshot,
            blueprint_inputs=blueprint_inputs,
            generation_mode='clarification_refresh',
            derived_from_run=base_run,
        )
        input_snapshot['clarification_refresh'] = {
            'answered_question_count': len(answered_clarifications),
            'question_keys': [item.get('clarification_id', '') for item in answered_clarifications if item.get('clarification_id')],
            'operator_name': operator_name,
            'refresh_note': refresh_note,
        }
        await sync_to_async(_record_blueprint_run_inputs_sync)(
            run.pk,
            blueprint_inputs['source_summary'],
            input_snapshot,
        )
        llm_payload = await _refresh_blueprint_from_clarifications_with_llm(
            workspace,
            base_run,
            blueprint_inputs=blueprint_inputs,
            answered_clarifications=answered_clarifications,
            refresh_note=refresh_note,
        )
        normalized_payload = await sync_to_async(_normalize_blueprint_payload_sync)(
            workspace.pk,
            llm_payload,
            workspace_profile_snapshot=blueprint_inputs['workspace_profile'],
        )
        normalized = await sync_to_async(_persist_blueprint_payload_sync)(run.pk, normalized_payload)
        if skip_employee_matching:
            employee_matches = await sync_to_async(_clone_employee_role_matches_sync)(
                workspace.pk,
                str(run.uuid),
                list(base_run.employee_role_matches or []),
            )
        else:
            employee_matches = await match_employees_to_roles(
                workspace,
                normalized_payload['role_candidates'],
                blueprint_run_uuid=run.uuid,
                planning_context=base_run.planning_context,
            )
        gap_summary, redundancy_summary = await sync_to_async(_compute_role_gap_summaries_sync)(
            workspace.pk,
            normalized_payload['role_candidates'],
            run.uuid,
        )
        coverage_analysis = await sync_to_async(_compute_coverage_analysis_sync)(
            workspace.pk,
            str(run.uuid),
            str(base_run.roadmap_analysis_id) if base_run.roadmap_analysis_id else None,
        )
        normalized_payload, gap_summary = _merge_coverage_analysis_into_payload(
            normalized_payload,
            gap_summary,
            coverage_analysis,
        )
        await sync_to_async(_finalize_blueprint_run_sync)(
            run.pk,
            normalized_payload,
            normalized['required_skill_set'],
            employee_matches,
            gap_summary,
            redundancy_summary,
        )
    except Exception as exc:
        logger.exception('Blueprint clarification refresh failed for workspace %s', workspace.slug)
        await sync_to_async(_fail_blueprint_run_sync)(run.pk, str(exc))

    return await sync_to_async(SkillBlueprintRun.objects.get)(pk=run.pk)


def normalize_url(url: str) -> str:
    stripped, _fragment = urldefrag(url or '')
    return stripped.rstrip('/') + '/' if stripped and not stripped.endswith('/') else stripped


def _normalize_clarification_status(value: Any) -> str:
    lowered = str(value or '').strip().lower()
    if lowered in {'accepted', 'resolve', 'resolved'}:
        return ClarificationQuestionStatus.ACCEPTED
    if lowered in {'obsolete', 'waived', 'waive', 'not_applicable', 'not-applicable', 'n/a'}:
        return ClarificationQuestionStatus.OBSOLETE
    if lowered in {ClarificationQuestionStatus.ANSWERED, ClarificationQuestionStatus.REJECTED}:
        return lowered
    if lowered == ClarificationQuestionStatus.OPEN:
        return ClarificationQuestionStatus.OPEN
    return ClarificationQuestionStatus.OPEN


def _is_closed_clarification_status(value: Any) -> bool:
    return _normalize_clarification_status(value) in _CLOSED_CLARIFICATION_STATUSES


def _normalize_evidence_refs(value: Any) -> list:
    if not isinstance(value, list):
        return []
    normalized: list = []
    for item in value:
        if isinstance(item, (dict, str, int, float, bool)):
            normalized.append(item)
    return normalized


def _parse_iso_datetime(value: Any) -> Optional[datetime]:
    text = str(value or '').strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace('Z', '+00:00'))
    except ValueError:
        return None


def _flatten_query_terms(values) -> str:
    terms: list[str] = []
    for value in values:
        if isinstance(value, list):
            for item in value:
                text = str(item or '').strip()
                if text:
                    terms.append(text)
        else:
            text = str(value or '').strip()
            if text:
                terms.append(text)
    return ' '.join(terms)[:600]


def _infer_existing_role_families_sync(workspace: IntakeWorkspace) -> set[str]:
    families: set[str] = set()
    titles = Employee.objects.filter(workspace=workspace).exclude(current_title='').values_list('current_title', flat=True)
    for title in titles:
        normalized = normalize_external_role_title(role_name=title)
        if normalized.get('canonical_family'):
            families.add(normalized['canonical_family'])
    return families


def _normalize_company_context_payload(company_context: dict[str, Any], workspace_profile: dict[str, Any]) -> dict[str, Any]:
    profile = workspace_profile.get('company_profile', {})
    return {
        'company_name': str(company_context.get('company_name') or profile.get('company_name') or '').strip(),
        'what_company_does': str(
            company_context.get('what_company_does')
            or profile.get('company_description')
            or ''
        ).strip(),
        'why_skills_improvement_now': str(
            company_context.get('why_skills_improvement_now')
            or profile.get('pilot_scope_notes')
            or profile.get('notable_constraints_or_growth_plans')
            or ''
        ).strip(),
        'products': _dedupe_strings(company_context.get('products') or profile.get('main_products', [])),
        'customers': _dedupe_strings(company_context.get('customers') or profile.get('target_customers', [])),
        'markets': _dedupe_strings(
            company_context.get('markets') or [profile.get('primary_market_geography', '')]
        ),
        'locations': _dedupe_strings(company_context.get('locations') or profile.get('locations', [])),
        'current_tech_stack': _dedupe_strings(
            company_context.get('current_tech_stack') or profile.get('current_tech_stack', [])
        ),
        'planned_tech_stack': _dedupe_strings(
            company_context.get('planned_tech_stack') or profile.get('planned_tech_stack', [])
        ),
        'missing_information': _dedupe_strings(company_context.get('missing_information') or []),
    }


def _normalize_roadmap_context_payload(items: list[Any]) -> list[dict[str, Any]]:
    normalized_items: list[dict[str, Any]] = []
    for index, item in enumerate(items or [], start=1):
        if not isinstance(item, dict):
            continue
        title = str(item.get('title') or item.get('initiative') or '').strip() or f'Initiative {index}'
        category = str(item.get('category') or 'roadmap').strip()
        time_horizon = str(item.get('time_horizon') or item.get('timing') or '').strip()
        desired_market_outcome = str(item.get('desired_market_outcome') or item.get('expected_outcome') or '').strip()
        initiative_id = slugify_key(item.get('initiative_id') or title or f'initiative-{index}')
        confidence = _coerce_confidence(item.get('confidence'), default=0.65)
        criticality = _normalize_criticality(item.get('criticality', ''), priority=4 if confidence >= 0.75 else 2)
        normalized_items.append(
            {
                'initiative_id': initiative_id,
                'title': title,
                'category': category,
                'summary': str(item.get('summary') or '').strip(),
                'time_horizon': time_horizon,
                'desired_market_outcome': desired_market_outcome,
                'target_customer_segments': _dedupe_strings(item.get('target_customer_segments') or []),
                'tech_stack': _dedupe_strings(item.get('tech_stack') or []),
                'success_metrics': _dedupe_strings(item.get('success_metrics') or []),
                'product_implications': _dedupe_strings(item.get('product_implications') or []),
                'market_implications': _dedupe_strings(item.get('market_implications') or []),
                'functions_required': _dedupe_strings(item.get('functions_required') or []),
                'confidence': confidence,
                'ambiguities': _dedupe_strings(item.get('ambiguities') or []),
                'criticality': criticality,
            }
        )
    return normalized_items


def _normalize_skill_requirement_payload(
    skill_payload: Any,
    *,
    related_initiatives: list[str],
) -> Optional[dict[str, Any]]:
    if isinstance(skill_payload, str):
        skill_payload = {'skill_name_en': skill_payload}
    if not isinstance(skill_payload, dict):
        return None

    raw_skill_name = (
        skill_payload.get('skill_name_en')
        or skill_payload.get('skill_name_ru')
        or skill_payload.get('skill_name')
        or ''
    )
    normalized_skill = normalize_skill_seed(raw_skill_name)
    if not normalized_skill.get('display_name_en'):
        return None

    priority = _coerce_int(skill_payload.get('priority'), default=3, minimum=1, maximum=5)
    target_level = _coerce_int(skill_payload.get('target_level'), default=2, minimum=1, maximum=5)
    requirement_type = _normalize_requirement_type(skill_payload.get('requirement_type', 'core'))
    supported_initiatives = _dedupe_strings(
        skill_payload.get('supported_initiatives') or related_initiatives
    )
    return {
        'skill_name_en': normalized_skill['display_name_en'],
        'skill_name_ru': skill_payload.get('skill_name_ru', '') or normalized_skill.get('display_name_ru', ''),
        'target_level': target_level,
        'priority': priority,
        'reason': str(skill_payload.get('reason') or '').strip(),
        'requirement_type': requirement_type,
        'criticality': _normalize_criticality(skill_payload.get('criticality', ''), priority=priority),
        'supported_initiatives': supported_initiatives,
        'confidence': _coerce_confidence(skill_payload.get('confidence'), default=0.65),
    }


def _merge_skill_requirement_entry(base: dict[str, Any], incoming: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    merged['target_level'] = max(
        _coerce_int(base.get('target_level'), default=1, minimum=1, maximum=5),
        _coerce_int(incoming.get('target_level'), default=1, minimum=1, maximum=5),
    )
    merged['priority'] = max(
        _coerce_int(base.get('priority'), default=1, minimum=1, maximum=5),
        _coerce_int(incoming.get('priority'), default=1, minimum=1, maximum=5),
    )
    merged['reason'] = _merge_reason_text([base.get('reason', ''), incoming.get('reason', '')])
    merged['requirement_type'] = _merge_requirement_type(
        base.get('requirement_type', 'core'),
        incoming.get('requirement_type', 'core'),
    )
    merged['criticality'] = _normalize_criticality(
        incoming.get('criticality') or base.get('criticality', ''),
        priority=merged['priority'],
    )
    merged['supported_initiatives'] = _dedupe_strings(
        [*base.get('supported_initiatives', []), *incoming.get('supported_initiatives', [])]
    )
    merged['confidence'] = max(
        _coerce_confidence(base.get('confidence'), default=0.6),
        _coerce_confidence(incoming.get('confidence'), default=0.6),
    )
    return merged


def _normalize_role_candidates_payload(
    role_candidates: list[Any],
    *,
    existing_role_families: set[str],
    roadmap_context: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    initiative_ids = [item['initiative_id'] for item in roadmap_context if item.get('initiative_id')]
    buckets: dict[tuple[str, str], dict[str, Any]] = {}

    for item in role_candidates or []:
        if not isinstance(item, dict):
            continue
        ambiguity_notes = _dedupe_strings(item.get('ambiguity_notes') or [])
        normalized_role = normalize_external_role_title(
            role_name=item.get('role_name', ''),
            role_family_hint=item.get('canonical_role_family', '') or item.get('role_family', ''),
        )
        canonical_family = normalized_role['canonical_family']
        seniority = _normalize_role_seniority(
            item.get('role_name', ''),
            [item.get('seniority', ''), *ambiguity_notes],
        )
        key = (canonical_family, seniority)
        related_initiatives = _dedupe_strings(item.get('related_initiatives') or initiative_ids[:3])
        existing_role = bool(item.get('role_already_exists_internally'))
        if 'role_already_exists_internally' not in item:
            existing_role = canonical_family in existing_role_families
        likely_requires_hiring = bool(item.get('likely_requires_hiring'))
        if 'likely_requires_hiring' not in item:
            likely_requires_hiring = not existing_role and _coerce_int(item.get('headcount_needed'), default=1, minimum=0) > 0

        bucket = buckets.setdefault(
            key,
            {
                'role_name': str(item.get('role_name') or normalized_role['canonical_label']).strip(),
                'role_key': slugify_key(f'{canonical_family}-{seniority}'),
                'canonical_role_family': canonical_family,
                'role_family': canonical_family,
                'seniority': seniority,
                'headcount_needed': 0,
                'related_initiatives': [],
                'rationale': '',
                'responsibilities': [],
                'skills': [],
                'role_already_exists_internally': existing_role,
                'likely_requires_hiring': likely_requires_hiring,
                'confidence': 0.0,
                'ambiguity_notes': [],
            },
        )

        bucket['headcount_needed'] = max(
            bucket['headcount_needed'],
            _coerce_int(item.get('headcount_needed'), default=1, minimum=0, maximum=50),
        )
        bucket['related_initiatives'] = _dedupe_strings(
            [*bucket['related_initiatives'], *related_initiatives]
        )
        bucket['rationale'] = _merge_reason_text([bucket.get('rationale', ''), item.get('rationale', '')])
        bucket['responsibilities'] = _dedupe_strings(
            [*bucket['responsibilities'], *(item.get('responsibilities') or [])]
        )
        bucket['role_already_exists_internally'] = bucket['role_already_exists_internally'] or existing_role
        bucket['likely_requires_hiring'] = bucket['likely_requires_hiring'] or likely_requires_hiring
        bucket['confidence'] = max(
            _coerce_confidence(bucket.get('confidence'), default=0.0),
            _coerce_confidence(item.get('confidence'), default=0.7),
        )
        bucket['ambiguity_notes'] = _dedupe_strings(
            [*bucket['ambiguity_notes'], *ambiguity_notes]
        )

        merged_skills: dict[str, dict[str, Any]] = {
            normalize_skill_seed(skill.get('skill_name_en') or skill.get('skill_name_ru') or '').get('canonical_key', ''): dict(skill)
            for skill in bucket['skills']
            if isinstance(skill, dict)
        }
        for raw_skill in item.get('skills', []):
            normalized_skill = _normalize_skill_requirement_payload(
                raw_skill,
                related_initiatives=related_initiatives,
            )
            if normalized_skill is None:
                continue
            skill_key = normalize_skill_seed(normalized_skill['skill_name_en'])['canonical_key']
            existing_skill = merged_skills.get(skill_key)
            if existing_skill is None:
                merged_skills[skill_key] = normalized_skill
            else:
                merged_skills[skill_key] = _merge_skill_requirement_entry(existing_skill, normalized_skill)
        bucket['skills'] = sorted(
            merged_skills.values(),
            key=lambda item: (-int(item.get('priority', 0) or 0), -int(item.get('target_level', 0) or 0), item.get('skill_name_en', '')),
        )

    return sorted(
        buckets.values(),
        key=lambda item: (-item['headcount_needed'], -item['confidence'], item['canonical_role_family'], item['seniority']),
    )


def _normalize_clarification_items(
    clarification_items: list[Any],
    *,
    role_candidates: list[dict[str, Any]],
    roadmap_context: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    normalized_items: list[dict[str, Any]] = []
    for index, item in enumerate(clarification_items or [], start=1):
        if not isinstance(item, dict):
            continue
        question = str(item.get('question') or '').strip()
        if not question:
            continue
        clarification_id = str(item.get('id') or f'{slugify_key(question)}-{index}')
        normalized_items.append(
            {
                'id': clarification_id,
                'question_uuid': str(item.get('question_uuid') or '').strip(),
                'question': question,
                'scope': str(item.get('scope') or 'blueprint').strip() or 'blueprint',
                'priority': str(item.get('priority') or 'medium').strip().lower() or 'medium',
                'why_it_matters': str(item.get('why_it_matters') or '').strip(),
                'intended_respondent_type': str(item.get('intended_respondent_type') or 'operator').strip() or 'operator',
                'evidence_refs': _normalize_evidence_refs(item.get('evidence_refs')),
                'impacted_roles': _dedupe_strings(item.get('impacted_roles') or []),
                'impacted_initiatives': _dedupe_strings(item.get('impacted_initiatives') or []),
                'status': _normalize_clarification_status(item.get('status') or 'open'),
                'answer': str(item.get('answer') or '').strip(),
                'note': str(item.get('note') or '').strip(),
                'changed_target_model': bool(item.get('changed_target_model')),
                'answered_by': str(item.get('answered_by') or '').strip(),
                'answered_at': str(item.get('answered_at') or '').strip(),
            }
        )

    if not roadmap_context:
        normalized_items.append(
            {
                'id': 'missing-roadmap-context',
                'question': 'Which roadmap initiatives are highest priority for the pilot window?',
                'scope': 'roadmap',
                'priority': 'high',
                'why_it_matters': 'Target roles and skills are only meaningful when tied to specific initiatives.',
                'intended_respondent_type': 'operator',
                'evidence_refs': [],
                'impacted_roles': [],
                'impacted_initiatives': [],
                'status': ClarificationQuestionStatus.OPEN,
                'answer': '',
                'note': '',
                'changed_target_model': False,
                'answered_by': '',
                'answered_at': '',
            }
        )

    if not role_candidates:
        normalized_items.append(
            {
                'id': 'missing-role-candidates',
                'question': 'Which minimal role set is actually required to execute the roadmap?',
                'scope': 'roles',
                'priority': 'high',
                'why_it_matters': 'Later stages depend on reviewed target roles and their required skills.',
                'intended_respondent_type': 'operator',
                'evidence_refs': [],
                'impacted_roles': [],
                'impacted_initiatives': [item['initiative_id'] for item in roadmap_context[:3]],
                'status': ClarificationQuestionStatus.OPEN,
                'answer': '',
                'note': '',
                'changed_target_model': False,
                'answered_by': '',
                'answered_at': '',
            }
        )

    return normalized_items


def _normalize_assessment_plan_payload(assessment_plan: dict[str, Any], clarification_items: list[dict[str, Any]]) -> dict[str, Any]:
    themes = _dedupe_strings(assessment_plan.get('question_themes') or [])
    if not themes:
        themes = [
            'Hidden strengths and adjacent skills',
            'Confidence in critical roadmap skills',
            'Aspirations and willingness to stretch into adjacent roles',
        ]
    unresolved_count = len([item for item in clarification_items if not _is_closed_clarification_status(item.get('status'))])
    return {
        'global_notes': str(assessment_plan.get('global_notes') or '').strip(),
        'question_themes': themes,
        'per_employee_question_count': _coerce_int(
            assessment_plan.get('per_employee_question_count'),
            default=8 if unresolved_count else 6,
            minimum=4,
            maximum=12,
        ),
    }


def _normalize_occupation_map_payload(items: list[Any]) -> list[dict[str, Any]]:
    normalized_items: list[dict[str, Any]] = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        role_name = str(item.get('role_name') or '').strip()
        reference_role = str(item.get('reference_role') or '').strip()
        if not role_name or not reference_role:
            continue
        normalized_items.append(
            {
                'role_name': role_name,
                'reference_role': reference_role,
                'reference_url': str(item.get('reference_url') or '').strip(),
                'match_reason': str(item.get('match_reason') or '').strip(),
                'match_score': _coerce_int(item.get('match_score'), default=80, minimum=0, maximum=100),
            }
        )
    return normalized_items


def _normalize_blueprint_payload_sync(
    workspace_pk,
    llm_payload: dict[str, Any],
    *,
    workspace_profile_snapshot: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    workspace = IntakeWorkspace.objects.get(pk=workspace_pk)
    workspace_profile = workspace_profile_snapshot or build_workspace_profile_snapshot(workspace)
    existing_role_families = _infer_existing_role_families_sync(workspace)

    company_context = _normalize_company_context_payload(
        dict(llm_payload.get('company_context') or {}),
        workspace_profile,
    )
    roadmap_context = _normalize_roadmap_context_payload(llm_payload.get('roadmap_context') or [])
    role_candidates = _normalize_role_candidates_payload(
        llm_payload.get('role_candidates') or [],
        existing_role_families=existing_role_families,
        roadmap_context=roadmap_context,
    )
    clarification_questions = _normalize_clarification_items(
        llm_payload.get('clarification_questions') or [],
        role_candidates=role_candidates,
        roadmap_context=roadmap_context,
    )
    automation_candidates = [
        {
            'activity': str(item.get('activity') or '').strip(),
            'reason': str(item.get('reason') or '').strip(),
            'affected_roles': _dedupe_strings(item.get('affected_roles') or []),
        }
        for item in (llm_payload.get('automation_candidates') or [])
        if isinstance(item, dict) and str(item.get('activity') or '').strip()
    ]

    return {
        'company_context': company_context,
        'roadmap_context': roadmap_context,
        'role_candidates': role_candidates,
        'clarification_questions': clarification_questions,
        'automation_candidates': automation_candidates,
        'occupation_map': _normalize_occupation_map_payload(llm_payload.get('occupation_map') or []),
        'assessment_plan': _normalize_assessment_plan_payload(
            dict(llm_payload.get('assessment_plan') or {}),
            clarification_questions,
        ),
    }


def _build_clarification_summary(clarification_questions: list[dict[str, Any]]) -> dict[str, int]:
    total = len(clarification_questions)
    answered = len(
        [
            item for item in clarification_questions
            if _normalize_clarification_status(item.get('status')) == ClarificationQuestionStatus.ANSWERED
        ]
    )
    accepted = len(
        [
            item for item in clarification_questions
            if _normalize_clarification_status(item.get('status')) == ClarificationQuestionStatus.ACCEPTED
        ]
    )
    rejected = len(
        [
            item for item in clarification_questions
            if _normalize_clarification_status(item.get('status')) == ClarificationQuestionStatus.REJECTED
        ]
    )
    obsolete = len(
        [
            item for item in clarification_questions
            if _normalize_clarification_status(item.get('status')) == ClarificationQuestionStatus.OBSOLETE
        ]
    )
    open_count = total - accepted - obsolete
    return {
        'total': total,
        'open': max(open_count, 0),
        'answered': answered,
        'accepted': accepted,
        'rejected': rejected,
        'obsolete': obsolete,
        'resolved': accepted,
        'waived': obsolete,
    }


def _resolve_generated_blueprint_status(clarification_questions: list[dict[str, Any]]) -> str:
    return (
        BlueprintStatus.NEEDS_CLARIFICATION
        if _build_clarification_summary(clarification_questions)['open'] > 0
        else BlueprintStatus.DRAFT
    )


def _build_blueprint_review_summary(
    *,
    roadmap_context: list[dict[str, Any]],
    role_candidates: list[dict[str, Any]],
    clarification_questions: list[dict[str, Any]],
    required_skill_set: list[dict[str, Any]],
    employee_matches: list[dict[str, Any]],
) -> dict[str, Any]:
    clarification_summary = _build_clarification_summary(clarification_questions)
    unresolved_ambiguity_count = sum(len(item.get('ambiguities', [])) for item in roadmap_context) + sum(
        len(item.get('ambiguity_notes', [])) for item in role_candidates
    )
    return {
        'roadmap_initiative_count': len(roadmap_context),
        'role_candidate_count': len(role_candidates),
        'role_requirement_count': sum(len(item.get('skills', [])) for item in role_candidates),
        'required_skill_count': len(required_skill_set),
        'employee_match_count': len(employee_matches),
        'clarification_summary': clarification_summary,
        'unresolved_ambiguity_count': unresolved_ambiguity_count,
        'ready_for_review': clarification_summary['open'] == 0,
    }


def _build_input_snapshot(
    *,
    role_library_snapshot: Optional[RoleLibrarySnapshot],
    blueprint_inputs: dict[str, Any],
    generation_mode: str,
    derived_from_run: Optional[SkillBlueprintRun] = None,
) -> dict[str, Any]:
    return {
        'role_library_snapshot_uuid': str(role_library_snapshot.uuid) if role_library_snapshot is not None else '',
        'generation_mode': generation_mode,
        'derived_from_run_uuid': str(derived_from_run.uuid) if derived_from_run is not None else '',
        'planning_context_uuid': str(blueprint_inputs.get('planning_context_uuid') or ''),
        'source_counts': dict((blueprint_inputs.get('source_summary') or {}).get('counts_by_kind', {})),
        'retrieval_summary': dict((blueprint_inputs.get('source_summary') or {}).get('retrieval', {})),
        'role_library_summary': dict((blueprint_inputs.get('source_summary') or {}).get('role_library', {})),
        'roadmap_analysis_uuid': str(blueprint_inputs.get('roadmap_analysis_uuid') or ''),
        'roadmap_input_mode': str(blueprint_inputs.get('roadmap_input_mode') or ''),
        'roadmap_analysis_digest': str(blueprint_inputs.get('roadmap_input') or ''),
    }


def _merge_clarification_updates(
    clarification_questions: list[dict[str, Any]],
    updates: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    indexed_updates = {
        str(item.get('clarification_id') or '').strip(): item
        for item in updates
        if str(item.get('clarification_id') or '').strip()
    }
    indexed_updates_by_uuid = {
        str(item.get('question_uuid') or '').strip(): item
        for item in updates
        if str(item.get('question_uuid') or '').strip()
    }
    merged: list[dict[str, Any]] = []
    for item in clarification_questions:
        updated_item = dict(item)
        update = indexed_updates_by_uuid.get(str(updated_item.get('question_uuid') or '').strip()) or indexed_updates.get(updated_item.get('id', ''))
        if update:
            answer_text = str(update.get('answer_text') or update.get('answer') or '').strip()
            raw_status = update.get('status')
            if not raw_status and answer_text:
                raw_status = ClarificationQuestionStatus.ANSWERED
            updated_item['answer'] = answer_text
            updated_item['status'] = _normalize_clarification_status(raw_status or updated_item.get('status', ClarificationQuestionStatus.OPEN))
            note = str(update.get('status_note') or update.get('note') or '').strip()
            if note:
                updated_item['note'] = note
            if 'changed_target_model' in update:
                updated_item['changed_target_model'] = bool(update.get('changed_target_model'))
            actor = str(update.get('operator_name') or '').strip()
            if actor:
                updated_item['answered_by'] = actor
            if answer_text or actor or note or 'status' in update:
                updated_item['answered_at'] = _utc_now_iso()
        else:
            updated_item['status'] = _normalize_clarification_status(updated_item.get('status'))
        merged.append(updated_item)
    return merged


def _build_patched_blueprint_payload_sync(run_pk, patch_payload: dict[str, Any]) -> dict[str, Any]:
    run = SkillBlueprintRun.objects.get(pk=run_pk)
    merged_payload = {
        'company_context': deepcopy(run.company_context or {}),
        'roadmap_context': deepcopy(run.roadmap_context or []),
        'role_candidates': deepcopy(run.role_candidates or []),
        'clarification_questions': deepcopy(run.clarification_questions or []),
        'automation_candidates': deepcopy(run.automation_candidates or []),
        'occupation_map': deepcopy(run.occupation_map or []),
        'assessment_plan': deepcopy(run.assessment_plan or {}),
    }
    for key in [
        'company_context',
        'roadmap_context',
        'role_candidates',
        'clarification_questions',
        'automation_candidates',
        'occupation_map',
        'assessment_plan',
    ]:
        if key in patch_payload and patch_payload[key] is not None:
            merged_payload[key] = deepcopy(patch_payload[key])
    return merged_payload


def _build_blueprint_retrieval_queries(profile_snapshot: dict) -> dict[str, str]:
    company_profile = profile_snapshot.get('company_profile', {})
    pilot_scope = profile_snapshot.get('pilot_scope', {})
    context_terms = _flatten_query_terms(
        [
            company_profile.get('company_name'),
            company_profile.get('company_description'),
            company_profile.get('main_products', []),
            company_profile.get('target_customers', []),
            company_profile.get('primary_market_geography'),
            company_profile.get('locations', []),
            company_profile.get('current_tech_stack', []),
            company_profile.get('planned_tech_stack', []),
            company_profile.get('pilot_scope_notes'),
            company_profile.get('notable_constraints_or_growth_plans'),
            pilot_scope.get('departments_in_scope', []),
            pilot_scope.get('roles_in_scope', []),
            pilot_scope.get('products_in_scope', []),
            pilot_scope.get('analyst_notes'),
        ]
    )
    return {
        'roadmap': (
            f'{context_terms} roadmap initiatives sequencing milestones expected outcomes '
            'product changes technology changes cross functional workstreams'
        ).strip(),
        'strategy': (
            f'{context_terms} strategy goals growth priorities market customer segments '
            'commercial direction technology direction'
        ).strip(),
        'role_reference': (
            f'{context_terms} job descriptions role expectations responsibilities requirements '
            'skills seniority levels hiring needs'
        ).strip(),
    }


def _build_parsed_source_digest(
    parsed_sources: list[ParsedSource],
    *,
    source_kinds: list[str],
    max_chars: int = 12000,
) -> str:
    sections: list[str] = []
    for parsed in parsed_sources:
        if parsed.source.source_kind not in source_kinds:
            continue
        title = parsed.source.title or parsed.source.source_kind
        text = (parsed.extracted_text or '')[:3500].strip()
        if text:
            sections.append(f'[{parsed.source.source_kind}] {title}\n{text}')
    return '\n\n'.join(sections)[:max_chars]


def _build_combined_evidence_fallback_digest(
    *,
    roadmap_matches: list[dict],
    strategy_matches: list[dict],
    role_reference_matches: list[dict],
    roadmap_fallback: str,
    strategy_fallback: str,
    role_reference_fallback: str,
    max_chars: int = 18000,
) -> str:
    sections: list[str] = []
    if not roadmap_matches and roadmap_fallback:
        sections.append(roadmap_fallback)
    if not strategy_matches and strategy_fallback:
        sections.append(strategy_fallback)
    if not role_reference_matches and role_reference_fallback:
        sections.append(role_reference_fallback)
    return '\n\n'.join(section for section in sections if section)[:max_chars]


def _build_structured_roadmap_input(roadmap_analysis: RoadmapAnalysisRun, *, max_chars: int = 16000) -> str:
    lines: list[str] = []
    initiatives_by_id = {
        str(item.get('id') or ''): item
        for item in (roadmap_analysis.initiatives or [])
        if isinstance(item, dict)
    }

    if roadmap_analysis.initiatives:
        lines.append('### Strategic initiatives')
        for item in roadmap_analysis.initiatives:
            if not isinstance(item, dict):
                continue
            lines.append(
                f"- {item.get('name', '')} | criticality={item.get('criticality', 'medium')} | "
                f"window={item.get('planned_window', 'unspecified')} | goal={str(item.get('goal', ''))[:180]}"
            )

    if roadmap_analysis.workstreams:
        lines.append('\n### Delivery workstreams')
        for item in roadmap_analysis.workstreams:
            if not isinstance(item, dict):
                continue
            initiative = initiatives_by_id.get(str(item.get('initiative_id') or ''), {})
            capabilities = []
            for capability in (item.get('required_capabilities') or [])[:5]:
                if not isinstance(capability, dict):
                    continue
                capabilities.append(
                    f"{capability.get('capability', '')} ({capability.get('level', '')}, {capability.get('criticality', '')})"
                )
            lines.append(
                f"- {item.get('name', '')} | initiative={initiative.get('name', item.get('initiative_id', ''))}\n"
                f"  scope={str(item.get('scope', ''))[:200]}\n"
                f"  delivery_type={item.get('delivery_type', '')}\n"
                f"  affected_systems={', '.join(item.get('affected_systems', [])[:8])}\n"
                f"  roles_needed={', '.join((item.get('team_shape') or {}).get('roles_needed', [])[:6])}\n"
                f"  capabilities={', '.join(capabilities)}"
            )

    if roadmap_analysis.capability_bundles:
        lines.append('\n### Capability bundles')
        for item in roadmap_analysis.capability_bundles:
            if not isinstance(item, dict):
                continue
            lines.append(
                f"- {item.get('capability_name', '')} | type={item.get('capability_type', '')} | "
                f"criticality={item.get('criticality', '')}\n"
                f"  workstreams={', '.join(item.get('workstream_ids', [])[:6])}\n"
                f"  role_families={', '.join(item.get('inferred_role_families', [])[:4])}\n"
                f"  skill_hints={', '.join(item.get('skill_hints', [])[:8])}"
            )

    if roadmap_analysis.dependencies:
        lines.append('\n### Dependencies')
        for item in roadmap_analysis.dependencies:
            if not isinstance(item, dict):
                continue
            lines.append(
                f"- {item.get('from_workstream_id', '')} -> {item.get('to_workstream_id', '')}: "
                f"{item.get('description', '')} ({item.get('criticality', '')})"
            )

    if roadmap_analysis.delivery_risks:
        lines.append('\n### Delivery risks')
        for item in roadmap_analysis.delivery_risks:
            if not isinstance(item, dict):
                continue
            lines.append(
                f"- [{item.get('severity', '')}] {item.get('risk_type', '')}: {item.get('description', '')} "
                f"(workstreams: {', '.join(item.get('affected_workstreams', [])[:5])})"
            )

    return '\n'.join(lines)[:max_chars]


def _build_legacy_roadmap_input(
    workspace: IntakeWorkspace,
    parsed_sources: list[ParsedSource],
    workspace_profile: dict[str, Any],
    *,
    allowed_source_uuids: list[str] | None = None,
) -> tuple[str, list[dict], str]:
    retrieval_queries = _build_blueprint_retrieval_queries(workspace_profile)
    roadmap_matches = (
        retrieve_workspace_evidence_sync(
            workspace,
            query_text=retrieval_queries['roadmap'],
            doc_types=['roadmap_context'],
            source_kinds=[WorkspaceSourceKind.ROADMAP],
            limit=8,
            additional_filters={'source_uuid': allowed_source_uuids} if allowed_source_uuids else None,
        )
        if allowed_source_uuids is None or allowed_source_uuids
        else []
    )
    roadmap_fallback = _build_parsed_source_digest(
        parsed_sources,
        source_kinds=[WorkspaceSourceKind.ROADMAP],
        max_chars=12000,
    )
    roadmap_input = (
        format_retrieved_evidence_digest(roadmap_matches, max_chars=12000)
        or roadmap_fallback
    )
    return roadmap_input, roadmap_matches, roadmap_fallback


def _build_blueprint_inputs_sync(workspace_pk, snapshot_pk, planning_context_pk=None) -> dict:
    workspace = IntakeWorkspace.objects.get(pk=workspace_pk)
    planning_context = None
    active_source_uuids: list[str] | None = None
    roadmap_source_uuids: list[str] | None = None
    strategy_source_uuids: list[str] | None = None
    role_reference_source_uuids: list[str] | None = None
    if planning_context_pk is not None:
        planning_context = PlanningContext.objects.select_related('workspace', 'project', 'parent_context').get(
            pk=planning_context_pk,
            workspace=workspace,
        )
        workspace_profile = build_planning_context_profile_snapshot(planning_context)
        effective_links = PlanningContext.resolve_effective_sources(planning_context)
        active_source_ids = [link.workspace_source_id for link in effective_links if link.is_active]
        active_source_uuids = [str(link.workspace_source.uuid) for link in effective_links if link.is_active]
        roadmap_source_uuids = [
            str(link.workspace_source.uuid)
            for link in effective_links
            if link.is_active
            and link.include_in_roadmap_analysis
            and (
                link.usage_type in {'roadmap', 'strategy'}
                or link.workspace_source.source_kind in {WorkspaceSourceKind.ROADMAP, WorkspaceSourceKind.STRATEGY}
            )
        ]
        strategy_source_uuids = [
            str(link.workspace_source.uuid)
            for link in effective_links
            if link.is_active
            and (
                link.usage_type == 'strategy'
                or link.workspace_source.source_kind == WorkspaceSourceKind.STRATEGY
            )
        ]
        role_reference_source_uuids = [
            str(link.workspace_source.uuid)
            for link in effective_links
            if link.is_active
            and link.include_in_blueprint
            and link.workspace_source.source_kind in {
                WorkspaceSourceKind.JOB_DESCRIPTION,
                WorkspaceSourceKind.EXISTING_MATRIX,
            }
        ]
        parsed_sources = list(
            ParsedSource.objects.select_related('source')
            .filter(workspace=workspace, source__status=WorkspaceSourceStatus.PARSED, source_id__in=active_source_ids)
            .order_by('source__source_kind', 'created_at')
        )
    else:
        workspace_profile = build_workspace_profile_snapshot(workspace)
        parsed_sources = list(
            ParsedSource.objects.select_related('source')
            .filter(workspace=workspace)
            .filter(source__status=WorkspaceSourceStatus.PARSED)
            .order_by('source__source_kind', 'created_at')
        )
    entries = list(
        RoleLibraryEntry.objects.filter(snapshot_id=snapshot_pk)
        .order_by('role_name')[:120]
    )

    source_counts = Counter(item.source.source_kind for item in parsed_sources)
    source_titles = defaultdict(list)
    for parsed in parsed_sources:
        title = parsed.source.title or parsed.source.source_kind
        source_titles[parsed.source.source_kind].append(title)

    employee_count = Employee.objects.filter(workspace=workspace).count()
    org_units = list(OrgUnit.objects.filter(workspace=workspace).order_by('name').values_list('name', flat=True)[:20])
    projects = list(Project.objects.filter(workspace=workspace).order_by('name').values_list('name', flat=True)[:20])
    top_titles = list(
        Employee.objects.filter(workspace=workspace)
        .exclude(current_title='')
        .values_list('current_title', flat=True)[:50]
    )

    role_library_digest = _build_role_library_digest(entries)
    role_library_summary = _summarize_role_library_entries(entries)

    retrieval_queries = _build_blueprint_retrieval_queries(workspace_profile)
    roadmap_analysis = (
        RoadmapAnalysisRun.objects.filter(
            workspace=workspace,
            **(
                {'planning_context': planning_context}
                if planning_context is not None
                else {'planning_context__isnull': True}
            ),
            status=RoadmapAnalysisRun.Status.COMPLETED,
        )
        .order_by('-created_at')
        .first()
    )
    roadmap_analysis_uuid = str(roadmap_analysis.uuid) if roadmap_analysis is not None else None
    roadmap_matches: list[dict] = []
    roadmap_fallback = _build_parsed_source_digest(
        parsed_sources,
        source_kinds=[WorkspaceSourceKind.ROADMAP],
        max_chars=12000,
    )
    if roadmap_analysis is not None:
        roadmap_input = _build_structured_roadmap_input(roadmap_analysis)
        roadmap_input_mode = 'structured'
    else:
        roadmap_input, roadmap_matches, roadmap_fallback = _build_legacy_roadmap_input(
            workspace,
            parsed_sources,
            workspace_profile,
            allowed_source_uuids=roadmap_source_uuids,
        )
        roadmap_input_mode = 'legacy'
    strategy_matches = (
        retrieve_workspace_evidence_sync(
            workspace,
            query_text=retrieval_queries['strategy'],
            doc_types=['strategy_context'],
            source_kinds=[WorkspaceSourceKind.STRATEGY],
            limit=6,
            additional_filters={'source_uuid': strategy_source_uuids} if strategy_source_uuids else None,
        )
        if planning_context is None or strategy_source_uuids
        else []
    )
    role_reference_matches = (
        retrieve_workspace_evidence_sync(
            workspace,
            query_text=retrieval_queries['role_reference'],
            doc_types=['role_reference'],
            source_kinds=[WorkspaceSourceKind.JOB_DESCRIPTION, WorkspaceSourceKind.EXISTING_MATRIX],
            limit=10,
            additional_filters={'source_uuid': role_reference_source_uuids} if role_reference_source_uuids else None,
        )
        if planning_context is None or role_reference_source_uuids
        else []
    )

    strategy_fallback = _build_parsed_source_digest(
        parsed_sources,
        source_kinds=[WorkspaceSourceKind.STRATEGY],
        max_chars=10000,
    )
    role_reference_fallback = _build_parsed_source_digest(
        parsed_sources,
        source_kinds=[WorkspaceSourceKind.JOB_DESCRIPTION, WorkspaceSourceKind.EXISTING_MATRIX],
        max_chars=12000,
    )
    supplemental_evidence = _build_parsed_source_digest(
        parsed_sources,
        source_kinds=[WorkspaceSourceKind.OTHER],
        max_chars=8000,
    )

    strategy_evidence_digest = (
        format_retrieved_evidence_digest(strategy_matches, max_chars=10000)
        or strategy_fallback
    )
    role_reference_evidence_digest = (
        format_retrieved_evidence_digest(role_reference_matches, max_chars=12000)
        or role_reference_fallback
    )
    combined_evidence_fallback = _build_combined_evidence_fallback_digest(
        roadmap_matches=roadmap_matches if roadmap_analysis is None else [{}],
        strategy_matches=strategy_matches,
        role_reference_matches=role_reference_matches,
        roadmap_fallback=roadmap_fallback,
        strategy_fallback=strategy_fallback,
        role_reference_fallback=role_reference_fallback,
    )

    return {
        'workspace_profile': workspace_profile,
        'planning_context_uuid': str(getattr(planning_context, 'uuid', '') or ''),
        'source_summary': {
            'counts_by_kind': dict(source_counts),
            'titles_by_kind': {key: value[:20] for key, value in source_titles.items()},
            'workspace_name': workspace.name,
            'planning_context_uuid': str(getattr(planning_context, 'uuid', '') or ''),
            'role_library': role_library_summary,
            'retrieval': {
                'roadmap_context': {
                    'query_text': retrieval_queries['roadmap'],
                    'match_count': len(roadmap_matches),
                    'used_vector_retrieval': bool(roadmap_matches) and roadmap_analysis is None,
                    'used_text_fallback': roadmap_analysis is None and not roadmap_matches and bool(roadmap_fallback),
                    'used_structured_analysis': roadmap_analysis is not None,
                },
                'strategy_context': {
                    'query_text': retrieval_queries['strategy'],
                    'match_count': len(strategy_matches),
                    'used_vector_retrieval': bool(strategy_matches),
                    'used_text_fallback': not strategy_matches and bool(strategy_fallback),
                },
                'role_reference': {
                    'query_text': retrieval_queries['role_reference'],
                    'match_count': len(role_reference_matches),
                    'used_vector_retrieval': bool(role_reference_matches),
                    'used_text_fallback': not role_reference_matches and bool(role_reference_fallback),
                },
            },
        },
        'org_summary': {
            'employee_count': employee_count,
            'org_units': org_units,
            'projects': projects,
            'sample_current_titles': top_titles,
        },
        'roadmap_input': roadmap_input,
        'roadmap_analysis_uuid': roadmap_analysis_uuid,
        'roadmap_analysis_pk': roadmap_analysis.pk if roadmap_analysis is not None else None,
        'roadmap_input_mode': roadmap_input_mode,
        'strategy_evidence_digest': strategy_evidence_digest,
        'role_reference_evidence_digest': role_reference_evidence_digest,
        'supplemental_evidence_digest': supplemental_evidence,
        'evidence_digest': combined_evidence_fallback,
        'role_library_digest': role_library_digest,
        'planning_context_name': getattr(planning_context, 'name', ''),
        'planning_context_project_uuid': str(getattr(planning_context, 'project_id', '') or ''),
        'active_source_uuids': active_source_uuids or [],
    }


def _upsert_role_library_entry_sync(snapshot_pk, page_url: str, page_text: dict, extracted: dict) -> None:
    normalized_entry = _merge_role_overlay(
        extracted,
        page_url=page_url,
        page_title=page_text.get('title', ''),
    )
    RoleLibraryEntry.objects.update_or_create(
        snapshot_id=snapshot_pk,
        page_url=page_url,
        defaults={
            'role_name': normalized_entry.get('role_name', '') or page_text.get('title', '') or page_url,
            'department': normalized_entry.get('department', ''),
            'role_family': normalized_entry.get('role_family', ''),
            'summary': normalized_entry.get('summary', ''),
            'levels': normalized_entry.get('levels', []),
            'responsibilities': normalized_entry.get('responsibilities', []),
            'requirements': normalized_entry.get('requirements', []),
            'skills': normalized_entry.get('skills', []),
            'raw_text': page_text.get('text', ''),
            'metadata': normalized_entry.get('metadata', {}),
        },
    )


def _complete_role_library_snapshot_sync(
    snapshot_pk,
    selected_urls: list[str],
    entry_count: int,
    discovery_summary: dict[str, Any],
    taxonomy_summary: dict[str, Any],
    skipped_urls: list[str],
) -> None:
    snapshot = RoleLibrarySnapshot.objects.get(pk=snapshot_pk)
    snapshot.status = RoleLibraryStatus.COMPLETED
    snapshot.discovery_payload = {
        'selected_urls': selected_urls,
        'selected_count': len(selected_urls),
        'skipped_urls': skipped_urls,
        **discovery_summary,
    }
    snapshot.summary = {
        'entry_count': entry_count,
        'provider': snapshot.provider,
        'seed_urls_used': discovery_summary.get('seed_urls_used', []),
        'seed_manifest_version': discovery_summary.get('seed_manifest_version', ''),
        **taxonomy_summary,
    }
    snapshot.error_message = ''
    snapshot.save(update_fields=['status', 'discovery_payload', 'summary', 'error_message', 'updated_at'])


def _fail_role_library_snapshot_sync(snapshot_pk, error_message: str) -> None:
    snapshot = RoleLibrarySnapshot.objects.get(pk=snapshot_pk)
    snapshot.status = RoleLibraryStatus.FAILED
    snapshot.error_message = error_message
    snapshot.save(update_fields=['status', 'error_message', 'updated_at'])


def _record_blueprint_run_inputs_sync(run_pk, source_summary: dict, input_snapshot: dict) -> None:
    run = SkillBlueprintRun.objects.get(pk=run_pk)
    run.source_summary = source_summary
    run.input_snapshot = input_snapshot
    run.save(update_fields=['source_summary', 'input_snapshot', 'updated_at'])


def _persist_blueprint_payload_sync(run_pk, llm_payload: dict) -> dict:
    run = SkillBlueprintRun.objects.select_related('workspace').get(pk=run_pk)
    workspace = run.workspace

    role_candidates = llm_payload.get('role_candidates', [])
    required_skill_set = _flatten_required_skill_set(role_candidates)

    with transaction.atomic():
        # Blueprint-generated normalized facts are now retained per run so a
        # newer draft cannot silently overwrite the latest reviewed/approved
        # target model that downstream stages depend on.

        role_lookup: dict[tuple[str, str], RoleProfile] = {}
        for role_payload in role_candidates:
            role_name = role_payload.get('role_name', '')
            seniority = role_payload.get('seniority', '')
            normalized_role = normalize_external_role_title(
                role_name=role_name,
                role_family_hint=role_payload.get('canonical_role_family', '') or role_payload.get('role_family', ''),
            )
            family_config = CANONICAL_ROLE_FAMILIES[normalized_role['canonical_family']]
            role_profile = RoleProfile.objects.create(
                workspace=workspace,
                blueprint_run=run,
                name=role_name,
                family=normalized_role['canonical_family'],
                seniority=seniority,
                canonical_occupation_key=family_config.get('occupation', {}).get('key', slugify_key(role_name)),
                metadata={
                    'related_initiatives': role_payload.get('related_initiatives', []),
                    'rationale': role_payload.get('rationale', ''),
                    'responsibilities': role_payload.get('responsibilities', []),
                    'headcount_needed': role_payload.get('headcount_needed', 0),
                    'external_role_family': role_payload.get('role_family', ''),
                    'canonical_role_label': normalized_role['canonical_label'],
                    'role_already_exists_internally': bool(role_payload.get('role_already_exists_internally')),
                    'likely_requires_hiring': bool(role_payload.get('likely_requires_hiring')),
                    'confidence': _coerce_confidence(role_payload.get('confidence'), default=0.7),
                    'ambiguity_notes': role_payload.get('ambiguity_notes', []),
                    'role_key': role_payload.get('role_key', slugify_key(f"{normalized_role['canonical_family']}-{seniority}")),
                    'blueprint_run_uuid': str(run.uuid),
                },
            )
            role_lookup[(role_name, seniority)] = role_profile

        occupation_map = llm_payload.get('occupation_map', [])
        for item in occupation_map:
            role_profile = role_lookup.get((item.get('role_name', ''), _find_role_seniority(role_candidates, item.get('role_name', ''))))
            if role_profile is None:
                continue
            resolved_occupation, occupation_match = resolve_esco_occupation_sync(
                str(item.get('reference_role') or item.get('role_name') or '').strip(),
                alternatives=[
                    str(role_profile.name or '').strip(),
                    str(role_profile.canonical_occupation_key or '').strip(),
                ],
                workspace=workspace,
                role_family_hint=str(role_profile.family or '').strip(),
                review_metadata={
                    'role_name': str(role_profile.name or '').strip(),
                    'canonical_occupation_key': str(role_profile.canonical_occupation_key or '').strip(),
                    'source': 'blueprint_occupation_map',
                },
            )
            OccupationMapping.objects.create(
                workspace=workspace,
                role_profile=role_profile,
                occupation_key=slugify_key(item.get('reference_role', '') or item.get('role_name', '')),
                occupation_name_en=item.get('reference_role', ''),
                occupation_name_ru='',
                esco_occupation=resolved_occupation,
                match_score=_normalize_role_fit_score(item.get('match_score', 0)),
                metadata={
                    'reference_url': item.get('reference_url', ''),
                    'match_reason': item.get('match_reason', ''),
                    'esco_occupation_uri': str(occupation_match.get('esco_occupation_uri') or ''),
                    'esco_match_source': str(occupation_match.get('match_source') or ''),
                    'esco_match_score': float(occupation_match.get('match_score') or 0.0),
                    'esco_match_confidence': str(occupation_match.get('match_confidence') or ''),
                    'esco_candidate_matches': list(occupation_match.get('candidate_matches') or []),
                    'blueprint_run_uuid': str(run.uuid),
                },
            )

        existing_role_profile_ids = set(
            OccupationMapping.objects.filter(workspace=workspace).values_list('role_profile_id', flat=True)
        )
        for role_profile in role_lookup.values():
            if role_profile.pk in existing_role_profile_ids:
                continue
            family_config = CANONICAL_ROLE_FAMILIES.get(role_profile.family, {})
            occupation = family_config.get('occupation')
            if not occupation:
                continue
            resolved_occupation, occupation_match = resolve_esco_occupation_sync(
                str(occupation.get('name_en') or role_profile.name or '').strip(),
                alternatives=[
                    str(role_profile.name or '').strip(),
                    str(role_profile.canonical_occupation_key or '').strip(),
                ],
                workspace=workspace,
                role_family_hint=str(role_profile.family or '').strip(),
                review_metadata={
                    'role_name': str(role_profile.name or '').strip(),
                    'canonical_occupation_key': str(role_profile.canonical_occupation_key or '').strip(),
                    'source': 'role_library_overlay',
                },
            )
            OccupationMapping.objects.create(
                workspace=workspace,
                role_profile=role_profile,
                occupation_key=occupation.get('key', slugify_key(role_profile.name)),
                occupation_name_en=occupation.get('name_en', role_profile.name),
                occupation_name_ru='',
                esco_occupation=resolved_occupation,
                match_score=0.85,
                metadata={
                    'reference_url': '',
                    'match_reason': 'Curated canonical role-family mapping',
                    'mapping_source': 'role_library_overlay',
                    'esco_occupation_uri': str(occupation_match.get('esco_occupation_uri') or ''),
                    'esco_match_source': str(occupation_match.get('match_source') or ''),
                    'esco_match_score': float(occupation_match.get('match_score') or 0.0),
                    'esco_match_confidence': str(occupation_match.get('match_confidence') or ''),
                    'esco_candidate_matches': list(occupation_match.get('candidate_matches') or []),
                    'blueprint_run_uuid': str(run.uuid),
                },
            )

        for role_payload in role_candidates:
            role_profile = role_lookup.get((role_payload.get('role_name', ''), role_payload.get('seniority', '')))
            if role_profile is None:
                continue
            for skill_payload in role_payload.get('skills', []):
                normalized_skill = normalize_skill_seed(
                    skill_payload.get('skill_name_en', ''),
                    workspace=workspace,
                    review_metadata={
                        'source': 'blueprint_seed',
                        'role_name': str(role_profile.name or '').strip(),
                    },
                )
                skill = ensure_workspace_skill_sync(
                    workspace,
                    normalized_skill=normalized_skill,
                    preferred_display_name_ru=skill_payload.get('skill_name_ru', ''),
                    aliases=[
                        skill_payload.get('skill_name_ru', ''),
                    ],
                    created_source='blueprint_seed',
                )
                RoleSkillRequirement.objects.create(
                    workspace=workspace,
                    role_profile=role_profile,
                    skill=skill,
                    target_level=int(skill_payload.get('target_level', 0) or 0),
                    priority=int(skill_payload.get('priority', 0) or 0),
                    is_required=_normalize_requirement_type(skill_payload.get('requirement_type', 'core')) in {'core', 'org_specific'},
                    source_kind='blueprint',
                    metadata={
                        'reason': skill_payload.get('reason', ''),
                        'requirement_type': _normalize_requirement_type(skill_payload.get('requirement_type', 'core')),
                        'criticality': _normalize_criticality(skill_payload.get('criticality', ''), priority=_coerce_int(skill_payload.get('priority'), default=3, minimum=1, maximum=5)),
                        'supported_initiatives': skill_payload.get('supported_initiatives', []),
                        'confidence': _coerce_confidence(skill_payload.get('confidence'), default=0.65),
                        'blueprint_run_uuid': str(run.uuid),
                    },
                )

    return {
        'role_candidates': role_candidates,
        'required_skill_set': required_skill_set,
    }


def _build_clarification_cycle_title(run: SkillBlueprintRun) -> str:
    return f'Clarifications for {run.title}'


def _build_clarification_question_snapshot(question: ClarificationQuestion) -> dict[str, Any]:
    return {
        'question_uuid': str(question.uuid),
        'id': question.question_key,
        'question': question.question_text,
        'scope': question.scope,
        'priority': question.priority,
        'why_it_matters': question.rationale,
        'intended_respondent_type': question.intended_respondent_type,
        'evidence_refs': list(question.evidence_refs or []),
        'impacted_roles': list(question.impacted_roles or []),
        'impacted_initiatives': list(question.impacted_initiatives or []),
        'status': question.status,
        'answer': question.answer_text,
        'note': question.status_note,
        'changed_target_model': bool(question.changed_target_model),
        'answered_by': question.answered_by,
        'answered_at': question.answered_at.isoformat() if question.answered_at else '',
    }


def _synchronize_run_from_clarification_cycle_sync(run_pk) -> None:
    run = SkillBlueprintRun.objects.get(pk=run_pk)
    cycle = ClarificationCycle.objects.filter(blueprint_run=run).first()
    if cycle is None:
        return

    questions = list(
        ClarificationQuestion.objects.filter(cycle=cycle)
        .order_by('created_at', 'question_key')
    )
    clarification_payload = [
        _build_clarification_question_snapshot(question)
        for question in questions
    ]
    clarification_summary = _build_clarification_summary(clarification_payload)
    cycle.status = (
        ClarificationCycleStatus.COMPLETED
        if clarification_summary['open'] == 0
        else ClarificationCycleStatus.OPEN
    )
    cycle.summary = clarification_summary
    cycle.save(update_fields=['status', 'summary', 'updated_at'])

    new_status = run.status
    if run.status in {
        BlueprintStatus.RUNNING,
        BlueprintStatus.DRAFT,
        BlueprintStatus.NEEDS_CLARIFICATION,
    }:
        new_status = _resolve_generated_blueprint_status(clarification_payload)
    elif run.status == BlueprintStatus.REVIEWED and clarification_summary['open'] > 0:
        new_status = BlueprintStatus.NEEDS_CLARIFICATION

    review_summary = _build_blueprint_review_summary(
        roadmap_context=list(run.roadmap_context or []),
        role_candidates=list(run.role_candidates or []),
        clarification_questions=clarification_payload,
        required_skill_set=list(run.required_skill_set or []),
        employee_matches=list(run.employee_role_matches or []),
    )
    update_fields = [
        'status',
        'clarification_questions',
        'review_summary',
        'updated_at',
    ]
    run.status = new_status
    run.clarification_questions = clarification_payload
    run.review_summary = review_summary
    run.save(update_fields=update_fields)


def _has_clarification_operator_input(question: ClarificationQuestion) -> bool:
    return bool(str(question.answer_text or '').strip() or str(question.status_note or '').strip())


def _assert_clarification_question_updateable(question: ClarificationQuestion) -> None:
    if question.status in _CLOSED_CLARIFICATION_STATUSES:
        raise ValueError(
            f'Clarification question "{question.question_key}" is closed and cannot be updated in place. '
            'Start a revision if you need to revisit it.'
        )


def _sync_clarification_cycle_from_run_sync(run_pk) -> None:
    run = SkillBlueprintRun.objects.get(pk=run_pk)
    cycle, _created = ClarificationCycle.objects.get_or_create(
        workspace=run.workspace,
        blueprint_run=run,
        defaults={
            'title': _build_clarification_cycle_title(run),
            'status': ClarificationCycleStatus.OPEN,
            'summary': {},
        },
    )
    if cycle.title != _build_clarification_cycle_title(run):
        cycle.title = _build_clarification_cycle_title(run)
        cycle.save(update_fields=['title', 'updated_at'])

    active_keys: set[str] = set()
    for item in run.clarification_questions or []:
        question = str(item.get('question') or '').strip()
        if not question:
            continue
        question_key = str(item.get('id') or slugify_key(question)).strip()
        active_keys.add(question_key)
        ClarificationQuestion.objects.update_or_create(
            cycle=cycle,
            question_key=question_key,
            defaults={
                'workspace': run.workspace,
                'blueprint_run': run,
                'question_text': question,
                'scope': str(item.get('scope') or 'blueprint').strip() or 'blueprint',
                'priority': str(item.get('priority') or 'medium').strip().lower() or 'medium',
                'intended_respondent_type': str(item.get('intended_respondent_type') or 'operator').strip() or 'operator',
                'rationale': str(item.get('why_it_matters') or '').strip(),
                'evidence_refs': _normalize_evidence_refs(item.get('evidence_refs')),
                'impacted_roles': _dedupe_strings(item.get('impacted_roles') or []),
                'impacted_initiatives': _dedupe_strings(item.get('impacted_initiatives') or []),
                'status': _normalize_clarification_status(item.get('status')),
                'answer_text': str(item.get('answer') or '').strip(),
                'answered_by': str(item.get('answered_by') or '').strip(),
                'answered_at': _parse_iso_datetime(item.get('answered_at')),
                'status_note': str(item.get('note') or '').strip(),
                'changed_target_model': bool(item.get('changed_target_model')),
                'effect_metadata': {
                    'source': 'blueprint_run_snapshot',
                    'blueprint_run_uuid': str(run.uuid),
                },
            },
        )

    stale_questions = ClarificationQuestion.objects.filter(cycle=cycle).exclude(question_key__in=active_keys)
    for question in stale_questions:
        question.status = ClarificationQuestionStatus.OBSOLETE
        if not question.status_note:
            question.status_note = 'No longer present in the latest blueprint run snapshot.'
        question.save(update_fields=['status', 'status_note', 'updated_at'])

    _synchronize_run_from_clarification_cycle_sync(run.pk)


def _apply_clarification_answers_sync(
    run_pk,
    *,
    operator_name: str,
    answer_items: list[dict[str, Any]],
) -> None:
    run = SkillBlueprintRun.objects.get(pk=run_pk)
    if not ClarificationCycle.objects.filter(blueprint_run=run).exists():
        _sync_clarification_cycle_from_run_sync(run.pk)
    cycle = ClarificationCycle.objects.get(blueprint_run=run)

    updates_by_uuid = {
        str(item.get('question_uuid') or '').strip(): item
        for item in answer_items
        if str(item.get('question_uuid') or '').strip()
    }
    updates_by_key = {
        str(item.get('clarification_id') or '').strip(): item
        for item in answer_items
        if str(item.get('clarification_id') or '').strip()
    }
    applied = 0
    with transaction.atomic():
        for question in ClarificationQuestion.objects.select_for_update().filter(cycle=cycle):
            update = updates_by_uuid.get(str(question.uuid)) or updates_by_key.get(question.question_key)
            if update is None:
                continue
            _assert_clarification_question_updateable(question)
            answer_text = str(update.get('answer_text') or update.get('answer') or '').strip()
            raw_status = update.get('status')
            if not raw_status and answer_text:
                raw_status = ClarificationQuestionStatus.ANSWERED
            question.answer_text = answer_text
            question.status = _normalize_clarification_status(raw_status or question.status)
            question.status_note = str(update.get('status_note') or update.get('note') or '').strip()
            question.changed_target_model = bool(update.get('changed_target_model'))
            question.answered_by = operator_name or str(update.get('operator_name') or question.answered_by).strip()
            question.answered_at = datetime.now(timezone.utc)
            question.effect_metadata = {
                **(question.effect_metadata or {}),
                'latest_answer_actor': question.answered_by,
                'latest_answer_at': question.answered_at.isoformat(),
            }
            question.save(
                update_fields=[
                    'answer_text',
                    'status',
                    'status_note',
                    'changed_target_model',
                    'answered_by',
                    'answered_at',
                    'effect_metadata',
                    'updated_at',
                ]
            )
            applied += 1

    if applied == 0:
        raise ValueError('No clarification questions matched the submitted updates.')

    _synchronize_run_from_clarification_cycle_sync(run.pk)


def _load_answered_clarifications_sync(run_pk) -> list[dict[str, Any]]:
    run = SkillBlueprintRun.objects.get(pk=run_pk)
    cycle = ClarificationCycle.objects.filter(blueprint_run=run).first()
    if cycle is None:
        return []
    answered: list[dict[str, Any]] = []
    for question in ClarificationQuestion.objects.filter(cycle=cycle).order_by('created_at', 'question_key'):
        if question.status not in {
            ClarificationQuestionStatus.ANSWERED,
            ClarificationQuestionStatus.ACCEPTED,
            ClarificationQuestionStatus.REJECTED,
        }:
            continue
        if not _has_clarification_operator_input(question):
            continue
        answered.append(
            {
                'question_uuid': str(question.uuid),
                'clarification_id': question.question_key,
                'question': question.question_text,
                'scope': question.scope,
                'priority': question.priority,
                'why_it_matters': question.rationale,
                'status': question.status,
                'answer': question.answer_text,
                'status_note': question.status_note,
                'impacted_roles': list(question.impacted_roles or []),
                'impacted_initiatives': list(question.impacted_initiatives or []),
                'changed_target_model': bool(question.changed_target_model),
            }
        )
    return answered


def _publish_blueprint_run_sync(
    run_pk,
    *,
    publisher_name: str,
    publish_notes: str,
) -> None:
    run = SkillBlueprintRun.objects.get(pk=run_pk)
    if run.is_published:
        return
    clarification_summary = _build_clarification_summary(list(run.clarification_questions or []))
    if run.status not in BLUEPRINT_REVIEW_READY_STATUSES:
        raise ValueError('Only reviewed or approved blueprints can be published.')
    if clarification_summary['open'] > 0:
        raise ValueError('All clarification items must be accepted or marked obsolete before publishing.')

    with transaction.atomic():
        publish_scope = (
            {'planning_context_id': run.planning_context_id}
            if run.planning_context_id is not None
            else {'planning_context__isnull': True}
        )
        SkillBlueprintRun.objects.filter(
            workspace=run.workspace,
            is_published=True,
            **publish_scope,
        ).exclude(pk=run.pk).update(is_published=False)
        run.is_published = True
        run.published_by = publisher_name or run.published_by
        run.published_notes = publish_notes or run.published_notes
        run.published_at = datetime.now(timezone.utc)
        run.change_log = [
            *(run.change_log or []),
            {
                'event': 'published',
                'at': _utc_now_iso(),
                'actor': publisher_name or 'operator',
                'note': publish_notes,
            },
        ]
        run.save(
            update_fields=[
                'is_published',
                'published_by',
                'published_notes',
                'published_at',
                'change_log',
                'updated_at',
            ]
        )


def _finalize_blueprint_run_sync(
    run_pk,
    llm_payload: dict,
    required_skill_set: list[dict],
    employee_matches: list[dict],
    gap_summary: dict,
    redundancy_summary: dict,
) -> None:
    run = SkillBlueprintRun.objects.get(pk=run_pk)
    clarification_questions = llm_payload.get('clarification_questions', [])
    run.status = _resolve_generated_blueprint_status(clarification_questions)
    run.company_context = llm_payload.get('company_context', {})
    run.roadmap_context = llm_payload.get('roadmap_context', [])
    run.role_candidates = llm_payload.get('role_candidates', [])
    run.clarification_questions = clarification_questions
    run.employee_role_matches = employee_matches
    run.required_skill_set = required_skill_set
    run.automation_candidates = llm_payload.get('automation_candidates', [])
    run.occupation_map = llm_payload.get('occupation_map', [])
    run.gap_summary = gap_summary
    run.redundancy_summary = redundancy_summary
    run.assessment_plan = llm_payload.get('assessment_plan', {})
    run.review_summary = _build_blueprint_review_summary(
        roadmap_context=run.roadmap_context,
        role_candidates=run.role_candidates,
        clarification_questions=run.clarification_questions,
        required_skill_set=required_skill_set,
        employee_matches=employee_matches,
    )
    run.save(
        update_fields=[
            'status', 'company_context', 'roadmap_context', 'role_candidates',
            'clarification_questions', 'employee_role_matches', 'required_skill_set', 'automation_candidates',
            'occupation_map', 'gap_summary', 'redundancy_summary', 'assessment_plan', 'review_summary', 'updated_at',
        ]
    )
    _sync_clarification_cycle_from_run_sync(run.pk)


def _apply_blueprint_review_sync(
    run_pk,
    *,
    reviewer_name: str,
    review_notes: str,
    clarification_updates: list[dict[str, Any]],
    approve: bool,
) -> None:
    run = SkillBlueprintRun.objects.get(pk=run_pk)
    updated_clarifications = _merge_clarification_updates(
        list(run.clarification_questions or []),
        clarification_updates,
    )
    clarification_summary = _build_clarification_summary(updated_clarifications)
    if approve and clarification_summary['open'] > 0:
        raise ValueError('All clarification items must be accepted or marked obsolete before approval.')

    run.clarification_questions = updated_clarifications
    run.review_summary = _build_blueprint_review_summary(
        roadmap_context=list(run.roadmap_context or []),
        role_candidates=list(run.role_candidates or []),
        clarification_questions=updated_clarifications,
        required_skill_set=list(run.required_skill_set or []),
        employee_matches=list(run.employee_role_matches or []),
    )
    run.reviewed_by = reviewer_name or run.reviewed_by
    run.review_notes = review_notes or run.review_notes
    run.reviewed_at = datetime.now(timezone.utc)
    if approve:
        run.status = BlueprintStatus.APPROVED
        run.approved_by = reviewer_name or run.approved_by
        run.approval_notes = review_notes or run.approval_notes
        run.approved_at = datetime.now(timezone.utc)
        event = 'approved'
    else:
        run.status = (
            BlueprintStatus.REVIEWED
            if clarification_summary['open'] == 0
            else BlueprintStatus.NEEDS_CLARIFICATION
        )
        event = 'reviewed'
    run.change_log = [
        *(run.change_log or []),
        {
            'event': event,
            'at': _utc_now_iso(),
            'actor': reviewer_name or 'operator',
            'note': review_notes,
        },
    ]
    run.save(
        update_fields=[
            'status',
            'clarification_questions',
            'review_summary',
            'reviewed_by',
            'review_notes',
            'reviewed_at',
            'approved_by',
            'approval_notes',
            'approved_at',
            'change_log',
            'updated_at',
        ]
    )
    _sync_clarification_cycle_from_run_sync(run.pk)


def _fail_blueprint_run_sync(run_pk, error_message: str) -> None:
    run = SkillBlueprintRun.objects.get(pk=run_pk)
    run.status = BlueprintStatus.FAILED
    run.source_summary = {**(run.source_summary or {}), 'error_message': error_message}
    run.change_log = [
        *(run.change_log or []),
        {
            'event': 'failed',
            'at': _utc_now_iso(),
            'actor': 'system',
            'note': error_message,
        },
    ]
    run.save(update_fields=['status', 'source_summary', 'change_log', 'updated_at'])


def _flatten_required_skill_set(role_candidates: list[dict]) -> list[dict]:
    aggregated: dict[str, dict] = {}
    for role in role_candidates:
        role_name = role.get('role_name', '')
        for skill in role.get('skills', []):
            normalized_skill = normalize_skill_seed(skill.get('skill_name_en', ''))
            key = normalized_skill['canonical_key']
            bucket = aggregated.setdefault(
                key,
                {
                    'skill_name_en': normalized_skill['display_name_en'],
                    'skill_name_ru': skill.get('skill_name_ru', '') or normalized_skill.get('display_name_ru', ''),
                    'max_target_level': 0,
                    'max_priority': 0,
                    'required_by_roles': [],
                    'supported_initiatives': [],
                    'requirement_types': [],
                    'max_confidence': 0.0,
                    'reasons': [],
                    'criticalities': [],
                },
            )
            bucket['max_target_level'] = max(bucket['max_target_level'], int(skill.get('target_level', 0) or 0))
            bucket['max_priority'] = max(bucket['max_priority'], int(skill.get('priority', 0) or 0))
            bucket['max_confidence'] = max(
                bucket['max_confidence'],
                _coerce_confidence(skill.get('confidence'), default=0.65),
            )
            if role_name and role_name not in bucket['required_by_roles']:
                bucket['required_by_roles'].append(role_name)
            bucket['supported_initiatives'] = _dedupe_strings(
                [*bucket['supported_initiatives'], *(skill.get('supported_initiatives') or [])]
            )
            reason_text = str(skill.get('reason') or '').strip()
            if reason_text:
                bucket['reasons'].append(reason_text)
            criticality = str(skill.get('criticality') or '').strip()
            if criticality:
                bucket['criticalities'].append(criticality)
            requirement_type = _normalize_requirement_type(skill.get('requirement_type', 'core'))
            if requirement_type not in bucket['requirement_types']:
                bucket['requirement_types'].append(requirement_type)
    flattened: list[dict[str, Any]] = []
    for bucket in aggregated.values():
        initiative_preview = ', '.join(bucket['supported_initiatives'][:3])
        roles_preview = ', '.join(bucket['required_by_roles'][:3])
        if 'core' in bucket['requirement_types']:
            requirement_type = 'core'
        elif 'org_specific' in bucket['requirement_types']:
            requirement_type = 'org_specific'
        elif bucket['requirement_types']:
            requirement_type = bucket['requirement_types'][0]
        else:
            requirement_type = 'core'
        criticality_values = {_normalize_criticality(value) for value in bucket['criticalities'] if value}
        if 'high' in criticality_values:
            flattened_criticality = 'high'
        elif 'medium' in criticality_values:
            flattened_criticality = 'medium'
        elif 'low' in criticality_values:
            flattened_criticality = 'low'
        else:
            flattened_criticality = _normalize_criticality('', priority=bucket['max_priority'])
        flattened.append(
            {
                **bucket,
                'target_level': bucket['max_target_level'],
                'priority': bucket['max_priority'],
                'confidence': round(bucket['max_confidence'], 2),
                'requirement_type': requirement_type,
                'criticality': flattened_criticality,
                'reason': _merge_reason_text(
                    [
                        *bucket['reasons'],
                        f"Required by {len(bucket['required_by_roles'])} role(s): {roles_preview}" if roles_preview else '',
                        f"Supports initiatives: {initiative_preview}" if initiative_preview else '',
                    ]
                ),
            }
        )
    return sorted(
        flattened,
        key=lambda item: (-item['max_priority'], -item['max_target_level'], item['skill_name_en']),
    )


def _find_role_seniority(role_candidates: list[dict], role_name: str) -> str:
    for role in role_candidates:
        if role.get('role_name') == role_name:
            return role.get('seniority', '')
    return ''


def _load_employee_matching_inputs_sync(workspace_pk, planning_context_pk=None) -> list[dict]:
    workspace = IntakeWorkspace.objects.get(pk=workspace_pk)
    context = None
    assigned_ids: set[str] = set()
    if planning_context_pk is not None:
        context = PlanningContext.objects.select_related('project').filter(
            pk=planning_context_pk,
            workspace=workspace,
        ).first()
        if context is not None and context.project_id:
            assigned_ids = {
                str(employee_id)
                for employee_id in EmployeeProjectAssignment.objects.filter(
                    project_id=context.project_id,
                ).values_list('employee_id', flat=True)
            }
    employees = list(Employee.objects.filter(workspace=workspace).order_by('full_name'))
    payloads = []
    for employee in employees:
        org_assignments = list(
            EmployeeOrgAssignment.objects.filter(employee=employee)
            .select_related('org_unit')
            .order_by('-is_primary', 'org_unit__name')
        )
        project_assignments = list(
            EmployeeProjectAssignment.objects.filter(employee=employee)
            .select_related('project')
            .order_by('project__name')
        )
        skill_evidence = list(
            EmployeeSkillEvidence.objects.filter(employee=employee)
            .select_related('skill')
            .filter(weight__gt=0, skill__resolution_status=Skill.ResolutionStatus.RESOLVED)
            .order_by('-weight', '-confidence', 'skill__display_name_en')[:12]
        )
        cv_profile = EmployeeCVProfile.objects.filter(
            workspace=workspace,
            employee=employee,
            status=EmployeeCVProfile.Status.MATCHED,
        ).order_by('-updated_at').first()
        extracted = dict((cv_profile.extracted_payload or {}) if cv_profile is not None else {})
        role_history = (extracted.get('role_history') or [])[:5]
        role_history_trimmed = [
            {
                'company_name': str(item.get('company_name') or '')[:80],
                'role_title': str(item.get('role_title') or '')[:80],
                'start_date': str(item.get('start_date') or ''),
                'end_date': str(item.get('end_date') or ''),
                'key_achievements': [str(value or '')[:200] for value in (item.get('achievements') or [])[:3]],
                'domains': [str(value or '')[:100] for value in (item.get('domains') or [])[:5]],
                'leadership_signals': [str(value or '')[:150] for value in (item.get('leadership_signals') or [])[:3]],
            }
            for item in role_history
        ]
        achievements = sorted(
            extracted.get('achievements') or [],
            key=lambda item: float(item.get('confidence_score', 0.0) or 0.0),
            reverse=True,
        )[:5]
        achievement_items = [
            {
                'summary': str(item.get('summary') or item.get('achievement') or '')[:200],
                'confidence_score': float(item.get('confidence_score', 0.0) or 0.0),
            }
            for item in achievements
        ]
        domain_experience = [
            {
                'domain': str(item.get('domain') or '')[:100],
                'confidence_score': float(item.get('confidence_score', 0.0) or 0.0),
            }
            for item in (extracted.get('domain_experience') or [])[:6]
        ]
        leadership_signals = [
            {
                'signal': str(item.get('signal') or '')[:150],
                'confidence_score': float(item.get('confidence_score', 0.0) or 0.0),
            }
            for item in (extracted.get('leadership_signals') or [])[:5]
        ]
        payloads.append(
            {
                'employee_uuid': str(employee.uuid),
                'full_name': employee.full_name,
                'email': employee.email,
                'current_title': employee.current_title,
                'seniority': cv_profile.seniority if cv_profile is not None else '',
                'headline': cv_profile.headline if cv_profile is not None else '',
                'org_units': [assignment.org_unit.name for assignment in org_assignments],
                'projects': [assignment.project.name for assignment in project_assignments],
                'skills_from_evidence': [
                    {
                        'skill_name_en': evidence.skill.display_name_en,
                        'current_level': float(evidence.current_level),
                        'confidence': float(evidence.confidence),
                        'source_kind': evidence.source_kind,
                        'resolution_status': evidence.skill.resolution_status,
                    }
                    for evidence in skill_evidence
                ],
                'role_history': role_history_trimmed,
                'achievements': achievement_items,
                'domain_experience': domain_experience,
                'leadership_signals': leadership_signals,
                'is_assigned_to_context_project': (
                    str(employee.uuid) in assigned_ids if context is not None and context.project_id else False
                ),
            }
        )
    return payloads


def _persist_employee_role_matches_sync(
    workspace_pk,
    blueprint_run_uuid: str,
    employee_uuid: str,
    matches: list[dict],
) -> list[dict]:
    workspace = IntakeWorkspace.objects.get(pk=workspace_pk)
    employee = Employee.objects.get(pk=employee_uuid, workspace=workspace)
    blueprint_run = SkillBlueprintRun.objects.filter(pk=blueprint_run_uuid, workspace=workspace).only(
        'planning_context_id'
    ).first()
    planning_context_id = getattr(blueprint_run, 'planning_context_id', None)
    persisted: list[dict] = []
    for item in matches:
        role_profile = RoleProfile.objects.filter(
            workspace=workspace,
            blueprint_run_id=blueprint_run_uuid,
            name=item.get('role_name', ''),
            seniority=item.get('seniority', ''),
        ).first()
        if role_profile is None:
            continue
        match, _created = EmployeeRoleMatch.objects.update_or_create(
            workspace=workspace,
            employee=employee,
            role_profile=role_profile,
            source_kind='blueprint',
            defaults={
                'planning_context_id': planning_context_id,
                'fit_score': _normalize_role_fit_score(item.get('fit_score', 0)),
                'rationale': item.get('reason', ''),
                'related_initiatives': item.get('related_initiatives', []),
                'metadata': {
                    'blueprint_run_uuid': blueprint_run_uuid,
                    'role_key': (role_profile.metadata or {}).get('role_key', ''),
                },
            },
        )
        persisted.append(
            {
                'role_name': role_profile.name,
                'seniority': role_profile.seniority,
                'role_key': (role_profile.metadata or {}).get('role_key', ''),
                'fit_score': float(match.fit_score),
                'reason': match.rationale,
                'related_initiatives': match.related_initiatives,
            }
        )
    return persisted


def _clone_employee_role_matches_sync(
    workspace_pk,
    blueprint_run_uuid: str,
    employee_matches_payload: list[dict],
) -> list[dict]:
    cloned_payload: list[dict] = []
    for employee_payload in employee_matches_payload:
        employee_uuid = str(employee_payload.get('employee_uuid') or '').strip()
        if not employee_uuid:
            continue
        matches = employee_payload.get('matches') or []
        persisted = _persist_employee_role_matches_sync(
            workspace_pk,
            blueprint_run_uuid,
            employee_uuid,
            matches,
        )
        cloned_payload.append(
            {
                'employee_uuid': employee_uuid,
                'full_name': employee_payload.get('full_name', ''),
                'matches': persisted,
            }
        )
    return cloned_payload


def _compute_role_gap_summaries_sync(workspace_pk, role_candidates: list[dict], blueprint_run_uuid) -> tuple[dict, dict]:
    workspace = IntakeWorkspace.objects.get(pk=workspace_pk)
    current_counts = Counter()
    for match in EmployeeRoleMatch.objects.filter(
        workspace=workspace,
        source_kind='blueprint',
        role_profile__blueprint_run_id=blueprint_run_uuid,
    ).select_related('role_profile'):
        if _normalize_role_fit_score(match.fit_score) >= 0.70:
            match_role_key = (match.metadata or {}).get('role_key') or (match.role_profile.metadata or {}).get('role_key')
            if not match_role_key:
                match_role_key = slugify_key(f'{match.role_profile.family}-{match.role_profile.seniority}')
            current_counts[match_role_key] += 1

    gaps = []
    redundancies = []
    for role in role_candidates:
        key = role.get('role_key') or slugify_key(
            f"{role.get('canonical_role_family', '') or role.get('role_family', '')}-{role.get('seniority', '')}"
        )
        desired = int(role.get('headcount_needed', 0) or 0)
        current = int(current_counts.get(key, 0))
        if desired > current:
            gaps.append(
                {
                    'role_key': key,
                    'role_name': role.get('role_name', ''),
                    'seniority': role.get('seniority', ''),
                    'desired': desired,
                    'current': current,
                    'gap': desired - current,
                    'related_initiatives': role.get('related_initiatives', []),
                }
            )
        elif current > desired:
            redundancies.append(
                {
                    'role_key': key,
                    'role_name': role.get('role_name', ''),
                    'seniority': role.get('seniority', ''),
                    'desired': desired,
                    'current': current,
                    'excess': current - desired,
                    'related_initiatives': role.get('related_initiatives', []),
                }
            )

    return (
        {
            'critical_role_gaps': gaps,
            'matched_employee_count': sum(current_counts.values()),
        },
        {
            'redundant_role_capacity': redundancies,
        },
    )


def _compute_coverage_analysis_sync(
    workspace_pk,
    blueprint_run_uuid: str,
    roadmap_analysis_uuid: str | None,
) -> dict[str, Any]:
    workspace = IntakeWorkspace.objects.get(pk=workspace_pk)
    if not roadmap_analysis_uuid:
        return {'coverage_score': None, 'message': 'No roadmap analysis available'}

    roadmap = RoadmapAnalysisRun.objects.filter(pk=roadmap_analysis_uuid).first()
    if roadmap is None:
        return {'coverage_score': None, 'message': 'Roadmap analysis could not be found'}

    role_profiles = list(
        RoleProfile.objects.filter(workspace=workspace, blueprint_run_id=blueprint_run_uuid)
        .prefetch_related('skill_requirements__skill__aliases')
    )
    role_families = {role_profile.family for role_profile in role_profiles if role_profile.family}
    role_skill_keys: set[str] = set()
    role_skill_aliases: set[str] = set()
    for role_profile in role_profiles:
        for requirement in role_profile.skill_requirements.all():
            role_skill_keys.add(requirement.skill.canonical_key)
            role_skill_aliases.add(requirement.skill.display_name_en.lower().strip())
            role_skill_aliases.add(requirement.skill.display_name_ru.lower().strip())
            for alias in requirement.skill.aliases.all():
                role_skill_aliases.add(alias.alias.lower().strip())

    workstream_coverage: list[dict[str, Any]] = []
    uncovered_workstreams: list[dict[str, Any]] = []
    for workstream in roadmap.workstreams or []:
        if not isinstance(workstream, dict):
            continue
        raw_roles_needed = [str(item or '').strip() for item in ((workstream.get('team_shape') or {}).get('roles_needed') or []) if str(item or '').strip()]
        normalized_roles_needed: set[str] = set()
        for role_name in raw_roles_needed:
            normalized_role = normalize_external_role_title(
                role_name=role_name,
                role_family_hint=role_name,
            )
            normalized_roles_needed.add(normalized_role.get('canonical_family', role_name))
        capabilities = [
            item for item in (workstream.get('required_capabilities') or [])
            if isinstance(item, dict) and str(item.get('capability') or '').strip()
        ]
        capability_keys = {slugify_key(item.get('capability', '')) for item in capabilities}
        capability_terms = {
            str(item.get('capability') or '').lower().strip()
            for item in capabilities
            if str(item.get('capability') or '').strip()
        }
        matched_roles = sorted(normalized_roles_needed & role_families)
        matched_capability_keys = capability_keys & role_skill_keys
        matched_capability_terms = capability_terms & role_skill_aliases
        capability_covered_count = len({item for item in matched_capability_keys if item}) + len({item for item in matched_capability_terms if item})
        coverage = {
            'workstream_id': str(workstream.get('id') or ''),
            'workstream_name': str(workstream.get('name') or ''),
            'initiative_id': str(workstream.get('initiative_id') or ''),
            'roles_needed': raw_roles_needed,
            'roles_covered': matched_roles,
            'roles_missing': sorted(normalized_roles_needed - set(matched_roles)),
            'capabilities_needed': len(capabilities),
            'capabilities_covered': capability_covered_count,
            'is_fully_covered': (
                (not normalized_roles_needed or len(matched_roles) == len(normalized_roles_needed))
                and (not capability_keys or capability_covered_count >= max(1, round(len(capability_keys) * 0.7)))
            ),
        }
        workstream_coverage.append(coverage)
        if not coverage['is_fully_covered']:
            uncovered_workstreams.append(coverage)

    uncovered_bundles: list[dict[str, Any]] = []
    for bundle in roadmap.capability_bundles or []:
        if not isinstance(bundle, dict):
            continue
        bundle_role_families = set()
        for family in bundle.get('inferred_role_families') or []:
            normalized = normalize_external_role_title(role_name=str(family or ''), role_family_hint=str(family or ''))
            bundle_role_families.add(normalized.get('canonical_family', str(family or '').strip()))
        if not (bundle_role_families & role_families):
            uncovered_bundles.append(
                {
                    'bundle_id': str(bundle.get('bundle_id') or ''),
                    'capability_name': str(bundle.get('capability_name') or ''),
                    'criticality': str(bundle.get('criticality') or 'medium'),
                    'inferred_role_families': sorted(bundle_role_families),
                    'workstream_ids': list(bundle.get('workstream_ids') or []),
                }
            )

    enabling_role_signals = {
        'platform_sre_engineer': ['infrastructure', 'deployment', 'kubernetes', 'docker', 'ci/cd', 'cloud'],
        'qa_engineer': ['testing', 'quality', 'test automation', 'qa'],
        'data_product_analyst': ['analytics', 'instrumentation', 'metrics', 'dashboards', 'reporting'],
    }
    all_workstream_text = ' '.join(
        f"{item.get('name', '')} {item.get('scope', '')} {' '.join(item.get('affected_systems', []))}"
        for item in (roadmap.workstreams or [])
        if isinstance(item, dict)
    ).lower()
    missing_enabling_roles: list[dict[str, Any]] = []
    for role_family, signals in enabling_role_signals.items():
        if role_family in role_families:
            continue
        hits = [signal for signal in signals if signal in all_workstream_text]
        if hits:
            missing_enabling_roles.append(
                {
                    'role_family': role_family,
                    'evidence_signals': hits,
                    'recommendation': f'Consider adding a {role_family.replace("_", " ")} role.',
                }
            )

    resolved_evidence = list(
        EmployeeSkillEvidence.objects.filter(
            workspace=workspace,
            weight__gt=0,
            skill__resolution_status=Skill.ResolutionStatus.RESOLVED,
        ).select_related('skill', 'employee')
    )
    evidence_available = bool(resolved_evidence)
    employee_skills: defaultdict[str, set[str]] = defaultdict(set)
    for evidence in resolved_evidence:
        employee_skills[evidence.skill.canonical_key].add(evidence.employee.full_name)

    concentration_risks: list[dict[str, Any]] = []
    for bundle in roadmap.capability_bundles or []:
        if not isinstance(bundle, dict) or str(bundle.get('criticality') or '').lower() not in {'high', 'medium'}:
            continue
        for skill_hint in bundle.get('skill_hints') or []:
            skill_key = slugify_key(skill_hint)
            employees_with_skill = sorted(employee_skills.get(skill_key, set()))
            if evidence_available and len(employees_with_skill) <= 1:
                concentration_risks.append(
                    {
                        'capability_bundle': str(bundle.get('capability_name') or ''),
                        'skill': str(skill_hint or ''),
                        'employee_count': len(employees_with_skill),
                        'employees': employees_with_skill,
                        'criticality': str(bundle.get('criticality') or 'medium'),
                        'risk': (
                            'Single-person dependency'
                            if employees_with_skill else
                            'No team member has this skill'
                        ),
                    }
                )

    clarification_suggestions: list[dict[str, Any]] = []
    seen_questions: set[str] = set()
    for workstream in uncovered_workstreams:
        question = (
            f"The workstream '{workstream['workstream_name']}' requires roles "
            f"[{', '.join(workstream['roles_missing'])}] that are not in the current blueprint. "
            'Should these roles be added, or is the work covered by existing roles under different names?'
        )
        if question not in seen_questions:
            seen_questions.add(question)
            clarification_suggestions.append(
                {
                    'scope': 'workstream_coverage',
                    'question': question,
                    'why_it_matters': 'Without clear role coverage, the workstream may be under-staffed.',
                    'affected_workstream': workstream['workstream_id'],
                }
            )
    if evidence_available:
        for risk in concentration_risks[:5]:
            question = (
                f"The capability '{risk['skill']}' (needed for '{risk['capability_bundle']}') has only "
                f"{risk['employee_count']} team member(s) with this skill. Is hiring or upskilling planned for this area?"
            )
            if question not in seen_questions:
                seen_questions.add(question)
                clarification_suggestions.append(
                    {
                        'scope': 'concentration_risk',
                        'question': question,
                        'why_it_matters': 'Single-person dependencies create delivery risk.',
                    }
                )

    total_workstreams = len([item for item in (roadmap.workstreams or []) if isinstance(item, dict)]) or 1
    covered_workstreams = sum(1 for item in workstream_coverage if item['is_fully_covered'])
    coverage_score = round(covered_workstreams / total_workstreams * 100)

    return {
        'workstream_coverage': workstream_coverage,
        'uncovered_workstreams': uncovered_workstreams,
        'uncovered_bundles': uncovered_bundles,
        'missing_enabling_roles': missing_enabling_roles,
        'concentration_risks': concentration_risks,
        'concentration_risk_status': 'computed' if evidence_available else 'not_computed',
        'coverage_score': coverage_score,
        'clarification_suggestions': clarification_suggestions,
    }


def _merge_coverage_analysis_into_payload(
    normalized_payload: dict[str, Any],
    gap_summary: dict[str, Any],
    coverage_analysis: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    merged_gap_summary = {
        **(gap_summary or {}),
        'coverage_analysis': coverage_analysis,
    }
    if not coverage_analysis.get('clarification_suggestions'):
        return normalized_payload, merged_gap_summary

    existing_questions = list(normalized_payload.get('clarification_questions') or [])
    existing_question_texts = {str(item.get('question') or '').strip() for item in existing_questions}
    for suggestion in coverage_analysis['clarification_suggestions']:
        question = str(suggestion.get('question') or '').strip()
        if not question or question in existing_question_texts:
            continue
        existing_question_texts.add(question)
        existing_questions.append(
            {
                'question': question,
                'scope': suggestion.get('scope', 'coverage_analysis'),
                'priority': 'high' if suggestion.get('scope') == 'workstream_coverage' else 'medium',
                'why_it_matters': suggestion.get('why_it_matters', ''),
                'evidence_refs': [],
                'impacted_roles': [],
                'impacted_initiatives': [],
                'status': ClarificationQuestionStatus.OPEN,
                'answer': '',
                'note': '',
            }
        )
    return {
        **normalized_payload,
        'clarification_questions': existing_questions,
    }, merged_gap_summary
