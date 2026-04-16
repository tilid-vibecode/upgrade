from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from django.core.management.base import BaseCommand, CommandError

from company_intake.models import IntakeWorkspace
from org_context.esco_matching import normalize_lookup_key
from org_context.models import (
    CatalogOverrideStatus,
    EscoOccupation,
    EscoSkill,
    OccupationResolutionOverride,
    SkillResolutionOverride,
)


@dataclass(frozen=True)
class SkillOverrideSeed:
    raw_terms: tuple[str, ...]
    canonical_key: str
    display_name_en: str
    display_name_ru: str = ''
    aliases: tuple[str, ...] = ()
    esco_preferred_label: str = ''
    notes: str = ''


@dataclass(frozen=True)
class OccupationOverrideSeed:
    raw_terms: tuple[str, ...]
    occupation_key: str
    occupation_name_en: str
    aliases: tuple[str, ...] = ()
    esco_preferred_label: str = ''
    notes: str = ''


DEFAULT_SKILL_OVERRIDE_SEEDS: tuple[SkillOverrideSeed, ...] = (
    SkillOverrideSeed(
        raw_terms=('analytics', 'product analytics'),
        canonical_key='product-analytics',
        display_name_en='Product Analytics',
        aliases=('analytics',),
        notes='Local taxonomy extension for handbook/CV terms that do not map cleanly to a single ESCO skill.',
    ),
)

DEFAULT_OCCUPATION_OVERRIDE_SEEDS: tuple[OccupationOverrideSeed, ...] = (
    OccupationOverrideSeed(
        raw_terms=('pm',),
        occupation_key='product-manager',
        occupation_name_en='Product manager',
        aliases=('pm',),
        esco_preferred_label='product manager',
        notes='Common shorthand used in role descriptions and CVs.',
    ),
)


