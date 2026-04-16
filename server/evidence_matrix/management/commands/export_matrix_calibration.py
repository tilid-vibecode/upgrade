import json
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from company_intake.models import IntakeWorkspace
from evidence_matrix.models import EvidenceMatrixRun, EvidenceMatrixStatus


class Command(BaseCommand):
    help = 'Export a completed evidence matrix run as a calibration dataset for weight tuning.'

    def add_arguments(self, parser):
        parser.add_argument('--workspace-slug', required=True, help='Workspace slug to export from.')
        parser.add_argument('--matrix-run-uuid', default='', help='Optional specific completed matrix run UUID.')
        parser.add_argument('--output', default='', help='Optional JSON output path.')

    def handle(self, *args, **options):
        workspace_slug = str(options['workspace_slug'] or '').strip()
        matrix_run_uuid = str(options.get('matrix_run_uuid') or '').strip()
        output_path = str(options.get('output') or '').strip()

        workspace = IntakeWorkspace.objects.filter(slug=workspace_slug).first()
        if workspace is None:
            raise CommandError(f'Workspace "{workspace_slug}" was not found.')

        queryset = EvidenceMatrixRun.objects.select_related('blueprint_run').filter(
            workspace=workspace,
            status=EvidenceMatrixStatus.COMPLETED,
        ).order_by('-updated_at')
        if matrix_run_uuid:
            queryset = queryset.filter(uuid=matrix_run_uuid)
        run = queryset.first()
        if run is None:
            raise CommandError('No completed evidence matrix run was found for export.')

        matrix_payload = dict(run.matrix_payload or {})
        cells = []
        for cell in list(matrix_payload.get('matrix_cells') or []):
            cells.append(
                {
                    'employee_uuid': str(cell.get('employee_uuid') or ''),
                    'employee_name': str(cell.get('employee_name') or ''),
                    'role_profile_uuid': str(cell.get('role_profile_uuid') or ''),
                    'role_name': str(cell.get('role_name') or ''),
                    'skill_key': str(cell.get('skill_key') or ''),
                    'skill_name_en': str(cell.get('skill_name_en') or ''),
                    'target_level': int(cell.get('target_level') or 0),
                    'current_level': float(cell.get('current_level') or 0.0),
                    'gap': float(cell.get('gap') or 0.0),
                    'confidence': float(cell.get('confidence') or 0.0),
                    'priority': int(cell.get('priority') or 0),
                    'role_fit_score': float(cell.get('role_fit_score') or 0.0),
                    'support_breakdown': list(cell.get('esco_support_breakdown') or []),
                    'support_signals': list(cell.get('support_signals') or cell.get('evidence_rows') or []),
                    'incompleteness_flags': list(cell.get('incompleteness_flags') or []),
                    'review_labels': {
                        'ready': None,
                        'current_level_override': None,
                        'confidence_override': None,
                        'notes': '',
                    },
                }
            )

        export_payload = {
            'exported_at': timezone.now().isoformat(),
            'workspace_slug': workspace.slug,
            'workspace_name': workspace.name,
            'matrix_run_uuid': str(run.uuid),
            'matrix_version': run.matrix_version,
            'blueprint_run_uuid': str(getattr(run.blueprint_run, 'uuid', '') or ''),
            'input_snapshot': dict(run.input_snapshot or {}),
            'summary_payload': dict(run.summary_payload or {}),
            'cell_count': len(cells),
            'cells': cells,
        }

        if output_path:
            target = Path(output_path)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(json.dumps(export_payload, ensure_ascii=False, indent=2), encoding='utf-8')
            self.stdout.write(self.style.SUCCESS(f'Calibration dataset exported to {target}'))
            return

        self.stdout.write(json.dumps(export_payload, ensure_ascii=False, indent=2))
