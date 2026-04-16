# LLM Prompt Inventory

This file is a navigation index for all current LLM call sites and prompt-construction locations in the backend.

It focuses on:

- direct business-logic calls to OpenAI-backed helpers
- the exact functions where `system_prompt` / `user_prompt` are formed
- adjacent deterministic prompt/text builders that are worth reviewing together with the LLM prompts

It excludes:

- tests
- embedding calls (`server/embedding_manager.py`) because those are vector-generation calls, not prompt-driven LLM interactions
- speech/transcription helpers unless they start forming domain prompts in the future

## Quick Summary

- Business-logic LLM call sites found: `9`
- Shared structured gateway: `1`
- Generic OpenAI wrappers present in infra: `2`
- Stage 10 export/rendering: `0` LLM calls by design

## Shared Gateway

### Structured JSON Gateway

- File: `tools/openai/structured_client.py`
- Function: `call_openai_structured(...)`
- Lines: `304-329`
- Path: `/Users/nikita/Sites/upg/server/tools/openai/structured_client.py`
- What it does:
  - takes `system_prompt`
  - takes `user_prompt`
  - wraps them into `messages = [{"role": "system"}, {"role": "user"}]`
  - sends them through `OpenAIStructuredClient.create_json_response(...)`
- Why it matters:
  - this is the common entry point for all current stage-level structured LLM calls

## Business Logic LLM Calls

| App / Stage | Function | File / Lines | Schema | Prompt Purpose | Notes |
|---|---|---|---|---|---|
| Stage 4 | `extract_role_library_entry_with_llm` | `skill_blueprint/services.py:939-961` | `gitlab_role_library_entry` | Extract structured role-library data from handbook / JD pages | Uses page URL, title, and extracted page text |
| Stage 5 | `_extract_blueprint_with_llm` | `skill_blueprint/services.py:1039-1072` | `workspace_blueprint` | Build the initial published blueprint from workspace/company/roadmap/org/reference evidence | Highest-scope synthesis prompt in the pipeline |
| Stage 5 | `_refresh_blueprint_from_clarifications_with_llm` | `skill_blueprint/services.py:1075-1117` | `workspace_blueprint_clarification_refresh` | Refresh the existing blueprint conservatively after clarification answers | Keeps role set and ids stable unless clarified evidence justifies change |
| Stage 4/5 bridge | `match_employees_to_roles` | `skill_blueprint/services.py:1190-1208` | `batch_employee_role_matches` | Batch-match employees to roadmap roles | Called in batches; prompt built inside the batching loop |
| Stage 6 | `_extract_cv_payload` | `org_context/cv_services.py:274-298` | `employee_cv_profile_stage6` | Extract structured employee skill/profile evidence from CV text | Conservative extraction prompt |
| Stage 7 | `_phrase_assessment_pack_with_llm` | `employee_assessment/services.py:518-543` | `employee_assessment_pack_wording` | Turn deterministic question specs into friendly employee-facing wording | LLM does phrasing only, not selection |
| Stage 8 | `_build_matrix_summary_with_llm` | `evidence_matrix/services.py:180-206` | `matrix_team_summary` | Summarize deterministic matrix results for operator consumption | Scoring remains deterministic; LLM only summarizes |
| Stage 9 | `_generate_team_plan_narrative` | `development_plans/services.py:788-817` | `team_development_plan_stage9` | Turn deterministic team recommendations into sponsor-readable narrative | Has deterministic fallback |
| Stage 9 | `_generate_individual_plan_narrative` | `development_plans/services.py:820-855` | `individual_development_plan_stage9` | Turn deterministic PDP recommendations into employee/manager narrative | Has deterministic fallback |

## Detailed Inventory

### 1. Role Library Extraction

- File: `/Users/nikita/Sites/upg/server/skill_blueprint/services.py`
- Function: `extract_role_library_entry_with_llm`
- Lines: `939-961`
- LLM call:
  - `call_openai_structured(...)`
- Schema:
  - `gitlab_role_library_entry`
- Prompt inputs:
  - `page_url`
  - `page_title`
  - extracted page text
- Prompt shape:
  - `system_prompt` tells the model to extract structured role-library data conservatively
  - `user_prompt` passes page metadata plus the first `~18k` chars of page text
