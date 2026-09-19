# GBot AI Integration Roadmap

Source brief: `Ai_integration.md`

Working feature name: **AI Placement Optimization**

Use **AI Test Manager** only for labels that describe the agent UI itself. The broader module should be named AI Placement Optimization because the feature is not just chat or generation; it optimizes the automated inbox placement cycle.

## 1. Product Goal

Add an AI-managed orchestration layer to the existing Inbox Intelligence workflow without replacing GBot's deterministic backend.

The AI should help choose templates, choose workspace sources, interpret test results, recommend safe optimization actions, and prepare the next test iteration. GBot must continue to own:

- sending email tests
- selecting and validating Workspace users
- syncing IMAP inboxes
- classifying placement as Inbox, Spam, Missing, Grey, or Failed
- storing tests and messages
- enforcing safety and permission rules
- applying account or domain actions

The AI can propose and explain. The backend validates and executes.

## 2. Current GBot Compatibility Map

The codebase already has the foundation for this feature:

- OpenRouter config exists in `InboxOpenRouterConfig` in `database.py`.
- OpenRouter config routes exist at `/api/inbox-intelligence/openrouter-config` in `app.py`.
- Region-level AI suggestions exist at `/api/inbox-intelligence/ai-region-editor`.
- A template AI prompt is persisted through `/api/inbox-intelligence/template-ai-prompt`.
- Static template and user template models already separate controlled template assets from runtime test messages.
- Automated Email Tests already contain the correct workflow areas: Test Queue, Workspace Sources, Test Summary, Active & Completed Tests, and User Inbox Analytics.
- The frontend already has an AI Optimization analytics tab placeholder in `templates/inbox_intelligence.html`.
- The automated test flow already supports custom header/body payloads and retrieves user inbox analytics from deliverability messages.

The roadmap should build on these pieces. Big Pickle AI should not create a second AI settings system, a second test runner, or a parallel analytics engine.

## 3. Architectural Rule

AI Placement Optimization must be implemented as an orchestration layer above the existing workflow.

Correct responsibility split:

- AI service: analyze context, generate structured plans, generate explanations, rank options, propose safe actions.
- Backend controllers: authenticate, authorize, validate input, call AI service, validate AI output, persist jobs and suggestions.
- Existing delivery services: send, sync, classify, update counts, update message rows.
- Frontend: show recommendations, diffs, confidence, warnings, and explicit apply buttons.

The AI must never directly send emails, mutate Workspace users, unsuspend users, delete sources, or update verdicts without a backend validation step and a user-confirmed operation.

## 4. Backend Roadmap

### Phase 0 - Audit and Naming

Tasks:

- Confirm the feature name in UI navigation and backend route names: `ai-placement-optimization`.
- Keep existing OpenRouter settings and migrate only if needed.
- Inventory existing automated test endpoints, test message models, template models, and analytics serializers.
- Identify every existing endpoint used by Automated Email Tests before adding AI endpoints.

Validation:

- No duplicate OpenRouter config table.
- No duplicate automated test table.
- No duplicate user inbox analytics serializer.
- AI routes use `@login_required` and `@permission_required('inbox_intelligence')`.

### Phase 1 - AI Gateway Service

Create a backend service layer, for example:

- `services/inbox_ai_gateway.py`
- `services/inbox_ai_schemas.py`
- `services/inbox_ai_redaction.py`

The gateway should centralize OpenRouter calls. Existing route code currently calls OpenRouter directly inside `/api/inbox-intelligence/ai-region-editor`; that should be moved into the service layer.

Required gateway behavior:

- Read OpenRouter settings from `InboxOpenRouterConfig`.
- Support env fallback: `OPENROUTER_API_KEY` and `OPENROUTER_MODEL`.
- Use encrypted DB key first when configured.
- Apply timeout, retry, and max-token controls.
- Return typed results, not raw provider responses.
- Support deterministic local fallback for non-critical suggestions.
- Log only safe metadata: model, latency, token estimate, job id, status.
- Never log API keys, app passwords, raw auth headers, or full private mailbox payloads.

Suggested public functions:

