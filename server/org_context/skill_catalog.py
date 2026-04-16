import re
from typing import Any

from company_intake.models import IntakeWorkspace
from django.db.models import Case, IntegerField, Q, Value, When

from .esco_matching import (
    build_occupation_lookup_terms,
    normalize_lookup_key,
    rank_esco_occupation_candidates,
)
from .models import (
    CatalogResolutionReviewItem,
    CatalogReviewStatus,
    CatalogOverrideStatus,
    EscoOccupation,
    EscoSkill,
    EscoSkillLabel,
    OccupationResolutionOverride,
    Skill,
    SkillAlias,
    SkillResolutionOverride,
)

# The old hand-curated example catalog is intentionally retired as the
# canonical source. Keep the symbol for compatibility with existing imports.
CANONICAL_SKILL_LIBRARY: dict[str, dict[str, Any]] = {}

def slugify_key(value: str) -> str:
    return re.sub(r'[^a-z0-9]+', '-', str(value or '').strip().lower()).strip('-') or 'unknown'


def dedupe_strings(values: list[str]) -> list[str]:
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


def _clean_skill_term(term: str) -> str:
    cleaned = re.sub(r'^[\-\*\u2022]+\s*', '', str(term or '').strip())
    return re.sub(r'\s+', ' ', cleaned)


def _esco_identifier_from_uri(concept_uri: str) -> str:
    return str(concept_uri or '').rstrip('/').rsplit('/', 1)[-1]


def _base_canonical_key(*, display_name_en: str, esco_skill: EscoSkill | None = None) -> str:
    base = slugify_key(display_name_en)
    if base and base != 'unknown':
        return base
    if esco_skill is not None:
        return f'esco-{_esco_identifier_from_uri(esco_skill.concept_uri)[:8]}'
    return 'unknown'


def _build_esco_aliases(esco_skill: EscoSkill) -> list[str]:
    values = list(
        EscoSkillLabel.objects.filter(esco_skill=esco_skill)
        .exclude(label__iexact=esco_skill.preferred_label)
        .order_by('label_kind', 'label')
        .values_list('label', flat=True)
    )
    return dedupe_strings(values)


def _record_catalog_review_item_sync(
    *,
    term_kind: str,
    raw_term: str,
    workspace: IntakeWorkspace | None = None,
    metadata: dict[str, Any] | None = None,
) -> None:
    normalized_term = normalize_lookup_key(raw_term)
    if not normalized_term:
        return

    item = (
        CatalogResolutionReviewItem.objects.filter(
        workspace=workspace,
        term_kind=term_kind,
        normalized_term=normalized_term,
    ).order_by('pk').first()
    )
    merged_metadata = dict(metadata or {})
    if item is None:
        CatalogResolutionReviewItem.objects.create(
            workspace=workspace,
            term_kind=term_kind,
            raw_term=str(raw_term or '').strip(),
            normalized_term=normalized_term,
            status=CatalogReviewStatus.OPEN,
            metadata=merged_metadata,
        )
        return

    item.raw_term = str(raw_term or '').strip() or item.raw_term
    item.seen_count = int(item.seen_count or 0) + 1
    item.metadata = {
        **(item.metadata or {}),
        **merged_metadata,
    }
    if item.status != CatalogReviewStatus.RESOLVED:
        item.status = CatalogReviewStatus.OPEN
    item.save(update_fields=['raw_term', 'seen_count', 'metadata', 'status', 'last_seen_at', 'updated_at'])


def _build_skill_override_payload(override: SkillResolutionOverride) -> dict[str, Any]:
    esco_skill = override.esco_skill
    display_name_en = str(
        (getattr(esco_skill, 'preferred_label', '') or override.display_name_en or override.raw_term)
    ).strip()
    return {
        'canonical_key': str(override.canonical_key or _base_canonical_key(display_name_en=display_name_en, esco_skill=esco_skill)).strip(),
        'display_name_en': display_name_en,
        'display_name_ru': str(override.display_name_ru or '').strip(),
        'aliases': dedupe_strings([*(override.aliases or []), override.raw_term]),
        'esco_skill_id': str(getattr(esco_skill, 'pk', '') or '').strip() or None,
        'esco_skill_uri': str(getattr(esco_skill, 'concept_uri', '') or '').strip(),
        'match_source': 'override',
    }