class Command(BaseCommand):
    help = 'Seed a small curated starter set of catalog overrides for non-ESCO or shorthand terms.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--workspace-slug',
            default='',
            help='Optional workspace slug. If omitted, global overrides are created.',
        )
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='Preview changes without writing rows.',
        )

    def handle(self, *args, **options):
        workspace_slug = str(options.get('workspace_slug') or '').strip()
        dry_run = bool(options.get('dry_run'))
        workspace = self._resolve_workspace(workspace_slug)

        skill_created = 0
        skill_updated = 0
        occupation_created = 0
        occupation_updated = 0

        for seed in DEFAULT_SKILL_OVERRIDE_SEEDS:
            created_count, updated_count = self._seed_skill_override(
                seed=seed,
                workspace=workspace,
                dry_run=dry_run,
            )
            skill_created += created_count
            skill_updated += updated_count

        for seed in DEFAULT_OCCUPATION_OVERRIDE_SEEDS:
            created_count, updated_count = self._seed_occupation_override(
                seed=seed,
                workspace=workspace,
                dry_run=dry_run,
            )
            occupation_created += created_count
            occupation_updated += updated_count

        scope_label = workspace.slug if workspace is not None else 'global'
        action_label = 'would seed' if dry_run else 'seeded'
        self.stdout.write(self.style.SUCCESS(f'Catalog bootstrap {action_label} for scope "{scope_label}".'))
        self.stdout.write(f'- skill overrides created: {skill_created}')
        self.stdout.write(f'- skill overrides updated: {skill_updated}')
        self.stdout.write(f'- occupation overrides created: {occupation_created}')
        self.stdout.write(f'- occupation overrides updated: {occupation_updated}')

    def _resolve_workspace(self, workspace_slug: str) -> IntakeWorkspace | None:
        if not workspace_slug:
            return None
        workspace = IntakeWorkspace.objects.filter(slug=workspace_slug).first()
        if workspace is None:
            raise CommandError(f'Workspace "{workspace_slug}" was not found.')
        return workspace

    def _seed_skill_override(
        self,
        *,
        seed: SkillOverrideSeed,
        workspace: IntakeWorkspace | None,
        dry_run: bool,
    ) -> tuple[int, int]:
        if not self._normalized_terms(seed.raw_terms):
            return 0, 0
        esco_skill = self._find_esco_skill(seed.esco_preferred_label)
        aliases = self._dedupe_strings([*seed.aliases, *seed.raw_terms])
        created_count = 0
        updated_count = 0
        for raw_term in seed.raw_terms:
            normalized_term = normalize_lookup_key(raw_term)
            defaults = {
                'raw_term': raw_term,
                'canonical_key': seed.canonical_key,
                'display_name_en': seed.display_name_en,
                'display_name_ru': seed.display_name_ru,
                'esco_skill': esco_skill,
                'aliases': aliases,
                'status': CatalogOverrideStatus.APPROVED,
                'source': 'bootstrap_catalog_resolution',
                'notes': seed.notes,
                'metadata': {
                    'seed_kind': 'default_bootstrap',
                    'workspace_scope': getattr(workspace, 'slug', ''),
                },
            }
            if dry_run:
                exists = SkillResolutionOverride.objects.filter(
                    workspace=workspace,
                    normalized_term=normalized_term,
                ).exists()
                if exists:
                    updated_count += 1
                else:
                    created_count += 1
                continue
            _obj, created = SkillResolutionOverride.objects.update_or_create(
                workspace=workspace,
                normalized_term=normalized_term,
                defaults=defaults,
            )
            if created:
                created_count += 1
            else:
                updated_count += 1
        return created_count, updated_count

    def _seed_occupation_override(
        self,
        *,
        seed: OccupationOverrideSeed,
        workspace: IntakeWorkspace | None,
        dry_run: bool,
    ) -> tuple[int, int]:
        if not self._normalized_terms(seed.raw_terms):
            return 0, 0
        esco_occupation = self._find_esco_occupation(seed.esco_preferred_label)
        aliases = self._dedupe_strings([*seed.aliases, *seed.raw_terms])
        created_count = 0
        updated_count = 0
        for raw_term in seed.raw_terms:
            normalized_term = normalize_lookup_key(raw_term)
            defaults = {
                'raw_term': raw_term,
                'occupation_key': seed.occupation_key,
                'occupation_name_en': seed.occupation_name_en,
                'aliases': aliases,
                'esco_occupation': esco_occupation,
                'status': CatalogOverrideStatus.APPROVED,
                'source': 'bootstrap_catalog_resolution',
                'notes': seed.notes,
                'metadata': {
                    'seed_kind': 'default_bootstrap',
                    'workspace_scope': getattr(workspace, 'slug', ''),
                },
            }
            if dry_run:
                exists = OccupationResolutionOverride.objects.filter(
                    workspace=workspace,
                    normalized_term=normalized_term,
                ).exists()
                if exists:
                    updated_count += 1
                else:
                    created_count += 1
                continue
            _obj, created = OccupationResolutionOverride.objects.update_or_create(
                workspace=workspace,
                normalized_term=normalized_term,
                defaults=defaults,
            )
            if created:
                created_count += 1
            else:
                updated_count += 1
        return created_count, updated_count

    def _find_esco_skill(self, preferred_label: str) -> EscoSkill | None:
        if not preferred_label:
            return None
        return EscoSkill.objects.filter(preferred_label__iexact=preferred_label).order_by('preferred_label').first()

    def _find_esco_occupation(self, preferred_label: str) -> EscoOccupation | None:
        if not preferred_label:
            return None
        return (
            EscoOccupation.objects.filter(preferred_label__iexact=preferred_label)
            .order_by('preferred_label')
            .first()
        )

    def _normalized_terms(self, raw_terms: Iterable[str]) -> list[str]:
        return [term for term in [normalize_lookup_key(item) for item in raw_terms] if term]

    def _dedupe_strings(self, values: Iterable[str]) -> list[str]:
        seen: set[str] = set()
        result: list[str] = []
        for value in values:
            normalized = str(value or '').strip()
            if not normalized:
                continue
            key = normalized.casefold()
            if key in seen:
                continue
            seen.add(key)
            result.append(normalized)
        return result