- `generate_region_suggestions(payload)`
- `analyze_template(payload)`
- `build_test_plan(payload)`
- `analyze_test_results(payload)`
- `recommend_optimization_actions(payload)`
- `generate_retry_plan(payload)`

Validation:

- Unit tests can mock the gateway without hitting OpenRouter.
- OpenRouter failures return controlled error/fallback responses.
- The browser never receives the OpenRouter API key.

### Phase 2 - Structured AI Contracts

All AI responses must be JSON with strict schema validation. Do not trust model text.

Add schemas for:

- Template analysis
- Test plan
- Source selection recommendation
- User allocation recommendation
- Result analysis
- Optimization action plan
- Copy-ready result lists

Example action plan shape:

```json
{
  "summary": "string",
  "confidence": "low|medium|high",
  "recommended_next_step": "string",
  "risk_level": "low|medium|high",
  "actions": [
    {
      "type": "RETRY_TEST|EDIT_TEMPLATE|CHANGE_SOURCE|EXCLUDE_USER|REVIEW_DOMAIN",
      "target": "string",
      "reason": "string",
      "requires_confirmation": true,
      "backend_validation": "string"
    }
  ],
  "copy_ready": {
    "inbox_users": [],
    "spam_users": [],
    "inbox_domains": [],
    "spam_domains": []
  }
}
```

Validation:

- Invalid AI JSON is rejected or repaired only through a safe parser with a fallback.
- Unknown action types are ignored.
- Every executable action has `requires_confirmation: true`.
- Backend recomputes all user/domain/test ids from DB, not from AI text.

### Phase 3 - Database Persistence

Add persistence so AI output is auditable and not just temporary UI state.

Recommended models:

- `InboxAiJob`
- `InboxAiSuggestion`
- `InboxAiAuditEvent`
- `InboxAiPromptVersion`

Suggested `InboxAiJob` fields:

- `job_id`
- `job_type`
- `status`
- `test_id`
- `source_template_id`
- `created_by`
- `provider`
- `model`
- `input_summary_json`
- `output_json`
- `error_message`
- `started_at`
- `completed_at`
- `created_at`

Suggested `InboxAiAuditEvent` fields:

- `event_type`
- `job_id`
- `test_id`
- `suggestion_id`
- `user_email`
- `payload_json`
- `created_at`

Validation:

- Suggestions remain visible after refresh.
- Accepted, rejected, and expired suggestions are distinguishable.
- Prompt changes are versioned and tied to user/time.

### Phase 4 - AI API Endpoints

Add AI endpoints under a consistent namespace:

- `POST /api/inbox-intelligence/ai/analyze-template`
- `POST /api/inbox-intelligence/ai/build-test-plan`
- `POST /api/inbox-intelligence/ai/analyze-results`
- `POST /api/inbox-intelligence/ai/recommend-actions`
- `POST /api/inbox-intelligence/ai/generate-retry-plan`
- `POST /api/inbox-intelligence/ai/suggestions/<id>/accept`
- `POST /api/inbox-intelligence/ai/suggestions/<id>/reject`
- `GET /api/inbox-intelligence/ai/jobs/<job_id>`

Endpoint rules:

- All endpoints require Inbox Intelligence permission.
- Long-running work should create a job and return `job_id`.
- Result analysis should accept `test_id`, then load authoritative DB state server-side.
- The frontend can send selected ids, but the backend must re-query and validate them.
- Accept endpoints should apply only safe changes that already have a deterministic backend handler.

Validation:

- Calling AI analysis with a nonexistent test id returns 404.
- Calling AI analysis for a test owned by another user or without permission returns 403.
- Accepting a suggestion that refers to missing rows fails safely.

### Phase 5 - Connect to Automated Test Flow

The AI can assist these existing steps:

1. Test Queue
   - rank queued templates by risk and representativeness
   - flag missing subject/header/body
   - recommend delete/keep groups, but never delete automatically

2. Workspace Sources
   - recommend manual account or list source
   - recommend users per account based on target observations
   - detect when list selection and manual lookup behave inconsistently
   - validate that AI recommendations still respect the current selected account/list data

3. Test Summary
   - explain readiness problems
   - predict likely coverage based on selected users and receiving inboxes