- Review priority:
  - good place to improve extraction quality for responsibilities, seniority signals, and required vs desirable skills

### 2. Initial Blueprint Generation

- File: `/Users/nikita/Sites/upg/server/skill_blueprint/services.py`
- Function: `_extract_blueprint_with_llm`
- Lines: `1039-1072`
- LLM call:
  - `call_openai_structured(...)`
- Schema:
  - `workspace_blueprint`
- Prompt inputs:
  - company profile metadata
  - pilot scope metadata
  - source summary
  - organization summary
  - roadmap evidence digest
  - strategy evidence digest
  - role reference evidence digest
  - supplemental parsed evidence digest
  - fallback evidence digest
  - role library digest
- Prompt shape:
  - this is the biggest synthesis prompt in the system
  - asks for roadmap initiatives, role candidates, skill requirements, and clarification questions
- Review priority:
  - high
  - changes here ripple into Stages 5-10

### 3. Blueprint Refresh From Clarifications

- File: `/Users/nikita/Sites/upg/server/skill_blueprint/services.py`
- Function: `_refresh_blueprint_from_clarifications_with_llm`
- Lines: `1075-1117`
- LLM call:
  - `call_openai_structured(...)`
- Schema:
  - `workspace_blueprint_clarification_refresh`
- Prompt inputs:
  - refresh note
  - current blueprint snapshot
  - answered clarifications
  - latest source summary
  - roadmap / strategy / role-reference evidence
  - fallback evidence digest
  - role library digest
- Prompt shape:
  - instructs the model to update conservatively and preserve ids / role families where possible
- Review priority:
  - high
  - this is the main prompt for “blueprint drift vs stability”

### 4. Employee-to-Role Matching

- File: `/Users/nikita/Sites/upg/server/skill_blueprint/services.py`
- Function: `match_employees_to_roles`
- Prompt-building lines: `1190-1208`
- LLM call:
  - `call_openai_structured(...)`
- Schema:
  - `batch_employee_role_matches`
- Prompt inputs:
  - batched employee profiles
  - role catalog derived from blueprint role candidates
- Prompt shape:
  - asks for up to 3 role matches per employee, sorted by fit score
  - fit score constrained to integer `0-100`
- Review priority:
  - medium-high
  - especially if role matching quality or adjacent-role ranking needs tuning

### 5. CV Extraction

- File: `/Users/nikita/Sites/upg/server/org_context/cv_services.py`
- Function: `_extract_cv_payload`
- Lines: `274-298`
- LLM call:
  - `call_openai_structured(...)`
- Schema:
  - `employee_cv_profile_stage6`
- Prompt inputs:
  - source title
  - language hint
  - parsed CV text
- Prompt shape:
  - strict extraction prompt
  - asks for conservative skill extraction, English normalization, optional Russian display names, and `0-5` skill levels
- Review priority:
  - high for CV parsing quality

### 6. Assessment Pack Wording

- File: `/Users/nikita/Sites/upg/server/employee_assessment/services.py`
- Function: `_phrase_assessment_pack_with_llm`
- Lines: `518-543`
- LLM call:
  - `call_openai_structured(...)`
- Schema:
  - `employee_assessment_pack_wording`
- Prompt inputs:
  - employee name and current title
  - primary role
  - adjacent roles
  - question themes
  - hidden-skills prompt spec
  - aspiration prompt spec
  - targeted question specs
  - global notes
- Prompt shape:
  - explicitly says the model may phrase but may not invent or change question ids
- Review priority:
  - high for employee tone and questionnaire quality

### 7. Matrix Team Summary

- File: `/Users/nikita/Sites/upg/server/evidence_matrix/services.py`
- Function: `_build_matrix_summary_with_llm`
- Lines: `180-206`
- LLM call:
  - `call_openai_structured(...)`
- Schema:
  - `matrix_team_summary`
- Prompt inputs:
  - roadmap context
  - deterministic team summary
  - top employee gaps
  - risk summary
  - incompleteness summary
- Prompt shape:
  - summarization-only prompt
  - matrix math already happened before this point
- Review priority:
  - medium
  - useful if operator summaries feel vague or overconfident

### 8. Team Development Plan Narrative

- File: `/Users/nikita/Sites/upg/server/development_plans/services.py`
- Function: `_generate_team_plan_narrative`
- Lines: `788-817`
- LLM call:
  - `call_openai_structured(...)`
