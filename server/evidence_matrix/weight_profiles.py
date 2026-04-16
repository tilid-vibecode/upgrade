from __future__ import annotations

from copy import deepcopy
from typing import Any


EXACT_SUPPORT_TYPE = 'exact'
HIERARCHY_PARENT_SUPPORT_TYPE = 'hierarchy_parent'
HIERARCHY_CHILD_SUPPORT_TYPE = 'hierarchy_child'
RELATED_SUPPORT_TYPE = 'related'

NESTED_WEIGHT_PROFILE_KEYS = (
    'level_multipliers',
    'confidence_multipliers',
    'weight_multipliers',
    'occupation_prior_origin_multipliers',
)

DEFAULT_WEIGHT_PROFILE = {
    'level_multipliers': {
        HIERARCHY_CHILD_SUPPORT_TYPE: 0.9,
        HIERARCHY_PARENT_SUPPORT_TYPE: 0.72,
        RELATED_SUPPORT_TYPE: 0.58,
    },
    'confidence_multipliers': {
        HIERARCHY_CHILD_SUPPORT_TYPE: 0.88,
        HIERARCHY_PARENT_SUPPORT_TYPE: 0.76,
        RELATED_SUPPORT_TYPE: 0.65,
    },
    'weight_multipliers': {
        HIERARCHY_CHILD_SUPPORT_TYPE: 0.78,
        HIERARCHY_PARENT_SUPPORT_TYPE: 0.62,
        RELATED_SUPPORT_TYPE: 0.48,
    },
    'occupation_prior_origin_multipliers': {
        'direct': 1.0,
        'ancestor': 0.72,
    },
}

def deep_merge_weight_profile(base: dict[str, Any], override: dict[str, Any] | None) -> dict[str, Any]:
    merged = deepcopy(base)
    for key, value in dict(override or {}).items():
        if key in NESTED_WEIGHT_PROFILE_KEYS:
            merged[key] = {
                **dict(merged.get(key) or {}),
                **dict(value or {}),
            }
            continue
        merged[key] = deepcopy(value)
    for key in NESTED_WEIGHT_PROFILE_KEYS:
        merged[key] = dict(merged.get(key) or {})
    return merged


DEFAULT_WEIGHT_PROFILES = {
    'balanced_v1': deepcopy(DEFAULT_WEIGHT_PROFILE),
    'conservative_v1': deep_merge_weight_profile(
        DEFAULT_WEIGHT_PROFILE,
        {
            'level_multipliers': {
                HIERARCHY_CHILD_SUPPORT_TYPE: 0.84,
                HIERARCHY_PARENT_SUPPORT_TYPE: 0.64,
                RELATED_SUPPORT_TYPE: 0.46,
            },
            'confidence_multipliers': {
                HIERARCHY_CHILD_SUPPORT_TYPE: 0.82,
                HIERARCHY_PARENT_SUPPORT_TYPE: 0.68,
                RELATED_SUPPORT_TYPE: 0.55,
            },
            'weight_multipliers': {
                HIERARCHY_CHILD_SUPPORT_TYPE: 0.70,
                HIERARCHY_PARENT_SUPPORT_TYPE: 0.52,
                RELATED_SUPPORT_TYPE: 0.38,
            },
            'occupation_prior_origin_multipliers': {
                'direct': 0.90,
                'ancestor': 0.58,
            },
        },
    ),
    'exploratory_v1': deep_merge_weight_profile(
        DEFAULT_WEIGHT_PROFILE,
        {
            'level_multipliers': {
                HIERARCHY_CHILD_SUPPORT_TYPE: 0.94,
                HIERARCHY_PARENT_SUPPORT_TYPE: 0.78,
                RELATED_SUPPORT_TYPE: 0.66,
            },
            'confidence_multipliers': {
                HIERARCHY_CHILD_SUPPORT_TYPE: 0.92,
                HIERARCHY_PARENT_SUPPORT_TYPE: 0.81,
                RELATED_SUPPORT_TYPE: 0.71,
            },
            'weight_multipliers': {
                HIERARCHY_CHILD_SUPPORT_TYPE: 0.84,
                HIERARCHY_PARENT_SUPPORT_TYPE: 0.68,
                RELATED_SUPPORT_TYPE: 0.54,
            },
            'occupation_prior_origin_multipliers': {
                'direct': 1.0,
                'ancestor': 0.80,
            },
        },
    ),
}


def build_weight_profiles(config_profiles: dict[str, Any] | None = None) -> dict[str, dict[str, Any]]:
    available: dict[str, dict[str, Any]] = {
        name: deep_merge_weight_profile(DEFAULT_WEIGHT_PROFILE, profile)
        for name, profile in DEFAULT_WEIGHT_PROFILES.items()
    }
    for name, profile in dict(config_profiles or {}).items():
        available[str(name)] = deep_merge_weight_profile(DEFAULT_WEIGHT_PROFILE, dict(profile or {}))
    return available


def resolve_weight_profile_config(config: dict[str, Any] | None = None) -> dict[str, Any]:
    normalized_config = dict(config or {})
    available_profiles = build_weight_profiles(normalized_config.get('WEIGHT_PROFILES') or {})
    requested_key = str(normalized_config.get('ACTIVE_WEIGHT_PROFILE') or 'balanced_v1').strip() or 'balanced_v1'
    active_key = requested_key if requested_key in available_profiles else 'balanced_v1'
    if active_key not in available_profiles:
        fallback_key = next(iter(available_profiles), 'balanced_v1')
        active_key = str(fallback_key)
        available_profiles.setdefault(active_key, deepcopy(DEFAULT_WEIGHT_PROFILE))
    return {
        'requested_key': requested_key,
        'active_key': active_key,
        'active_profile': deepcopy(available_profiles[active_key]),
        'available_profiles': deepcopy(available_profiles),
    }
