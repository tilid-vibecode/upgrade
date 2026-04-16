# Prototype flow notes

Implemented in this iteration:

## 1. Intake and parsing foundation kept from the previous prototype

- Media-storage-first public uploads under `/api/v1/prototype/media/*`
- Workspaces and attached sources under `/api/v1/prototype/workspaces/*`
- Parsed source + normalized org context foundation
- CSV org imports for BambooHR-style and roster-style inputs
- PDF, DOCX, TXT parsing

Stage 00 hardening now also adds:

- `GET /api/v1/prototype/workspaces/{workspace_slug}/workflow-status`
- workspace-bound source detail, update, archive, and download routes
- workspace ownership on prototype media files
- immutable final assessment submission semantics
- explicit matrix assessment-cycle selection

## 2. External URL parsing

`WorkspaceSource` entries with `transport=external_url` can now be fetched and parsed.

This is intended for public role-library pages such as GitLab handbook job descriptions.

Source-management additions:

- `GET /api/v1/prototype/workspaces/{workspace_slug}/sources/{source_uuid}`
- `PATCH /api/v1/prototype/workspaces/{workspace_slug}/sources/{source_uuid}`
- `DELETE /api/v1/prototype/workspaces/{workspace_slug}/sources/{source_uuid}`
- `GET /api/v1/prototype/workspaces/{workspace_slug}/sources/{source_uuid}/download`

## 3. Actual LLM-backed prototype stages

A new structured OpenAI client was added in `tools/openai/structured_client.py`.

It calls the OpenAI Chat Completions API with JSON schema response format and requires:

- `OPENAI_API_KEY`
- optional `UPG_FLOW_MODEL` (defaults to `gpt-4o-mini`)

## 4. CV evidence extraction

New route:

- `POST /api/v1/prototype/workspaces/{workspace_slug}/org-context/cv-evidence/build`

This stage:

- reads parsed CV sources
- extracts employee profile + skills via LLM
- upserts employees
- stores `EmployeeSkillEvidence` rows with `source_kind=employee_cv`

## 5. Role-library snapshot sync

New routes:

- `POST /api/v1/prototype/workspaces/{workspace_slug}/role-library/sync`
- `GET /api/v1/prototype/workspaces/{workspace_slug}/role-library/latest`

New models:

- `RoleLibrarySnapshot`
- `RoleLibraryEntry`

Default seed URLs are GitLab handbook role sections for:

- engineering
- product
- marketing

The sync flow discovers role URLs, fetches pages, and structures them with LLM.

A helper script was added:

- `scripts/gitlab_handbook_snapshot.py`

## 6. Blueprint generation

New routes:

- `POST /api/v1/prototype/workspaces/{workspace_slug}/blueprint/generate`
- `GET /api/v1/prototype/workspaces/{workspace_slug}/blueprint/latest`

`SkillBlueprintRun` now stores:

- `source_summary`
- `company_context`
- `roadmap_context`
- `role_candidates`
- `clarification_questions`
- `employee_role_matches`
- previous skill/gap/assessment fields

Blueprint generation now:

- summarizes company context from uploaded materials
- extracts roadmap initiatives
- proposes minimal role set for execution
- proposes role-skill requirements
- creates clarification questions
- creates an assessment plan
- normalizes `RoleProfile`, `RoleSkillRequirement`, and `OccupationMapping`
- matches employees to target roles with LLM and stores `EmployeeRoleMatch`

## 7. Employee assessment generation and submission

New routes:

- `POST /api/v1/prototype/workspaces/{workspace_slug}/assessments/generate`
- `GET /api/v1/prototype/workspaces/{workspace_slug}/assessments/latest`
- `GET /api/v1/prototype/workspaces/{workspace_slug}/assessments/latest/packs`
- `POST /api/v1/prototype/assessment-packs/{pack_uuid}/submit`

New model:

- `EmployeeAssessmentPack`

This stage:

- generates a short self-assessment per employee using roadmap + role matches + current evidence
- stores questionnaire JSON per employee
- accepts manual response submission
- converts self-ratings into `EmployeeSkillEvidence` rows with `source_kind=self_report`
- rejects re-submission once a pack is already finalized

## 8. Evidence matrix generation

New routes:

- `POST /api/v1/prototype/workspaces/{workspace_slug}/evidence-matrix/build`
- `GET /api/v1/prototype/workspaces/{workspace_slug}/evidence-matrix/latest`

`EvidenceMatrixRun` now stores:

- `summary_payload`
- `matrix_payload`

This stage:

- picks each employee’s best-fit target role
- aggregates weighted evidence for required skills
- computes current level, confidence, and gap
- produces a matrix payload plus LLM summary
- can now pin `self_assessment` evidence to an explicit assessment cycle

## 9. Final team plan + individual PDPs

New routes:

- `POST /api/v1/prototype/workspaces/{workspace_slug}/development-plans/generate`
- `GET /api/v1/prototype/workspaces/{workspace_slug}/development-plans/latest-team`
- `GET /api/v1/prototype/workspaces/{workspace_slug}/development-plans/latest-individual`

`DevelopmentPlanRun` now supports:

- team scope
- individual scope with `employee` foreign key

This stage:

- generates a team development plan from roadmap + matrix
- generates an individual PDP for every employee in scope
- uploads generated JSON artifacts back through `media_storage`
- stores the generated artifact key on the plan run

## 10. Recommended manual flow for the prototype

1. Create workspace
2. Complete workspace context
3. Upload and attach sources
4. Parse sources
5. Run roadmap analysis
6. Generate blueprint
7. Resolve clarifications and publish
8. Build CV evidence
9. Generate assessment packs
10. Submit assessment answers
11. Build evidence matrix
12. Generate development plans

Notes:
- `parse` is source extraction, chunking, and indexing only.
- `cv-evidence/build` is an explicit operator action that enriches employee profiles after parsing.
- The downstream dependency chain is `evidence -> assessments -> matrix -> plans`.