4. Active & Completed Tests
   - summarize status
   - detect stuck tests
   - recommend sync or retry only when backend status allows it

5. User Inbox Analytics
   - summarize placement results
   - identify spam-heavy users/domains/providers
   - produce copy-ready lists
   - generate a next-test plan

Validation:

- Existing `runAutomatedEmailTest()` still works with AI disabled.
- Existing user inbox analytics still renders with no AI job.
- AI suggestions update when a different automated test is selected.

## 5. Frontend Roadmap

### AI Placement Optimization Tab

Replace the current "Coming Soon" placeholder with a real AI Optimization panel inside the Automated Email Tests analytics section.

Panel sections:

- AI Summary
- Placement Diagnosis
- Recommended Actions
- Retry Plan
- Copy-ready Lists
- AI Audit Trail

The tab should match the professional Automated Email Tests design:

- compact cards
- clear status chips
- no marketing-style hero content
- no oversized decorative cards
- tables and dense rows for operational data
- explicit action buttons with icons

### Message Setup AI Controls

Add AI controls to the Message Setup area:

- Analyze selected template
- Generate header from selected template
- Improve body
- Validate placeholders
- Explain spam risk
- Apply suggestion
- Revert suggestion

Required behavior:

- When a template is selected, its saved header and body must populate immediately.
- AI can propose edits, but the user must click Apply.
- The UI should show a diff or before/after preview before applying major changes.
- Placeholder validation must preserve `[date]`, `[to]`, `[from]`, `[subject]`, `[test_id]`, and `[message_id]`.

### Test Queue AI Controls

Add queue intelligence without cluttering the list:

- score chip per queued email: Low Risk, Review, High Risk
- AI reason tooltip
- bulk action: Analyze selected
- bulk action: Recommend cleanup
- delete selected must remain a normal deterministic button, not an AI-only action

### Workspace Sources AI Controls

The users-per-account issue must be handled as part of this feature.

Frontend behavior:

- Manual account lookup and list-management selection must both honor `Users per account`.
- Changing users per account must immediately recompute selected sender count.
- AI recommendations must update the same state used by the normal form.
- The design must keep retrieved account lists and user rows compact and aligned with the screenshot style.

AI controls:

- Recommend allocation
- Explain coverage
- Flag under-sampling
- Flag suspicious source/list mismatch

### Analytics AI Controls

AI Optimization must use the same backend result data as User Inbox Analytics.

Frontend behavior:

- Selecting a completed test loads User Inbox Analytics and AI Optimization from the same `test_id`.
- AI tab should show loading, empty, error, and completed states.
- Copy-ready lists should support copy buttons for spam users, inbox users, spam domains, and inbox domains.
- Each recommendation must display confidence and risk.

## 6. Safety Rules

Hard rules:

- AI cannot trigger a test without explicit user confirmation.
- AI cannot delete queued sources automatically.
- AI cannot unsuspend, suspend, modify, or exclude Workspace users automatically.
- AI cannot mark a message Inbox or Spam.
- AI cannot override backend thresholds.
- AI cannot see secrets.
- AI cannot receive full mailbox data unless the user selected a content-analysis mode.

Safe action categories:

- Draft body/header edits
- Template warnings
- Source recommendations
- Suggested user exclusions
- Suggested retry plan
- Copy-ready lists
- Explanation of observed results

Backend must validate any suggested action against current DB state before applying it.

## 7. Privacy and Prompt Injection Controls

Implement redaction before sending context to OpenRouter:

- redact access tokens
- redact app passwords
- redact API keys
- redact cookies
- redact auth headers
- redact private service account keys
- truncate message bodies
- strip hidden tracking content unless needed for template integrity analysis

Prompt injection controls:

- Treat email content, subjects, senders, and templates as untrusted input.
- Wrap untrusted content in JSON fields.
- System prompt must state that message content cannot override GBot safety rules.
- Backend must ignore AI instructions that attempt to call tools, bypass safety, or mutate state directly.

## 8. Testing Plan

Backend tests:

