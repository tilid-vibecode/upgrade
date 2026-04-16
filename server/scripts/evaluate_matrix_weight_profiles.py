#!/usr/bin/env python3
import json
import sys
from pathlib import Path

from evidence_matrix.weight_profiles import build_weight_profiles


READY_GAP_THRESHOLD = 0.5
LOW_CONFIDENCE_THRESHOLD = 0.55
EXACT_SUPPORT_TYPE = 'exact'


def _adjust_signal(signal: dict, profile: dict) -> dict:
    support_type = str(signal.get('support_type') or '')
    raw_current_level = float(signal.get('raw_current_level') or signal.get('current_level') or 0.0)
    raw_confidence = float(signal.get('raw_confidence') or signal.get('confidence') or 0.0)
    raw_weight = float(signal.get('raw_weight') or signal.get('weight') or 0.0)
    raw_base_current_level = float(signal.get('raw_base_current_level') or 0.0)
    raw_base_confidence = float(signal.get('raw_base_confidence') or 0.0)
    raw_base_weight = float(signal.get('raw_base_weight') or 0.0)

    level_multiplier = 1.0
    confidence_multiplier = 1.0
    weight_multiplier = 1.0
    if support_type != EXACT_SUPPORT_TYPE:
        level_multiplier = float((profile.get('level_multipliers') or {}).get(support_type, 1.0))
        confidence_multiplier = float((profile.get('confidence_multipliers') or {}).get(support_type, 1.0))
        weight_multiplier = float((profile.get('weight_multipliers') or {}).get(support_type, 1.0))
    current_level = raw_current_level
    confidence = raw_confidence
    weight = raw_weight
    if support_type == 'occupation_prior':
        origin = str(signal.get('prior_origin') or 'direct')
        origin_multiplier = float(
            (profile.get('occupation_prior_origin_multipliers') or {}).get(origin, 1.0)
        )
        if raw_base_current_level or raw_base_weight:
            current_level = raw_base_current_level or raw_current_level
            confidence = raw_base_confidence or raw_confidence
            weight = raw_base_weight or raw_weight
            level_multiplier *= origin_multiplier
            weight_multiplier *= origin_multiplier

    return {
        'source_kind': str(signal.get('source_kind') or ''),
        'current_level': round(current_level * level_multiplier, 2),
        'confidence': round(min(1.0, confidence * confidence_multiplier), 2),
        'weight': round(weight * weight_multiplier, 2),
    }


def _weighted_level(signals: list[dict]) -> float:
    total_weight = sum(float(item.get('weight') or 0.0) for item in signals)
    if total_weight <= 0:
        return 0.0
    weighted_sum = sum(float(item.get('current_level') or 0.0) * float(item.get('weight') or 0.0) for item in signals)
    return round(weighted_sum / total_weight, 2)


def _weighted_confidence(signals: list[dict]) -> float:
    total_weight = sum(float(item.get('weight') or 0.0) for item in signals)
    if total_weight <= 0:
        return 0.0
    weighted_sum = sum(float(item.get('confidence') or 0.0) * float(item.get('weight') or 0.0) for item in signals)
    return round(weighted_sum / total_weight, 2)


def _evidence_mass(signals: list[dict]) -> float:
    return round(sum(float(item.get('weight') or 0.0) for item in signals), 2)


def _fused_confidence(weighted_confidence: float, evidence_mass: float, source_diversity: int) -> float:
    diversity_score = min(1.0, max(0.0, source_diversity) / 2.0)
    confidence = (
        float(weighted_confidence or 0.0) * 0.65
        + min(1.0, float(evidence_mass or 0.0)) * 0.25
        + diversity_score * 0.10
    )
    return round(min(1.0, confidence), 2)


def evaluate_profile(dataset: dict, profile_name: str, profile: dict) -> dict:
    evaluated_cells = []
    labeled_ready_total = 0
    labeled_ready_correct = 0
    for cell in list(dataset.get('cells') or []):
        adjusted = [_adjust_signal(signal, profile) for signal in list(cell.get('support_signals') or [])]
        current_level = _weighted_level(adjusted)
        weighted_confidence = _weighted_confidence(adjusted)
        evidence_mass = _evidence_mass(adjusted)
        source_diversity = len({item.get('source_kind') for item in adjusted if item.get('source_kind') and item.get('source_kind') != 'occupation_prior'})
        confidence = _fused_confidence(weighted_confidence, evidence_mass, source_diversity)
        gap = round(max(0.0, float(cell.get('target_level') or 0) - current_level), 2)
        predicted_ready = gap <= READY_GAP_THRESHOLD and confidence >= LOW_CONFIDENCE_THRESHOLD
        labeled_ready = (cell.get('review_labels') or {}).get('ready')
        if labeled_ready is not None:
            labeled_ready_total += 1
            if bool(labeled_ready) == predicted_ready:
                labeled_ready_correct += 1
        evaluated_cells.append(
            {
                'employee_uuid': cell.get('employee_uuid', ''),
                'role_name': cell.get('role_name', ''),
                'skill_key': cell.get('skill_key', ''),
                'predicted_current_level': current_level,
                'predicted_gap': gap,
                'predicted_confidence': confidence,
                'predicted_ready': predicted_ready,
            }
        )

    return {
        'profile': profile_name,
        'cell_count': len(evaluated_cells),
        'average_gap': round(sum(item['predicted_gap'] for item in evaluated_cells) / max(1, len(evaluated_cells)), 2),
        'average_confidence': round(sum(item['predicted_confidence'] for item in evaluated_cells) / max(1, len(evaluated_cells)), 2),
        'ready_cell_count': sum(1 for item in evaluated_cells if item['predicted_ready']),
        'labeled_ready_accuracy': (
            round(labeled_ready_correct / labeled_ready_total, 3) if labeled_ready_total else None
        ),
    }


def main() -> int:
    if len(sys.argv) != 2:
        print('Usage: evaluate_matrix_weight_profiles.py <calibration_dataset.json>')
        return 1

    dataset_path = Path(sys.argv[1]).expanduser().resolve()
    dataset = json.loads(dataset_path.read_text(encoding='utf-8'))
    snapshot = dict(dataset.get('input_snapshot') or {})
    weight_profiles = dict(snapshot.get('available_weight_profiles') or {}) or build_weight_profiles()
    results = [
        evaluate_profile(dataset, name, profile)
        for name, profile in weight_profiles.items()
    ]
    print(json.dumps({'dataset': str(dataset_path), 'results': results}, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
