from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from django.db.models import Case, IntegerField, Q, Value, When

from .models import (
    CatalogOverrideStatus,
    EscoOccupation,
    EscoOccupationLabel,
    OccupationMapping,
    OccupationResolutionOverride,
)

TITLE_EXPANSIONS: dict[str, str] = {
    'pm': 'product manager',
    'sre': 'site reliability engineer',
    'qa': 'quality assurance engineer',
    'ux': 'user experience designer',
    'ui': 'user interface designer',
}

ROLE_FAMILY_EXPANSIONS: dict[str, list[str]] = {
    'backend_engineer': ['backend software engineer', 'software developer', 'backend developer'],
    'frontend_engineer': ['frontend developer', 'web developer'],
    'fullstack_engineer': ['full stack developer', 'software developer'],
    'mobile_engineer': ['mobile application developer', 'mobile developer', 'software developer'],
    'data_product_analyst': ['data analyst', 'product analyst', 'business analyst'],
    'data_ml_engineer': ['data engineer', 'machine learning engineer', 'ml engineer'],
    'product_manager': ['product manager', 'product owner'],
    'qa_engineer': ['quality assurance engineer', 'test engineer'],
    'platform_sre_engineer': ['devops engineer', 'site reliability engineer', 'systems administrator', 'platform engineer'],
    'engineering_manager': ['engineering manager', 'software development manager'],
    'product_designer': ['ux designer', 'user experience designer', 'product designer', 'ui designer'],
    'growth_product_marketer': ['product marketer', 'product marketing manager', 'growth marketer'],
    'marketing_specialist': ['marketing specialist', 'content manager', 'community manager', 'digital marketing specialist'],
    'business_development_manager': ['business development manager', 'ict business development manager', 'business developer', 'partnership manager'],
    'sales_manager': ['sales manager', 'sales lead', 'account executive', 'business sales executive'],
    'support_manager': ['technical support manager', 'ict help desk manager', 'software support manager', 'customer service manager'],
    'founding_engineer': ['software developer', 'full stack developer', 'startup engineer'],
    'executive_leader': ['chief executive officer', 'chief executive', 'managing director'],
}

ROLE_FAMILY_HINT_ALIASES: dict[str, str] = {
    'business development': 'business_development_manager',
    'marketing': 'marketing_specialist',
    'content': 'marketing_specialist',
    'community': 'marketing_specialist',
    'sales': 'sales_manager',
    'revenue': 'sales_manager',
    'support': 'support_manager',
    'technical support': 'support_manager',
    'customer support': 'support_manager',
    'help desk': 'support_manager',
    'service desk': 'support_manager',
    'leadership': 'executive_leader',
    'executive': 'executive_leader',
    'engineering': 'founding_engineer',
    'development': 'founding_engineer',
}

GENERIC_TOKENS = {
    'a', 'an', 'and', 'for', 'of', 'the', 'to', 'with',
}


@dataclass(frozen=True)
class OccupationMatchCandidate:
    occupation: EscoOccupation
    score: float
    confidence: str
    matched_label: str
    matched_label_kind: str
    matched_term: str
    reasons: list[str]


def clean_occupation_term(term: str) -> str:
    cleaned = re.sub(r'^[\-\*\u2022]+\s*', '', str(term or '').strip())
    cleaned = re.sub(r'[\(\)\[\]]', ' ', cleaned)
    cleaned = re.sub(r'[/,&]+', ' ', cleaned)
    cleaned = re.sub(r'\s+', ' ', cleaned)
    return cleaned.strip()


def normalize_lookup_key(value: str) -> str:
    normalized = re.sub(r'[-_]+', ' ', str(value or '').strip())
    normalized = re.sub(r'[^a-zA-Z0-9\s]+', ' ', normalized)
    return re.sub(r'\s+', ' ', normalized).casefold().strip()


def strip_occupation_modifiers(term: str) -> str:
    cleaned = str(term or '').strip()
    cleaned = re.sub(
        r'\b(senior|staff|principal|lead|head|chief|junior|jr|mid|middle|ii|iii|iv)\b',
        ' ',
        cleaned,
        flags=re.IGNORECASE,
    )
    return re.sub(r'\s+', ' ', cleaned).strip()


def tokenize_lookup_value(value: str) -> list[str]:
    tokens = [
        token
        for token in normalize_lookup_key(value).split()
        if token and token not in GENERIC_TOKENS
    ]
    return tokens


def _role_family_expansion_keys(role_family_hint: str) -> list[str]:
    raw_hint = str(role_family_hint or '').strip()
    normalized_hint = normalize_lookup_key(raw_hint)
    keys: list[str] = []
    for candidate in [
        raw_hint,
        normalized_hint,
        ROLE_FAMILY_HINT_ALIASES.get(normalized_hint, ''),
    ]:
        cleaned = str(candidate or '').strip()
        if cleaned and cleaned not in keys:
            keys.append(cleaned)
    return keys


