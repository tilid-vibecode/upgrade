import csv
import re
import sys
from datetime import datetime, time
from decimal import Decimal
from pathlib import Path
from typing import Iterable

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils.dateparse import parse_date, parse_datetime

from org_context.models import (
    EscoConceptScheme,
    EscoDictionaryEntry,
    EscoGreenOccupationShare,
    EscoImportRun,
    EscoImportStatus,
    EscoIscoGroup,
    EscoOccupation,
    EscoOccupationBroaderRelation,
    EscoOccupationCollectionMembership,
    EscoOccupationLabel,
    EscoOccupationSkillRelation,
    EscoSkill,
    EscoSkillBroaderRelation,
    EscoSkillCollectionMembership,
    EscoSkillGroup,
    EscoSkillHierarchyPath,
    EscoSkillLabel,
    EscoSkillRelation,
)


CORE_FILES = {
    'skills': 'skills_en.csv',
    'occupations': 'occupations_en.csv',
    'skill_groups': 'skillGroups_en.csv',
    'skill_hierarchy': 'skillsHierarchy_en.csv',
    'skill_relations': 'skillSkillRelations_en.csv',
    'occupation_skill_relations': 'occupationSkillRelations_en.csv',
    'skill_broader_relations': 'broaderRelationsSkillPillar_en.csv',
    'occupation_broader_relations': 'broaderRelationsOccPillar_en.csv',
    'concept_schemes': 'conceptSchemes_en.csv',
    'dictionary': 'dictionary_en.csv',
    'isco_groups': 'ISCOGroups_en.csv',
    'green_share': 'greenShareOcc_en.csv',
}

SKILL_COLLECTION_FILES = {
    'digitalSkillsCollection_en.csv': ('digital_skills', 'Digital skills'),
    'digCompSkillsCollection_en.csv': ('digcomp_skills', 'DigComp skills'),
    'greenSkillsCollection_en.csv': ('green_skills', 'Green skills'),
    'transversalSkillsCollection_en.csv': ('transversal_skills', 'Transversal skills'),
    'languageSkillsCollection_en.csv': ('language_skills', 'Language skills'),
    'researchSkillsCollection_en.csv': ('research_skills', 'Research skills'),
}

OCCUPATION_COLLECTION_FILES = {
    'researchOccupationsCollection_en.csv': ('research_occupations', 'Research occupations'),
}

BATCH_SIZE = 1000

csv.field_size_limit(sys.maxsize)