def _find_skill_resolution_override(
    cleaned_term: str,
    *,
    workspace: IntakeWorkspace | None = None,
    statuses: list[str] | tuple[str, ...] | None = None,
) -> SkillResolutionOverride | None:
    normalized_term = normalize_lookup_key(cleaned_term)
    if not normalized_term:
        return None
    if statuses is None:
        statuses = [
            CatalogOverrideStatus.APPROVED,
            CatalogOverrideStatus.REJECTED,
        ]
    queryset = SkillResolutionOverride.objects.filter(
        status__in=statuses,
        normalized_term=normalized_term,
    )
    if workspace is not None:
        queryset = queryset.filter(Q(workspace=workspace) | Q(workspace__isnull=True)).annotate(
            workspace_rank=Case(
                When(workspace=workspace, then=Value(0)),
                default=Value(1),
                output_field=IntegerField(),
            ),
            source_rank=Case(
                When(source='bootstrap_skill_seed', then=Value(1)),
                default=Value(0),
                output_field=IntegerField(),
            ),
        ).order_by('workspace_rank', 'source_rank', '-updated_at', 'pk')
    else:
        queryset = queryset.filter(workspace__isnull=True).annotate(
            source_rank=Case(
                When(source='bootstrap_skill_seed', then=Value(1)),
                default=Value(0),
                output_field=IntegerField(),
            )
        ).order_by('source_rank', '-updated_at', 'pk')
    return queryset.first()


def _find_skill_resolution_override_for_terms(
    terms: list[str],
    *,
    workspace: IntakeWorkspace | None = None,
    statuses: list[str] | tuple[str, ...] | None = None,
) -> SkillResolutionOverride | None:
    for term in dedupe_strings(terms):
        override = _find_skill_resolution_override(
            term,
            workspace=workspace,
            statuses=statuses,
        )
        if override is not None:
            return override
    return None


def _find_matching_esco_skill(*terms: str) -> EscoSkill | None:
    normalized_terms = [
        normalize_lookup_key(term)
        for term in terms
        if normalize_lookup_key(term)
    ]
    if not normalized_terms:
        return None

    label_match = (
        EscoSkillLabel.objects.select_related('esco_skill')
        .filter(
            normalized_label__in=normalized_terms,
            language_code='en',
        )
        .annotate(
            label_rank=Case(
                When(label_kind=EscoSkillLabel.LabelKind.PREFERRED, then=Value(0)),
                When(label_kind=EscoSkillLabel.LabelKind.ALT, then=Value(1)),
                When(label_kind=EscoSkillLabel.LabelKind.HIDDEN, then=Value(2)),
                default=Value(3),
                output_field=IntegerField(),
            ),
            status_rank=Case(
                When(esco_skill__status='released', then=Value(0)),
                default=Value(1),
                output_field=IntegerField(),
            ),
        )
        .order_by('status_rank', 'label_rank', 'esco_skill__preferred_label')
        .first()
    )
    if label_match is not None:
        return label_match.esco_skill

    return (
        EscoSkill.objects.filter(normalized_preferred_label__in=normalized_terms)
        .annotate(
            status_rank=Case(
                When(status='released', then=Value(0)),
                default=Value(1),
                output_field=IntegerField(),
            )
        )
        .order_by('status_rank', 'preferred_label')
        .first()
    )


def _resolve_esco_skill_from_normalized(normalized_skill: dict[str, Any]) -> EscoSkill | None:
    esco_skill_id = normalized_skill.get('esco_skill_id')
    if esco_skill_id:
        skill = EscoSkill.objects.filter(pk=esco_skill_id).first()
        if skill is not None:
            return skill
    concept_uri = str(normalized_skill.get('esco_skill_uri') or '').strip()
    if concept_uri:
        return EscoSkill.objects.filter(concept_uri=concept_uri).first()
    return None