def build_occupation_lookup_terms(
    primary_term: str,
    *,
    alternatives: list[str] | None = None,
    role_family_hint: str = '',
    workspace=None,
) -> list[str]:
    values: list[str] = []
    for term in [primary_term, *(alternatives or [])]:
        cleaned = clean_occupation_term(term)
        if not cleaned:
            continue
        values.append(cleaned)
        stripped = strip_occupation_modifiers(cleaned)
        if stripped and stripped.casefold() != cleaned.casefold():
            values.append(stripped)
        lowered = normalize_lookup_key(cleaned)
        if lowered in TITLE_EXPANSIONS:
            values.append(TITLE_EXPANSIONS[lowered])

    for key in _role_family_expansion_keys(role_family_hint):
        for expansion in ROLE_FAMILY_EXPANSIONS.get(key, []):
            values.append(expansion)

    override_terms = [
        normalize_lookup_key(term)
        for term in values
        if normalize_lookup_key(term)
    ]
    if override_terms:
        override_qs = OccupationResolutionOverride.objects.filter(
            status=CatalogOverrideStatus.APPROVED,
            normalized_term__in=override_terms,
        ).select_related('esco_occupation')
        if workspace is not None:
            override_qs = override_qs.filter(Q(workspace=workspace) | Q(workspace__isnull=True))
        else:
            override_qs = override_qs.filter(workspace__isnull=True)
        for override in override_qs.order_by('workspace_id', '-updated_at', 'pk')[:12]:
            values.extend(
                [
                    override.raw_term,
                    override.occupation_name_en,
                    *list(override.aliases or []),
                    getattr(override.esco_occupation, 'preferred_label', ''),
                ]
            )

    if role_family_hint:
        history_qs = OccupationMapping.objects.select_related('esco_occupation').filter(role_profile__family=role_family_hint)
        if workspace is not None:
            history_qs = history_qs.filter(workspace=workspace)
        for mapping in history_qs.order_by('-match_score', 'occupation_name_en')[:6]:
            values.extend(
                [
                    mapping.occupation_name_en,
                    getattr(mapping.esco_occupation, 'preferred_label', ''),
                ]
            )

    deduped: list[str] = []
    seen: set[str] = set()
    for value in values:
        normalized = normalize_lookup_key(value)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        deduped.append(re.sub(r'\s+', ' ', str(value or '').strip()))
    return deduped


def _label_kind_rank(label_kind: str) -> int:
    return {
        EscoOccupationLabel.LabelKind.PREFERRED: 0,
        EscoOccupationLabel.LabelKind.ALT: 1,
        EscoOccupationLabel.LabelKind.HIDDEN: 2,
    }.get(str(label_kind or ''), 3)


def _label_match_base(label_kind: str, *, exact: bool) -> float:
    if exact:
        return {
            EscoOccupationLabel.LabelKind.PREFERRED: 0.98,
            EscoOccupationLabel.LabelKind.ALT: 0.93,
            EscoOccupationLabel.LabelKind.HIDDEN: 0.88,
        }.get(str(label_kind or ''), 0.84)
    return {
        EscoOccupationLabel.LabelKind.PREFERRED: 0.72,
        EscoOccupationLabel.LabelKind.ALT: 0.67,
        EscoOccupationLabel.LabelKind.HIDDEN: 0.6,
    }.get(str(label_kind or ''), 0.55)


def _confidence_for_score(score: float) -> str:
    if score >= 0.9:
        return 'high'
    if score >= 0.74:
        return 'medium'
    if score >= 0.56:
        return 'low'
    return 'none'


def _token_overlap_score(query_tokens: set[str], candidate_tokens: set[str]) -> float:
    if not query_tokens or not candidate_tokens:
        return 0.0
    overlap = len(query_tokens & candidate_tokens)
    union = len(query_tokens | candidate_tokens)
    return overlap / max(1, union)


def _family_hint_bonus(candidate_tokens: set[str], role_family_hint: str) -> tuple[float, list[str]]:
    reasons: list[str] = []
    for key in _role_family_expansion_keys(role_family_hint):
        expansions = ROLE_FAMILY_EXPANSIONS.get(key, [])
        for expansion in expansions:
            expansion_tokens = set(tokenize_lookup_value(expansion))
            if expansion_tokens and expansion_tokens <= candidate_tokens:
                reasons.append(f'family hint aligned with "{expansion}"')
                return 0.08, reasons
    return 0.0, reasons