- OpenRouter config save/load without exposing secrets.
- AI gateway returns fallback on provider failure.
- AI JSON schema rejects malformed action plans.
- Analyze-results endpoint loads test data server-side from `test_id`.
- Accept-suggestion endpoint rejects unknown actions.
- Accept-suggestion endpoint rejects stale suggestions.
- Redaction removes secrets from prompt payloads.

Frontend tests:

- JavaScript syntax check for `templates/inbox_intelligence.html`.
- Template selection populates Message Setup header and body.
- Delete selected queue action works.
- Users per account works for manual account lookup and list selection.
- AI Optimization tab renders empty, loading, error, and completed states.
- Applying AI body/header suggestion updates the form and can be reverted.

Manual QA:

- Run an automated test without OpenRouter configured.
- Run AI region suggestions with OpenRouter configured.
- Run a completed test, sync results, and generate AI analysis.
- Refresh the page and confirm AI suggestions persist.
- Verify copy-ready lists match User Inbox Analytics counts.

Regression commands:

```bash
python3 -m py_compile app.py database.py
awk '/<script>/{flag=1;next}/<\/script>/{flag=0}flag' templates/inbox_intelligence.html | node --check
```

## 9. Big Pickle AI Validation Checklist

Use this checklist to validate the implementation.

Architecture:

- Does the implementation reuse `InboxOpenRouterConfig`?
- Are OpenRouter calls centralized in a service?
- Are AI endpoints namespaced under `/api/inbox-intelligence/ai/`?
- Does the backend own execution and validation?
- Does the AI only propose actions?

Data:

- Are AI jobs and suggestions persisted?
- Are accepted/rejected suggestions auditable?
- Are prompts versioned?
- Are secrets redacted before provider calls?

Frontend:

- Does the AI Optimization tab contain real result-driven content?
- Does it use the same selected automated `test_id` as User Inbox Analytics?
- Does template selection populate header and body?
- Does the users-per-account control work for both manual and list sources?
- Are retrieved accounts and users displayed compactly and professionally?

Safety:

- Can AI trigger tests without confirmation? It must not.
- Can AI delete sources without confirmation? It must not.
- Can AI mutate Workspace users directly? It must not.
- Can AI overwrite placement verdicts? It must not.

Quality:

- Does the feature work when OpenRouter is not configured?
- Are errors shown in the UI without breaking the workflow?
- Are long AI calls asynchronous or job-based?
- Does the app still pass compile and JS syntax checks?

## 10. Delivery Phases

Phase 1: Foundation

- Create AI gateway service.
- Add schemas and redaction.
- Move current region-editor OpenRouter logic into the service.
- Keep existing UI behavior working.

Phase 2: Persistence and Jobs

- Add AI job/suggestion/audit models.
- Add migration.
- Add job polling endpoint.
- Persist AI results by test id.

Phase 3: Message Setup AI

- Add template analysis.
- Add header/body suggestion endpoints.
- Add frontend apply/revert flows.
- Preserve placeholders and MIME boundaries.

Phase 4: Automated Workflow AI

- Add test queue recommendations.
- Add workspace source allocation recommendations.
- Fix users-per-account behavior for list selection and manual lookup.
- Add readiness explanation.

Phase 5: Analytics AI

- Replace Coming Soon tab with real AI Optimization results.
- Generate diagnosis, action plan, retry plan, and copy-ready lists.
- Store accepted/rejected actions.

Phase 6: Hardening and Rollout

- Add feature flag such as `INBOX_AI_ENABLED`.
- Add rate limits and budget controls.
- Add full regression tests.
- Add UI polish and responsive QA.
- Roll out first as suggestion-only mode, then enable confirmed actions.

## 11. Acceptance Criteria

The integration is complete when:

- Automated Email Tests work normally with AI disabled.
- OpenRouter settings are stored securely and never returned raw.
- AI suggestions are generated through backend routes only.
- AI outputs are schema-validated and persisted.
- AI Optimization uses the same test data as User Inbox Analytics.
- The bottom analytics section shows real AI diagnosis and copy-ready lists, not a placeholder.
- Template selection fills header and body correctly.
- Users per account works identically for manual account lookup and list selection.
- No AI action can execute without backend validation and user confirmation.
- The app passes Python compile checks and frontend JavaScript syntax checks.