def normalize_skill_seed(
    term: str,
    *,
    workspace: IntakeWorkspace | None = None,
    review_metadata: dict[str, Any] | None = None,
    allow_freeform: bool = True,
) -> dict[str, Any]:
    cleaned = _clean_skill_term(term)
    override = _find_skill_resolution_override(cleaned, workspace=workspace)
    if override is not None:
        if override.status == CatalogOverrideStatus.REJECTED:
            payload = _build_skill_override_payload(override)
            return {
                **payload,
                'is_rejected': True,
                'match_source': 'rejected_override',
                'needs_review': False,
            }
        return _build_skill_override_payload(override)

    esco_skill = _find_matching_esco_skill(cleaned)
    if esco_skill is not None:
        return {
            'canonical_key': _base_canonical_key(display_name_en=esco_skill.preferred_label, esco_skill=esco_skill),
            'display_name_en': esco_skill.preferred_label,
            'display_name_ru': '',
            'aliases': dedupe_strings([cleaned] if cleaned and cleaned.casefold() != esco_skill.preferred_label.casefold() else []),
            'esco_skill_id': str(esco_skill.pk),
            'esco_skill_uri': esco_skill.concept_uri,
            'match_source': 'esco',
        }

    _record_catalog_review_item_sync(
        term_kind=CatalogResolutionReviewItem.TermKind.SKILL,
        raw_term=cleaned,
        workspace=workspace,
        metadata={
            **(review_metadata or {}),
            'allow_freeform': bool(allow_freeform),
        },
    )
    canonical_key = slugify_key(cleaned)
    if not allow_freeform:
        return {
            'canonical_key': canonical_key,
            'display_name_en': cleaned,
            'display_name_ru': '',
            'aliases': [],
            'esco_skill_id': None,
            'esco_skill_uri': '',
            'match_source': 'review_pending',
            'needs_review': True,
        }
    return {
        'canonical_key': canonical_key,
        'display_name_en': cleaned,
        'display_name_ru': '',
        'aliases': [],
        'esco_skill_id': None,
        'esco_skill_uri': '',
        'match_source': 'freeform',
        'needs_review': True,
    }


def merge_skill_aliases_sync(skill: Skill, aliases: list[str]) -> None:
    for alias in dedupe_strings(aliases):
        normalized_alias = str(alias or '').strip()
        if not normalized_alias:
            continue
        SkillAlias.objects.get_or_create(
            skill=skill,
            alias=normalized_alias,
            language_code='ru' if re.search(r'[а-яА-Я]', normalized_alias) else '',
        )


def _choose_workspace_canonical_key(
    workspace: IntakeWorkspace,
    *,
    desired_key: str,
    esco_skill: EscoSkill | None = None,
) -> str:
    base_key = slugify_key(desired_key)
    if not base_key:
        base_key = 'unknown'

    candidate = base_key
    suffix = _esco_identifier_from_uri(esco_skill.concept_uri)[:8] if esco_skill is not None else ''
    sequence = 1
    while True:
        existing = Skill.objects.filter(workspace=workspace, canonical_key=candidate).first()
        if existing is None:
            return candidate
        if esco_skill is not None and existing.esco_skill_id == esco_skill.id:
            return candidate
        if suffix and candidate == base_key:
            candidate = f'{base_key}-{suffix}'
            continue
        sequence += 1
        candidate = f'{base_key}-{sequence}'