def _collect_candidate_labels(candidate_ids: list[Any]) -> dict[Any, list[tuple[str, str]]]:
    labels_by_occupation: dict[Any, list[tuple[str, str]]] = {}
    for row in EscoOccupationLabel.objects.filter(
        esco_occupation_id__in=candidate_ids,
        language_code='en',
    ).values_list('esco_occupation_id', 'label', 'label_kind'):
        labels_by_occupation.setdefault(row[0], []).append((row[1], row[2]))
    return labels_by_occupation


def rank_esco_occupation_candidates(
    primary_term: str,
    *,
    alternatives: list[str] | None = None,
    role_family_hint: str = '',
    workspace=None,
    limit: int = 5,
) -> list[OccupationMatchCandidate]:
    lookup_terms = build_occupation_lookup_terms(
        primary_term,
        alternatives=alternatives,
        role_family_hint=role_family_hint,
        workspace=workspace,
    )
    normalized_terms = [normalize_lookup_key(term) for term in lookup_terms if normalize_lookup_key(term)]
    if not normalized_terms:
        return []

    query_tokens = {
        token
        for term in lookup_terms
        for token in tokenize_lookup_value(term)
    }

    token_query = Q()
    for token in sorted(query_tokens, key=len, reverse=True)[:6]:
        if len(token) < 3:
            continue
        token_query |= Q(normalized_preferred_label__icontains=token)
        token_query |= Q(labels__normalized_label__icontains=token)

    exact_query = Q(normalized_preferred_label__in=normalized_terms) | Q(labels__normalized_label__in=normalized_terms)
    candidate_filter = exact_query
    if token_query.children:
        candidate_filter = candidate_filter | token_query
    base_queryset = (
        EscoOccupation.objects.filter(candidate_filter)
        .annotate(
            status_rank=Case(
                When(status='released', then=Value(0)),
                default=Value(1),
                output_field=IntegerField(),
            )
        )
        .distinct()
        .order_by('status_rank', 'preferred_label')
    )
    candidate_ids = list(base_queryset.values_list('pk', flat=True)[:60])
    if not candidate_ids:
        return []

    candidates = list(EscoOccupation.objects.filter(pk__in=candidate_ids).order_by('preferred_label'))
    labels_by_occupation = _collect_candidate_labels(candidate_ids)
    ranked: list[OccupationMatchCandidate] = []

    for candidate in candidates:
        candidate_labels = [(candidate.preferred_label, EscoOccupationLabel.LabelKind.PREFERRED)]
        candidate_labels.extend(labels_by_occupation.get(candidate.pk, []))

        best_score = 0.0
        best_label = candidate.preferred_label
        best_label_kind = EscoOccupationLabel.LabelKind.PREFERRED
        best_term = lookup_terms[0]
        reasons: list[str] = []
        candidate_tokens = set(tokenize_lookup_value(candidate.preferred_label))
        family_bonus, family_reasons = _family_hint_bonus(candidate_tokens, role_family_hint)
        for label, label_kind in candidate_labels:
            normalized_label = normalize_lookup_key(label)
            label_tokens = set(tokenize_lookup_value(label))
            overlap_score = _token_overlap_score(query_tokens, label_tokens)
            for original_term in lookup_terms:
                normalized_term = normalize_lookup_key(original_term)
                exact = normalized_label == normalized_term
                contains = normalized_term and (
                    normalized_label.startswith(normalized_term)
                    or normalized_term.startswith(normalized_label)
                    or normalized_term in normalized_label
                )
                label_score = _label_match_base(label_kind, exact=exact)
                if contains and not exact:
                    label_score = max(label_score, 0.78 - (_label_kind_rank(label_kind) * 0.04))
                total_score = min(1.0, label_score + (overlap_score * 0.22) + family_bonus)
                local_reasons = []
                if exact:
                    local_reasons.append(f'exact {label_kind} label match')
                elif contains:
                    local_reasons.append(f'partial {label_kind} label match')
                if overlap_score >= 0.34:
                    local_reasons.append(f'token overlap {round(overlap_score, 2)}')
                local_reasons.extend(family_reasons)
                if total_score > best_score:
                    best_score = total_score
                    best_label = label
                    best_label_kind = label_kind
                    best_term = original_term
                    reasons = local_reasons

        if best_score >= 0.56:
            ranked.append(
                OccupationMatchCandidate(
                    occupation=candidate,
                    score=round(best_score, 2),
                    confidence=_confidence_for_score(best_score),
                    matched_label=best_label,
                    matched_label_kind=best_label_kind,
                    matched_term=best_term,
                    reasons=reasons,
                )
            )

    ranked.sort(
        key=lambda item: (
            -item.score,
            _label_kind_rank(item.matched_label_kind),
            item.occupation.preferred_label,
        )
    )
    return ranked[:limit]
