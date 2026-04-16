# Upgrade backend

Use the root README for the full local setup guide:

- [/Users/nikita/Sites/upg/README.md](/Users/nikita/Sites/upg/README.md)

Short version:

```bash
cd /path/to/upg
cp .env.example .env

cd server
python3.13 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python manage.py migrate
honcho start
```

The backend depends on these local services:

- PostgreSQL
- Redis
- MinIO
- Qdrant

The backend reads `.env` from the repository root, not from `server/`.

## Prototype flow added in this iteration

The unauthenticated prototype flow now lives under `/api/v1/prototype/*`.

Main endpoints:

- `POST /api/v1/prototype/media/upload` — generic upload into `media_storage`
- `POST /api/v1/prototype/workspaces` — create or reopen a workspace
- `GET /api/v1/prototype/workspaces/{workspace_slug}/workflow-status` — full stage/status summary for the operator UI
- `POST /api/v1/prototype/workspaces/{workspace_slug}/sources` — attach a file/URL/text source to the workspace
- `PATCH /api/v1/prototype/workspaces/{workspace_slug}/sources/{source_uuid}` — update source metadata or transport payload
- `DELETE /api/v1/prototype/workspaces/{workspace_slug}/sources/{source_uuid}` — archive a source from the active pilot flow
- `GET /api/v1/prototype/workspaces/{workspace_slug}/sources/{source_uuid}/download` — fetch a signed URL for a workspace-owned source file
- `POST /api/v1/prototype/workspaces/{workspace_slug}/parse` — parse attached sources and populate normalized org-context tables
- `GET /api/v1/prototype/workspaces/{workspace_slug}/org-context/summary` — verify imported counts
- `GET /api/v1/prototype/workspaces/{workspace_slug}/org-context/employees` — inspect imported employees

Stage 00 hardening notes:

- prototype uploads can now be stamped to a specific workspace owner
- workspace-owned prototype files are no longer meant to be browsed through the old unscoped media listing path
- matrix builds can target an explicit `assessment_cycle_uuid`
- submitted assessment packs are immutable

The first pass parser supports:

- CSV org structures
- PDF text extraction
- DOCX text extraction
- TXT / inline text

Google Docs links are represented as workspace sources, but remote fetching is still intentionally left for a later iteration.