def ensure_workspace_skill_sync(
    workspace: IntakeWorkspace,
    *,
    normalized_skill: dict[str, Any],
    preferred_display_name_ru: str = '',
    aliases: list[str] | None = None,
    raw_term: str = '',
    created_source: str = 'catalog_seed',
    promote_aliases: bool = True,
    resolution_status: str = Skill.ResolutionStatus.RESOLVED,
) -> Skill:
    canonical_key = str(normalized_skill.get('canonical_key') or '').strip()
    if not canonical_key:
        raise ValueError('Skill normalization must produce a canonical key.')

    esco_skill = _resolve_esco_skill_from_normalized(normalized_skill)
    if esco_skill is not None:
        existing_by_esco = Skill.objects.filter(workspace=workspace, esco_skill=esco_skill).first()
        skill = existing_by_esco if existing_by_esco is not None else None
    else:
        skill = None

    default_display_name_en = str(
        (getattr(esco_skill, 'preferred_label', '') or normalized_skill.get('display_name_en') or canonical_key)
    ).strip()
    default_display_name_ru = str(preferred_display_name_ru or normalized_skill.get('display_name_ru') or '').strip()
    source_terms = dedupe_strings(
        [
            raw_term,
            default_display_name_en,
            default_display_name_ru,
            *(normalized_skill.get('aliases') or []),
            *(aliases or []),
        ]
    )

    if skill is None:
        skill = Skill.objects.filter(workspace=workspace, canonical_key=canonical_key).first()

    if skill is None:
        resolved_key = _choose_workspace_canonical_key(
            workspace,
            desired_key=canonical_key or default_display_name_en,
            esco_skill=esco_skill,
        )
        metadata = {}
        match_source = str(normalized_skill.get('match_source') or '').strip()
        if match_source and match_source != 'freeform':
            metadata['catalog_match_source'] = match_source
        if esco_skill is not None:
            metadata.update(
                {
                    'esco_skill_uri': esco_skill.concept_uri,
                    'esco_skill_match_source': normalized_skill.get('match_source', 'esco'),
                }
            )
        skill = Skill.objects.create(
            workspace=workspace,
            canonical_key=resolved_key,
            display_name_en=default_display_name_en,
            display_name_ru=default_display_name_ru,
            source=created_source,
            esco_skill=esco_skill,
            metadata=metadata,
            resolution_status=resolution_status,
            is_operator_confirmed=resolution_status == Skill.ResolutionStatus.RESOLVED,
            source_terms=source_terms,
        )
    else:
        update_fields: list[str] = []
        if not skill.display_name_en and default_display_name_en:
            skill.display_name_en = default_display_name_en
            update_fields.append('display_name_en')
        if not skill.display_name_ru and default_display_name_ru:
            skill.display_name_ru = default_display_name_ru
            update_fields.append('display_name_ru')
        merged_metadata = dict(skill.metadata or {})
        match_source = str(normalized_skill.get('match_source') or '').strip()
        if match_source and match_source != 'freeform':
            merged_metadata.setdefault('catalog_match_source', match_source)
        if skill.esco_skill_id is None and esco_skill is not None:
            skill.esco_skill = esco_skill
            merged_metadata.update(
                {
                    'esco_skill_uri': esco_skill.concept_uri,
                    'esco_skill_match_source': normalized_skill.get('match_source', 'esco'),
                }
            )
            skill.metadata = merged_metadata
            update_fields.extend(['esco_skill', 'metadata'])
        elif merged_metadata != (skill.metadata or {}):
            skill.metadata = merged_metadata
            update_fields.append('metadata')
        merged_source_terms = dedupe_strings([*(skill.source_terms or []), *source_terms])
        if merged_source_terms != list(skill.source_terms or []):
            skill.source_terms = merged_source_terms
            update_fields.append('source_terms')
        if update_fields:
            skill.save(update_fields=[*update_fields, 'updated_at'])

    aliases_to_merge = []
    if promote_aliases:
        aliases_to_merge = [
            preferred_display_name_ru,
            *(normalized_skill.get('aliases') or []),
            *(aliases or []),
        ]
        if esco_skill is not None:
            aliases_to_merge.extend(_build_esco_aliases(esco_skill))
    if aliases_to_merge:
        merge_skill_aliases_sync(skill, aliases_to_merge)
    return skill