class Command(BaseCommand):
    help = 'Import the checked-in ESCO CSV dataset into the global ESCO catalog tables.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--dataset-dir',
            type=str,
            default='',
            help='Path to the extracted ESCO CSV directory. Defaults to auto-discovery under server/esco.',
        )
        parser.add_argument(
            '--dataset-version',
            type=str,
            default='v1.2.1',
            help='Version label recorded in EscoImportRun.',
        )
        parser.add_argument(
            '--language-code',
            type=str,
            default='en',
            help='Language code recorded in EscoImportRun.',
        )
        parser.add_argument(
            '--no-purge',
            action='store_true',
            help='Do not clear existing ESCO catalog rows before importing.',
        )

    def handle(self, *args, **options):
        dataset_dir = self._resolve_dataset_dir(options['dataset_dir'])
        self._validate_dataset_dir(dataset_dir)
        purge_existing = not bool(options['no_purge'])

        import_run = EscoImportRun.objects.create(
            dataset_version=options['dataset_version'],
            language_code=options['language_code'],
            dataset_path=str(dataset_dir),
            status=EscoImportStatus.RUNNING,
            summary={},
        )

        try:
            if purge_existing:
                self._purge_existing_catalog()

            summary: dict[str, int | str] = {
                'dataset_dir': str(dataset_dir),
            }
            summary.update(self._import_skill_groups(dataset_dir))
            summary.update(self._import_skills(dataset_dir))
            summary.update(self._import_occupations(dataset_dir))
            summary.update(self._import_concept_schemes(dataset_dir))
            summary.update(self._import_dictionary_entries(dataset_dir))
            summary.update(self._import_isco_groups(dataset_dir))
            summary.update(self._import_skill_hierarchy(dataset_dir))
            summary.update(self._import_skill_broader_relations(dataset_dir))
            summary.update(self._import_occupation_broader_relations(dataset_dir))
            summary.update(self._import_skill_relations(dataset_dir))
            summary.update(self._import_occupation_skill_relations(dataset_dir))
            summary.update(self._import_skill_collections(dataset_dir))
            summary.update(self._import_occupation_collections(dataset_dir))
            summary.update(self._import_green_share(dataset_dir))

            import_run.status = EscoImportStatus.COMPLETED
            import_run.summary = summary
            import_run.error_message = ''
            import_run.save(update_fields=['status', 'summary', 'error_message', 'updated_at'])
            self.stdout.write(self.style.SUCCESS('ESCO import completed successfully.'))
            for key in sorted(summary):
                self.stdout.write(f'- {key}: {summary[key]}')
        except Exception as exc:
            import_run.status = EscoImportStatus.FAILED
            import_run.error_message = str(exc)
            import_run.save(update_fields=['status', 'error_message', 'updated_at'])
            raise

    def _resolve_dataset_dir(self, dataset_dir_option: str) -> Path:
        if dataset_dir_option:
            candidate = Path(dataset_dir_option).expanduser()
            if not candidate.is_absolute():
                candidate = (Path.cwd() / candidate).resolve()
            return candidate

        esco_root = settings.BASE_DIR / 'esco'
        if not esco_root.exists():
            raise CommandError(f'ESCO dataset directory not found: {esco_root}')

        matches = sorted(path.parent for path in esco_root.rglob('skills_en.csv'))
        if not matches:
            raise CommandError(f'Could not find skills_en.csv under {esco_root}')
        return matches[0]

    def _validate_dataset_dir(self, dataset_dir: Path) -> None:
        missing = [
            filename for filename in CORE_FILES.values()
            if not (dataset_dir / filename).exists()
        ]
        if missing:
            raise CommandError(
                f'Dataset directory {dataset_dir} is missing required ESCO files: {", ".join(missing)}'
            )

    def _purge_existing_catalog(self) -> None:
        self.stdout.write('Purging existing ESCO catalog tables...')
        with transaction.atomic():
            EscoSkillCollectionMembership.objects.all().delete()
            EscoOccupationCollectionMembership.objects.all().delete()
            EscoOccupationSkillRelation.objects.all().delete()
            EscoSkillRelation.objects.all().delete()
            EscoSkillBroaderRelation.objects.all().delete()
            EscoOccupationBroaderRelation.objects.all().delete()
            EscoSkillLabel.objects.all().delete()
            EscoOccupationLabel.objects.all().delete()
            EscoGreenOccupationShare.objects.all().delete()
            EscoSkillHierarchyPath.objects.all().delete()
            EscoConceptScheme.objects.all().delete()
            EscoDictionaryEntry.objects.all().delete()
            EscoSkill.objects.all().delete()
            EscoSkillGroup.objects.all().delete()
            EscoOccupation.objects.all().delete()
            EscoIscoGroup.objects.all().delete()

    def _import_skill_groups(self, dataset_dir: Path) -> dict[str, int]:
        path = dataset_dir / CORE_FILES['skill_groups']
        self.stdout.write(f'Importing skill groups from {path.name}...')
        groups: list[EscoSkillGroup] = []
        for row in self._read_csv(path):
            groups.append(
                EscoSkillGroup(
                    concept_uri=row.get('conceptUri', ''),
                    concept_type=row.get('conceptType', ''),
                    preferred_label=row.get('preferredLabel', ''),
                    status=row.get('status', ''),
                    modified_date=self._parse_datetime(row.get('modifiedDate', '')),
                    scope_note=row.get('scopeNote', ''),
                    in_scheme=self._split_multivalue(row.get('inScheme', '')),
                    description=row.get('description', ''),
                    code=row.get('code', ''),
                    metadata={},
                )
            )
        EscoSkillGroup.objects.bulk_create(groups, batch_size=BATCH_SIZE)
        return {'skill_group_count': len(groups)}

    def _import_skills(self, dataset_dir: Path) -> dict[str, int]:
        path = dataset_dir / CORE_FILES['skills']
        self.stdout.write(f'Importing skills from {path.name}...')
        merged_rows = self._dedupe_rows_by_key(
            self._read_csv(path),
            key_field='conceptUri',
            multivalue_fields={'altLabels', 'hiddenLabels', 'inScheme'},
        )
        skills: list[EscoSkill] = []
        labels: list[EscoSkillLabel] = []
        for row in merged_rows:
            concept_uri = row.get('conceptUri', '')
            preferred_label = row.get('preferredLabel', '')
            skills.append(
                EscoSkill(
                    concept_uri=concept_uri,
                    concept_type=row.get('conceptType', ''),
                    skill_type=row.get('skillType', ''),
                    reuse_level=row.get('reuseLevel', ''),
                    preferred_label=preferred_label,
                    normalized_preferred_label=self._normalize_label(preferred_label),
                    status=row.get('status', ''),
                    modified_date=self._parse_datetime(row.get('modifiedDate', '')),
                    scope_note=row.get('scopeNote', ''),
                    definition=row.get('definition', ''),
                    description=row.get('description', ''),
                    in_scheme=self._split_multivalue(row.get('inScheme', '')),
                    metadata={},
                )
            )
        EscoSkill.objects.bulk_create(skills, batch_size=BATCH_SIZE)
        skill_lookup = EscoSkill.objects.in_bulk(field_name='concept_uri')

        for row in merged_rows:
            skill = skill_lookup.get(row.get('conceptUri', ''))
            if skill is None:
                continue
            labels.extend(self._build_skill_labels(skill, row))
        EscoSkillLabel.objects.bulk_create(labels, batch_size=BATCH_SIZE)
        return {
            'esco_skill_count': len(skills),
            'esco_skill_label_count': len(labels),
        }

    def _import_occupations(self, dataset_dir: Path) -> dict[str, int]:
        path = dataset_dir / CORE_FILES['occupations']
        self.stdout.write(f'Importing occupations from {path.name}...')
        merged_rows = self._dedupe_rows_by_key(
            self._read_csv(path),
            key_field='conceptUri',
            multivalue_fields={'altLabels', 'hiddenLabels', 'inScheme'},
        )
        occupations: list[EscoOccupation] = []
        labels: list[EscoOccupationLabel] = []
        for row in merged_rows:
            concept_uri = row.get('conceptUri', '')
            preferred_label = row.get('preferredLabel', '')
            occupations.append(
                EscoOccupation(
                    concept_uri=concept_uri,
                    concept_type=row.get('conceptType', ''),
                    isco_group=row.get('iscoGroup', ''),
                    preferred_label=preferred_label,
                    normalized_preferred_label=self._normalize_label(preferred_label),
                    status=row.get('status', ''),
                    modified_date=self._parse_datetime(row.get('modifiedDate', '')),
                    regulated_profession_note=row.get('regulatedProfessionNote', ''),
                    scope_note=row.get('scopeNote', ''),
                    definition=row.get('definition', ''),
                    description=row.get('description', ''),
                    in_scheme=self._split_multivalue(row.get('inScheme', '')),
                    code=row.get('code', ''),
                    nace_code='\n'.join(self._split_multivalue(row.get('naceCode', ''))),
                    metadata={},
                )
            )
        EscoOccupation.objects.bulk_create(occupations, batch_size=BATCH_SIZE)
        occupation_lookup = EscoOccupation.objects.in_bulk(field_name='concept_uri')

        for row in merged_rows:
            occupation = occupation_lookup.get(row.get('conceptUri', ''))
            if occupation is None:
                continue
            labels.extend(self._build_occupation_labels(occupation, row))
        EscoOccupationLabel.objects.bulk_create(labels, batch_size=BATCH_SIZE)
        return {
            'esco_occupation_count': len(occupations),
            'esco_occupation_label_count': len(labels),
        }

    def _import_concept_schemes(self, dataset_dir: Path) -> dict[str, int]:
        path = dataset_dir / CORE_FILES['concept_schemes']
        self.stdout.write(f'Importing concept schemes from {path.name}...')
        rows = [
            EscoConceptScheme(
                concept_type=row.get('conceptType', ''),
                concept_scheme_uri=row.get('conceptSchemeUri', ''),
                preferred_label=row.get('preferredLabel', ''),
                title=row.get('title', ''),
                status=row.get('status', ''),
                description=row.get('description', ''),
                has_top_concept=self._split_multivalue(row.get('hasTopConcept', '')),
            )
            for row in self._read_csv(path)
        ]
        EscoConceptScheme.objects.bulk_create(rows, batch_size=BATCH_SIZE)
        return {'esco_concept_scheme_count': len(rows)}

    def _import_dictionary_entries(self, dataset_dir: Path) -> dict[str, int]:
        path = dataset_dir / CORE_FILES['dictionary']
        self.stdout.write(f'Importing dictionary entries from {path.name}...')
        rows = [
            EscoDictionaryEntry(
                filename=row.get('filename', ''),
                data_header=row.get('data header', ''),
                property_name=row.get('property', ''),
                description=row.get('description', ''),
            )
            for row in self._read_csv(path)
        ]
        EscoDictionaryEntry.objects.bulk_create(rows, batch_size=BATCH_SIZE)
        return {'esco_dictionary_entry_count': len(rows)}

    def _import_isco_groups(self, dataset_dir: Path) -> dict[str, int]:
        path = dataset_dir / CORE_FILES['isco_groups']
        self.stdout.write(f'Importing ISCO groups from {path.name}...')
        rows = [
            EscoIscoGroup(
                concept_type=row.get('conceptType', ''),
                concept_uri=row.get('conceptUri', ''),
                code=row.get('code', ''),
                preferred_label=row.get('preferredLabel', ''),
                status=row.get('status', ''),
                in_scheme=self._split_multivalue(row.get('inScheme', '')),
                description=row.get('description', ''),
                metadata={'alt_labels': self._split_multivalue(row.get('altLabels', ''))},
            )
            for row in self._read_csv(path)
        ]
        EscoIscoGroup.objects.bulk_create(rows, batch_size=BATCH_SIZE)
        return {'esco_isco_group_count': len(rows)}

    def _import_skill_hierarchy(self, dataset_dir: Path) -> dict[str, int]:
        path = dataset_dir / CORE_FILES['skill_hierarchy']
        self.stdout.write(f'Importing skill hierarchy breadcrumbs from {path.name}...')
        rows = [
            EscoSkillHierarchyPath(
                level_0_uri=row.get('Level 0 URI', ''),
                level_0_preferred_term=row.get('Level 0 preferred term', ''),
                level_1_uri=row.get('Level 1 URI', ''),
                level_1_preferred_term=row.get('Level 1 preferred term', ''),
                level_2_uri=row.get('Level 2 URI', ''),
                level_2_preferred_term=row.get('Level 2 preferred term', ''),
                level_3_uri=row.get('Level 3 URI', ''),
                level_3_preferred_term=row.get('Level 3 preferred term', ''),
                description=row.get('Description', ''),
                scope_note=row.get('Scope note', ''),
                level_0_code=row.get('Level 0 code', ''),
                level_1_code=row.get('Level 1 code', ''),
                level_2_code=row.get('Level 2 code', ''),
                level_3_code=row.get('Level 3 code', ''),
            )
            for row in self._read_csv(path)
        ]
        EscoSkillHierarchyPath.objects.bulk_create(rows, batch_size=BATCH_SIZE)
        return {'esco_skill_hierarchy_path_count': len(rows)}

    def _import_skill_broader_relations(self, dataset_dir: Path) -> dict[str, int]:
        path = dataset_dir / CORE_FILES['skill_broader_relations']
        self.stdout.write(f'Importing skill broader relations from {path.name}...')
        skill_lookup = EscoSkill.objects.in_bulk(field_name='concept_uri')
        group_lookup = EscoSkillGroup.objects.in_bulk(field_name='concept_uri')
        rows: list[EscoSkillBroaderRelation] = []
        for row in self._read_csv(path):
            concept_uri = row.get('conceptUri', '')
            broader_uri = row.get('broaderUri', '')
            rows.append(
                EscoSkillBroaderRelation(
                    concept_type=row.get('conceptType', ''),
                    concept_uri=concept_uri,
                    concept_label=row.get('conceptLabel', ''),
                    broader_type=row.get('broaderType', ''),
                    broader_uri=broader_uri,
                    broader_label=row.get('broaderLabel', ''),
                    esco_skill=skill_lookup.get(concept_uri),
                    esco_skill_group=group_lookup.get(concept_uri),
                    broader_skill=skill_lookup.get(broader_uri),
                    broader_skill_group=group_lookup.get(broader_uri),
                )
            )
        EscoSkillBroaderRelation.objects.bulk_create(rows, batch_size=BATCH_SIZE)
        return {'esco_skill_broader_relation_count': len(rows)}

    def _import_occupation_broader_relations(self, dataset_dir: Path) -> dict[str, int]:
        path = dataset_dir / CORE_FILES['occupation_broader_relations']
        self.stdout.write(f'Importing occupation broader relations from {path.name}...')
        occupation_lookup = EscoOccupation.objects.in_bulk(field_name='concept_uri')
        rows = [
            EscoOccupationBroaderRelation(
                concept_type=row.get('conceptType', ''),
                concept_uri=row.get('conceptUri', ''),
                concept_label=row.get('conceptLabel', ''),
                broader_type=row.get('broaderType', ''),
                broader_uri=row.get('broaderUri', ''),
                broader_label=row.get('broaderLabel', ''),
                esco_occupation=occupation_lookup.get(row.get('conceptUri', '')),
                broader_occupation=occupation_lookup.get(row.get('broaderUri', '')),
            )
            for row in self._read_csv(path)
        ]
        EscoOccupationBroaderRelation.objects.bulk_create(rows, batch_size=BATCH_SIZE)
        return {'esco_occupation_broader_relation_count': len(rows)}

    def _import_skill_relations(self, dataset_dir: Path) -> dict[str, int]:
        path = dataset_dir / CORE_FILES['skill_relations']
        self.stdout.write(f'Importing skill-to-skill relations from {path.name}...')
        skill_lookup = EscoSkill.objects.in_bulk(field_name='concept_uri')
        rows: list[EscoSkillRelation] = []
        for row in self._read_csv(path):
            original_skill = skill_lookup.get(row.get('originalSkillUri', ''))
            related_skill = skill_lookup.get(row.get('relatedSkillUri', ''))
            if original_skill is None or related_skill is None:
                continue
            rows.append(
                EscoSkillRelation(
                    original_skill=original_skill,
                    related_skill=related_skill,
                    original_skill_type=row.get('originalSkillType', ''),
                    relation_type=row.get('relationType', ''),
                    related_skill_type=row.get('relatedSkillType', ''),
                    metadata={},
                )
            )
        EscoSkillRelation.objects.bulk_create(rows, batch_size=BATCH_SIZE)
        return {'esco_skill_relation_count': len(rows)}

    def _import_occupation_skill_relations(self, dataset_dir: Path) -> dict[str, int]:
        path = dataset_dir / CORE_FILES['occupation_skill_relations']
        self.stdout.write(f'Importing occupation-to-skill relations from {path.name}...')
        occupation_lookup = EscoOccupation.objects.in_bulk(field_name='concept_uri')
        skill_lookup = EscoSkill.objects.in_bulk(field_name='concept_uri')
        rows: list[EscoOccupationSkillRelation] = []
        for row in self._read_csv(path):
            occupation = occupation_lookup.get(row.get('occupationUri', ''))
            skill = skill_lookup.get(row.get('skillUri', ''))
            if occupation is None or skill is None:
                continue
            rows.append(
                EscoOccupationSkillRelation(
                    occupation=occupation,
                    skill=skill,
                    relation_type=row.get('relationType', ''),
                    skill_type=row.get('skillType', ''),
                    metadata={
                        'occupation_label': row.get('occupationLabel', ''),
                        'skill_label': row.get('skillLabel', ''),
                    },
                )
            )
        EscoOccupationSkillRelation.objects.bulk_create(rows, batch_size=BATCH_SIZE)
        return {'esco_occupation_skill_relation_count': len(rows)}

    def _import_skill_collections(self, dataset_dir: Path) -> dict[str, int]:
        skill_lookup = EscoSkill.objects.in_bulk(field_name='concept_uri')
        memberships: list[EscoSkillCollectionMembership] = []
        for filename, (collection_key, collection_label) in SKILL_COLLECTION_FILES.items():
            path = dataset_dir / filename
            if not path.exists():
                continue
            self.stdout.write(f'Importing skill collection {filename}...')
            for row in self._read_csv(path):
                skill = skill_lookup.get(row.get('conceptUri', ''))
                if skill is None:
                    continue
                memberships.append(
                    EscoSkillCollectionMembership(
                        esco_skill=skill,
                        collection_key=collection_key,
                        collection_label=collection_label,
                        broader_concept_uris=self._split_multivalue(row.get('broaderConceptUri', ''), include_pipe=True),
                        broader_concept_labels=self._split_multivalue(row.get('broaderConceptPT', ''), include_pipe=True),
                        metadata={
                            'status': row.get('status', ''),
                            'skill_type': row.get('skillType', ''),
                            'reuse_level': row.get('reuseLevel', ''),
                            'description': row.get('description', ''),
                        },
                    )
                )
        EscoSkillCollectionMembership.objects.bulk_create(memberships, batch_size=BATCH_SIZE)
        return {'esco_skill_collection_membership_count': len(memberships)}

    def _import_occupation_collections(self, dataset_dir: Path) -> dict[str, int]:
        occupation_lookup = EscoOccupation.objects.in_bulk(field_name='concept_uri')
        memberships: list[EscoOccupationCollectionMembership] = []
        for filename, (collection_key, collection_label) in OCCUPATION_COLLECTION_FILES.items():
            path = dataset_dir / filename
            if not path.exists():
                continue
            self.stdout.write(f'Importing occupation collection {filename}...')
            for row in self._read_csv(path):
                occupation = occupation_lookup.get(row.get('conceptUri', ''))
                if occupation is None:
                    continue
                memberships.append(
                    EscoOccupationCollectionMembership(
                        esco_occupation=occupation,
                        collection_key=collection_key,
                        collection_label=collection_label,
                        broader_concept_uris=self._split_multivalue(row.get('broaderConceptUri', ''), include_pipe=True),
                        broader_concept_labels=self._split_multivalue(row.get('broaderConceptPT', ''), include_pipe=True),
                        metadata={
                            'status': row.get('status', ''),
                            'description': row.get('description', ''),
                        },
                    )
                )
        EscoOccupationCollectionMembership.objects.bulk_create(memberships, batch_size=BATCH_SIZE)
        return {'esco_occupation_collection_membership_count': len(memberships)}

    def _import_green_share(self, dataset_dir: Path) -> dict[str, int]:
        path = dataset_dir / CORE_FILES['green_share']
        self.stdout.write(f'Importing green occupation share rows from {path.name}...')
        occupation_lookup = EscoOccupation.objects.in_bulk(field_name='concept_uri')
        isco_group_lookup = EscoIscoGroup.objects.in_bulk(field_name='concept_uri')
        rows: list[EscoGreenOccupationShare] = []
        for row in self._read_csv(path):
            concept_uri = row.get('conceptUri', '')
            raw_share = str(row.get('greenShare', '') or '0').strip()
            rows.append(
                EscoGreenOccupationShare(
                    concept_type=row.get('conceptType', ''),
                    concept_uri=concept_uri,
                    code=row.get('code', ''),
                    preferred_label=row.get('preferredLabel', ''),
                    green_share=Decimal(raw_share or '0'),
                    esco_occupation=occupation_lookup.get(concept_uri),
                    isco_group=isco_group_lookup.get(concept_uri),
                )
            )
        EscoGreenOccupationShare.objects.bulk_create(rows, batch_size=BATCH_SIZE)
        return {'esco_green_occupation_share_count': len(rows)}

    def _build_skill_labels(self, skill: EscoSkill, row: dict[str, str]) -> list[EscoSkillLabel]:
        labels = [
            EscoSkillLabel(
                esco_skill=skill,
                label=skill.preferred_label,
                normalized_label=self._normalize_label(skill.preferred_label),
                label_kind=EscoSkillLabel.LabelKind.PREFERRED,
                language_code='en',
            )
        ]
        for value in self._split_multivalue(row.get('altLabels', '')):
            labels.append(
                EscoSkillLabel(
                    esco_skill=skill,
                    label=value,
                    normalized_label=self._normalize_label(value),
                    label_kind=EscoSkillLabel.LabelKind.ALT,
                    language_code='en',
                )
            )
        for value in self._split_multivalue(row.get('hiddenLabels', '')):
            labels.append(
                EscoSkillLabel(
                    esco_skill=skill,
                    label=value,
                    normalized_label=self._normalize_label(value),
                    label_kind=EscoSkillLabel.LabelKind.HIDDEN,
                    language_code='en',
                )
            )
        return labels

    def _build_occupation_labels(self, occupation: EscoOccupation, row: dict[str, str]) -> list[EscoOccupationLabel]:
        labels = [
            EscoOccupationLabel(
                esco_occupation=occupation,
                label=occupation.preferred_label,
                normalized_label=self._normalize_label(occupation.preferred_label),
                label_kind=EscoOccupationLabel.LabelKind.PREFERRED,
                language_code='en',
            )
        ]
        for value in self._split_multivalue(row.get('altLabels', '')):
            labels.append(
                EscoOccupationLabel(
                    esco_occupation=occupation,
                    label=value,
                    normalized_label=self._normalize_label(value),
                    label_kind=EscoOccupationLabel.LabelKind.ALT,
                    language_code='en',
                )
            )
        for value in self._split_multivalue(row.get('hiddenLabels', '')):
            labels.append(
                EscoOccupationLabel(
                    esco_occupation=occupation,
                    label=value,
                    normalized_label=self._normalize_label(value),
                    label_kind=EscoOccupationLabel.LabelKind.HIDDEN,
                    language_code='en',
                )
            )
        return labels

    def _dedupe_rows_by_key(
        self,
        rows: Iterable[dict[str, str]],
        *,
        key_field: str,
        multivalue_fields: set[str],
    ) -> list[dict[str, str]]:
        merged: dict[str, dict[str, str]] = {}
        for row in rows:
            key = str(row.get(key_field, '') or '').strip()
            if not key:
                continue
            existing = merged.get(key)
            if existing is None:
                merged[key] = dict(row)
                continue
            merged[key] = self._merge_row_dicts(
                existing,
                row,
                multivalue_fields=multivalue_fields,
            )
        return list(merged.values())

    def _merge_row_dicts(
        self,
        existing: dict[str, str],
        incoming: dict[str, str],
        *,
        multivalue_fields: set[str],
    ) -> dict[str, str]:
        merged = dict(existing)
        for field, incoming_value in incoming.items():
            if field in multivalue_fields:
                merged[field] = '\n'.join(
                    self._merge_multivalue_texts(
                        merged.get(field, ''),
                        incoming_value,
                    )
                )
                continue
            if not str(merged.get(field, '') or '').strip() and str(incoming_value or '').strip():
                merged[field] = incoming_value
        return merged

    def _merge_multivalue_texts(self, *values: str) -> list[str]:
        combined: list[str] = []
        for value in values:
            combined.extend(self._split_multivalue(value))
        return dedupe_preserve_order(combined)

    def _read_csv(self, path: Path) -> Iterable[dict[str, str]]:
        with path.open('r', encoding='utf-8-sig', newline='') as handle:
            reader = csv.DictReader(handle)
            for row in reader:
                yield {str(key or '').strip(): str(value or '').strip() for key, value in row.items()}

    def _split_multivalue(self, value: str, *, include_pipe: bool = False) -> list[str]:
        text = str(value or '').replace('\r\n', '\n').replace('\r', '\n')
        if not text.strip():
            return []
        parts = [text]
        separators = ['\n']
        if include_pipe:
            separators.append('|')
        for separator in separators:
            next_parts: list[str] = []
            for part in parts:
                next_parts.extend(part.split(separator))
            parts = next_parts
        seen: set[str] = set()
        result: list[str] = []
        for part in parts:
            cleaned = re.sub(r'\s+', ' ', str(part or '').strip())
            if not cleaned:
                continue
            key = cleaned.casefold()
            if key in seen:
                continue
            seen.add(key)
            result.append(cleaned)
        return result

    def _normalize_label(self, value: str) -> str:
        return re.sub(r'\s+', ' ', str(value or '').strip()).casefold()

    def _parse_datetime(self, value: str):
        text = str(value or '').strip()
        if not text:
            return None
        parsed = parse_datetime(text)
        if parsed is not None:
            return parsed
        parsed_date = parse_date(text)
        if parsed_date is not None:
            return datetime.combine(parsed_date, time.min)
        return None


def dedupe_preserve_order(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        cleaned = re.sub(r'\s+', ' ', str(value or '').strip())
        if not cleaned:
            continue
        key = cleaned.casefold()
        if key in seen:
            continue
        seen.add(key)
        result.append(cleaned)
    return result
