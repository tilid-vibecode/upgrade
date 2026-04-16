#!/usr/bin/env python
import argparse
import asyncio
import os

import django
from asgiref.sync import sync_to_async

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'server.settings')
django.setup()

from company_intake.models import IntakeWorkspace  # noqa: E402
from skill_blueprint.services import sync_role_library_for_workspace  # noqa: E402


async def main():
    parser = argparse.ArgumentParser(description='Sync a GitLab handbook role-library snapshot for a workspace.')
    parser.add_argument('workspace_slug')
    parser.add_argument('--max-pages', type=int, default=40)
    parser.add_argument('--base-url', action='append', dest='base_urls', default=[])
    args = parser.parse_args()

    workspace = await sync_to_async(
        IntakeWorkspace.objects.filter(slug=args.workspace_slug).first
    )()
    if workspace is None:
        raise SystemExit(f'Workspace not found: {args.workspace_slug}')

    snapshot = await sync_role_library_for_workspace(
        workspace,
        base_urls=args.base_urls,
        max_pages=args.max_pages,
    )
    print(f'Snapshot status: {snapshot.status}')
    print(f'Snapshot uuid: {snapshot.uuid}')
    print(f'Summary: {snapshot.summary}')
    print(f'Canonical families: {snapshot.summary.get("canonical_family_counts", {})}')
    print(f'Normalized skills: {snapshot.summary.get("normalized_skill_count", 0)}')
    print(f'Aliases seeded: {snapshot.summary.get("alias_count", 0)}')


if __name__ == '__main__':
    asyncio.run(main())