def resolve_workspace_skill_sync(
    workspace: IntakeWorkspace,
    *,
    raw_term: str,
    normalized_skill: dict[str, Any] | None = None,
    preferred_display_name_ru: str = '',
    aliases: list[str] | None = None,
    created_source: str = 'catalog_seed',
    promote_aliases: bool = True,
    allow_freeform: bool = True,
) -> tuple[Skill | None, dict[str, Any], bool]:
    def with_resolution_state(skill: Skill | None) -> dict[str, Any]:
        if skill is None:
            return normalized_skill
        return {
            **normalized_skill,
            'needs_review': skill.resolution_status == Skill.ResolutionStatus.PENDING_REVIEW,
        }

    if normalized_skill is None:
        normalized_skill = normalize_skill_seed(
            raw_term,
            workspace=workspace,
            review_metadata={
                'preferred_display_name_ru': str(preferred_display_name_ru or '').strip(),
                'aliases': dedupe_strings(list(aliases or [])),
                'created_source': created_source,
            },
            allow_freeform=allow_freeform,
        )
    override = _find_skill_resolution_override_for_terms(
        [
            raw_term,
            preferred_display_name_ru,
            *(aliases or []),
            *(normalized_skill.get('aliases') or []),
            str(normalized_skill.get('display_name_en') or '').strip(),
            str(normalized_skill.get('display_name_ru') or '').strip(),
            str(normalized_skill.get('canonical_key') or '').strip(),
        ],
        workspace=workspace,
    )
    if override is not None:
        if override.status == CatalogOverrideStatus.REJECTED:
            payload = _build_skill_override_payload(override)
            return (
                None,
                {
                    **payload,
                    'is_rejected': True,
                    'match_source': 'rejected_override',
                    'needs_review': False,
                },
                False,
            )
        normalized_skill = {
            **normalized_skill,
            **_build_skill_override_payload(override),
            'needs_review': False,
        }
    esco_skill = _resolve_esco_skill_from_normalized(normalized_skill)
    canonical_key = str(normalized_skill.get('canonical_key') or '').strip()

    if esco_skill is not None:
        existing_by_esco = Skill.objects.filter(workspace=workspace, esco_skill=esco_skill).first()
        if existing_by_esco is not None:
            if promote_aliases:
                merge_skill_aliases_sync(
                    existing_by_esco,
                    [
                        preferred_display_name_ru,
                        *(aliases or []),
                        *(normalized_skill.get('aliases') or []),
                    ],
                )
            return existing_by_esco, with_resolution_state(existing_by_esco), True

    if canonical_key:
        existing_by_key = Skill.objects.filter(workspace=workspace, canonical_key=canonical_key).first()
        if existing_by_key is not None:
            if promote_aliases:
                merge_skill_aliases_sync(
                    existing_by_key,
                    [
                        preferred_display_name_ru,
                        *(aliases or []),
                        *(normalized_skill.get('aliases') or []),
                    ],
                )
            return existing_by_key, with_resolution_state(existing_by_key), True

    alias_candidates = dedupe_strings([
        raw_term,
        preferred_display_name_ru,
        *(aliases or []),
        *(normalized_skill.get('aliases') or []),
    ])
    alias_filter = Q()
    for alias in alias_candidates:
        alias_filter |= Q(alias__iexact=alias)
    if alias_filter:
        existing_alias = SkillAlias.objects.select_related('skill').filter(
            alias_filter,
            skill__workspace=workspace,
        ).first()
        if existing_alias is not None:
            if promote_aliases:
                merge_skill_aliases_sync(
                    existing_alias.skill,
                    [
                        preferred_display_name_ru,
                        *(aliases or []),
                    ],
                )
            return existing_alias.skill, with_resolution_state(existing_alias.skill), True

    if normalized_skill.get('needs_review') and not allow_freeform:
        created_skill = ensure_workspace_skill_sync(
            workspace,
            normalized_skill=normalized_skill,
            preferred_display_name_ru=preferred_display_name_ru,
            aliases=aliases,
            raw_term=raw_term,
            created_source=created_source,
            promote_aliases=promote_aliases,
            resolution_status=Skill.ResolutionStatus.PENDING_REVIEW,
        )
        return created_skill, with_resolution_state(created_skill), True

    created_skill = ensure_workspace_skill_sync(
        workspace,
        normalized_skill=normalized_skill,
        preferred_display_name_ru=preferred_display_name_ru,
        aliases=aliases,
        raw_term=raw_term,
        created_source=created_source,
        promote_aliases=promote_aliases,
    )
    return created_skill, with_resolution_state(created_skill), True


def _find_occupation_resolution_override(
    lookup_terms: list[str],
    *,
    workspace: IntakeWorkspace | None = None,
) -> OccupationResolutionOverride | None:
    normalized_terms = [normalize_lookup_key(term) for term in lookup_terms if normalize_lookup_key(term)]
    if not normalized_terms:
        return None
    queryset = OccupationResolutionOverride.objects.filter(
        status=CatalogOverrideStatus.APPROVED,
        normalized_term__in=normalized_terms,
    )
    if workspace is not None:
        queryset = queryset.filter(Q(workspace=workspace) | Q(workspace__isnull=True)).annotate(
            workspace_rank=Case(
                When(workspace=workspace, then=Value(0)),
                default=Value(1),
                output_field=IntegerField(),
            ),
            source_rank=Case(
                When(source='bootstrap_skill_seed', then=Value(1)),
                default=Value(0),
                output_field=IntegerField(),
            ),
        ).order_by('workspace_rank', 'source_rank', '-updated_at', 'pk')
    else:
        queryset = queryset.filter(workspace__isnull=True).annotate(
            source_rank=Case(
                When(source='bootstrap_skill_seed', then=Value(1)),
                default=Value(0),
                output_field=IntegerField(),
            )
        ).order_by('source_rank', '-updated_at', 'pk')
    return queryset.first()