- Schema:
  - `team_development_plan_stage9`
- Prompt inputs:
  - company context
  - roadmap context
  - matrix summary
  - matrix risks
  - deterministic recommendation payload
- Prompt shape:
  - asks for pragmatic sponsor-ready narrative
  - explicitly forbids inventing new recommendation types
- Review priority:
  - high if you want sharper team-plan writing

### 9. Individual PDP Narrative

- File: `/Users/nikita/Sites/upg/server/development_plans/services.py`
- Function: `_generate_individual_plan_narrative`
- Lines: `820-855`
- LLM call:
  - `call_openai_structured(...)`
- Schema:
  - `individual_development_plan_stage9`
- Prompt inputs:
  - company context
  - roadmap context
  - employee matrix payload
  - deterministic individual recommendation payload
- Prompt shape:
  - asks for PDP wording only
  - explicitly forbids inventing new actions or real course ids
- Review priority:
  - high if you want PDP tone / usefulness improvements

## Non-LLM Prompt / Wording Builders Worth Reviewing Together

These are not LLM calls, but they generate employee-facing or export-facing wording and usually should be reviewed alongside the prompts above.

### Stage 7 Deterministic Assessment Question Text

- File: `/Users/nikita/Sites/upg/server/employee_assessment/services.py`
- Functions:
  - `_build_optional_example_prompt`
  - `_deterministic_targeted_prompt`
- Lines: `1168-1180`
- Why it matters:
  - these are the fallback/default question texts when the LLM wording is absent or partial
  - they strongly influence Stage 7 pack tone

### Stage 7 Hidden/Aspiration Prompt Specs

- File: `/Users/nikita/Sites/upg/server/employee_assessment/services.py`
- Location: `340-350`
- What is formed:
  - hidden-skills prompt metadata
  - aspiration prompt metadata
- Why it matters:
  - these specs feed the Stage 7 wording prompt and shape the pack structure

### Stage 10 Export Rendering

- File: `/Users/nikita/Sites/upg/server/development_plans/renderers.py`
- Key entry points:
  - `build_plan_export_payload` at `13-21`
  - `render_plan_artifact` at `24-55`
- Why it matters:
  - no LLM here
  - but this is where final team/PDP language is rendered into JSON / Markdown / HTML
  - worth reviewing if the output documents feel awkward even when Stage 9 prompt output is good

## Generic OpenAI Infrastructure Present In Repo

These are generic wrappers, not current business-stage prompt owners.

### Chat Completions JSON Wrapper

- File: `/Users/nikita/Sites/upg/server/tools/openai/agent.py`
- Function: `chat_json_request`
- Lines: `48-92`
- Notes:
  - accepts arbitrary caller-supplied `messages`
  - no current business-stage prompt text is formed here

### Responses API Wrapper

- File: `/Users/nikita/Sites/upg/server/tools/openai/responses_agent.py`
- Function: `responses_json_request`
- Lines: `53-140`
- Notes:
  - accepts arbitrary caller-supplied `input_items` and optional `instructions`
  - no current stage-level prompt formation found through this path in app code

## No Business LLM Prompt Calls Found In These App Areas

- `company_intake`
- `media_storage`
- `billing`
- `stage 10` export generation

## Suggested Review Order

If the goal is to improve prompt quality with the highest leverage first, review in this order:

1. `skill_blueprint/services.py:_extract_blueprint_with_llm`
2. `skill_blueprint/services.py:_refresh_blueprint_from_clarifications_with_llm`
3. `org_context/cv_services.py:_extract_cv_payload`
4. `employee_assessment/services.py:_phrase_assessment_pack_with_llm`
5. `development_plans/services.py:_generate_team_plan_narrative`
6. `development_plans/services.py:_generate_individual_plan_narrative`
7. `evidence_matrix/services.py:_build_matrix_summary_with_llm`
8. `skill_blueprint/services.py:match_employees_to_roles`
9. deterministic Stage 7 wording fallbacks

## Search Patterns Used

Primary repo scan terms:

- `call_openai_structured(`
- `system_prompt =`
- `user_prompt =`
- `chat.completions.create(`
- `responses.create(`

This inventory reflects the current codebase state at generation time and should be easy to refresh with the same search patterns later.