def resolve_esco_occupation_sync(
    primary_term: str,
    *,
    alternatives: list[str] | None = None,
    workspace: IntakeWorkspace | None = None,
    role_family_hint: str = '',
    review_metadata: dict[str, Any] | None = None,
) -> tuple[EscoOccupation | None, dict[str, Any]]:
    lookup_terms = build_occupation_lookup_terms(
        primary_term,
        alternatives=alternatives,
        role_family_hint=role_family_hint,
        workspace=workspace,
    )
    override = _find_occupation_resolution_override(lookup_terms, workspace=workspace)
    if override is not None and override.esco_occupation is not None:
        occupation = override.esco_occupation
        return occupation, {
            'match_source': 'override',
            'search_terms': lookup_terms,
            'esco_occupation_uri': str(getattr(occupation, 'concept_uri', '') or '').strip(),
            'occupation_name_en': str(getattr(occupation, 'preferred_label', '') or override.occupation_name_en or '').strip(),
            'match_score': 1.0,
            'match_confidence': 'high',
            'candidate_matches': [
                {
                    'occupation_name_en': str(getattr(occupation, 'preferred_label', '') or override.occupation_name_en or '').strip(),
                    'esco_occupation_uri': str(getattr(occupation, 'concept_uri', '') or '').strip(),
                    'score': 1.0,
                    'confidence': 'high',
                    'matched_label': override.raw_term,
                    'matched_label_kind': 'override',
                    'reasons': ['approved override'],
                }
            ],
        }

    candidates = rank_esco_occupation_candidates(
        primary_term,
        alternatives=alternatives,
        role_family_hint=role_family_hint,
        workspace=workspace,
    )
    if not candidates:
        _record_catalog_review_item_sync(
            term_kind=CatalogResolutionReviewItem.TermKind.OCCUPATION,
            raw_term=primary_term,
            workspace=workspace,
            metadata={
                **(review_metadata or {}),
                'alternatives': dedupe_strings(list(alternatives or [])),
                'role_family_hint': role_family_hint,
                'candidate_matches': [],
            },
        )
        return None, {
            'match_source': 'freeform',
            'search_terms': lookup_terms,
            'candidate_matches': [],
        }

    best = candidates[0]
    candidate_payload = [
        {
            'occupation_name_en': candidate.occupation.preferred_label,
            'esco_occupation_uri': candidate.occupation.concept_uri,
            'score': candidate.score,
            'confidence': candidate.confidence,
            'matched_label': candidate.matched_label,
            'matched_label_kind': candidate.matched_label_kind,
            'reasons': candidate.reasons,
        }
        for candidate in candidates[:3]
    ]
    if best.confidence == 'low':
        _record_catalog_review_item_sync(
            term_kind=CatalogResolutionReviewItem.TermKind.OCCUPATION,
            raw_term=primary_term,
            workspace=workspace,
            metadata={
                **(review_metadata or {}),
                'alternatives': dedupe_strings(list(alternatives or [])),
                'role_family_hint': role_family_hint,
                'candidate_matches': candidate_payload,
            },
        )
        return None, {
            'match_source': 'esco_ranked',
            'search_terms': lookup_terms,
            'esco_occupation_uri': '',
            'occupation_name_en': '',
            'match_score': best.score,
            'match_confidence': best.confidence,
            'matched_label': best.matched_label,
            'matched_label_kind': best.matched_label_kind,
            'candidate_matches': candidate_payload,
            'needs_review': True,
            'suggested_esco_occupation_uri': best.occupation.concept_uri,
            'suggested_occupation_name_en': best.occupation.preferred_label,
        }
    return best.occupation, {
        'match_source': 'esco_ranked',
        'search_terms': lookup_terms,
        'esco_occupation_uri': best.occupation.concept_uri,
        'occupation_name_en': best.occupation.preferred_label,
        'match_score': best.score,
        'match_confidence': best.confidence,
        'matched_label': best.matched_label,
        'matched_label_kind': best.matched_label_kind,
        'candidate_matches': candidate_payload,
        'needs_review': best.confidence == 'low',
    }
