# MC&S CoWorker v3 — Build Plan

**Generated:** 2026-05-08
**Companion to:** `MCS_CoWorker_v3_Architecture.md` (design source of truth) and `MCS-coworker-V3-spec.md` at the repo root (status snapshot)
**Purpose:** Living plan for executing Phases 3 → 16. Updated at every phase boundary.

When this plan and the architecture doc disagree on **design**, the architecture doc wins.
When this plan and the architecture doc disagree on **build status**, the code wins (verify with `git log` and `pytest`).

---

## 0. Working agreement

Methodology principles for the remaining build. These are how we deliver "methodically worked through, errors caught, completes successfully."

1. **Verify before plan.** Each phase begins with `git log`, `pytest --collect-only`, and reading the code in the area. Spec docs drift; code is ground truth.
2. **Test alongside implementation.** Every commit ends with `pytest backend/tests` green. Tests are part of the same commit as the code they cover, never a follow-up.
3. **One concept per commit.** A migration + the model + the test + the route is one concept. Five unrelated things is not. Roll-back becomes a single commit revert.
4. **Migration up/down/up before commit.** `alembic upgrade head && alembic downgrade -1 && alembic upgrade head` for every migration. Both directions or it does not ship.
5. **Phase exit criteria are written down and tested.** Every phase has a checklist with executable acceptance tests. The phase is not "done" until the checklist is green.
6. **Pause at phase boundaries.** I write a phase-wrap summary, you sign off, then we begin the next phase. Phase boundaries are reversible — stop halfway and what's there is usable.
7. **Carry-forward register, not magic memory.** Open items, deferred decisions, and surprises are recorded in §10 of this doc. Nothing important survives only in conversation.
8. **Stop-on-error.** If a step fails, surface it. Do not paper over with `try/except`, `--no-verify`, or skipped tests. Diagnose the root cause. CLAUDE.md is explicit: "Never disable a type check or a lint rule to make code pass. Fix the code."
9. **No yak-shaving.** Surprises that are not blockers go to the register, not into the current commit. A bug fix doesn't become a refactor PR.
10. **No premature abstraction.** Build for the tool you need, not the framework you imagine. Abstraction layers earn their keep through the third use.

What this plan **does not** promise: zero errors. What it does promise: every error is caught at the test layer and surfaced before it ships.

---

## 1. State as of 2026-05-08

**Verified against `git log` and `pytest --collect-only`, not the spec snapshot.**

| Item | Spec snapshot says | Code actually says |
|---|---|---|
| Phase 3 day-one progress | Step 1 of 7 | **Step 2 of 7 done** (commit `277ad92` adds `refresh_access_token`) |
| Test count on main | 41 | **47** |
| Current files in `coworker/graph/` | implied empty | `__init__.py`, `auth.py`, `exceptions.py` |
| Untracked files | `auth.py.bak` | `auth.p` **and** `auth.py.bak` (likely typo + leftover) |

Implication: the spec was generated mid-day. Step 2 is in main; we resume from Step 3.

Phases 0–2 complete in dev. Production droplet `coworker-v3-prod-syd1` is named, not provisioned. All work to date is local WSL2.

---

## 2. Phase sequencing — strategic view

```
Phase 3  Connectors          (in progress — day 1 step 2 of 7 done)
Phase 4  Memory              ◄── unblocks 5, 6, 7, 8, 11
Phase 5  Orchestrator        ◄── unblocks 6, 8, 9
Phase 6  Plugin system       ◄── unblocks 7, 9, 11, 12, 13
Phase 7  Vision              parallel-eligible with 8 once 6 lands
Phase 8  Specialists         parallel-eligible with 7 once 5 lands
Phase 9  Approval queue      ◄── unblocks 10, 11, 12, 13
Phase 10 Web frontend        consumer of 3–9; can start UI scaffolding earlier
Phase 11 Real-time + reflection
Phase 12 Mobile / PWA / calendar
Phase 13 Onboarding
Phase 14 Operations          parallel-eligible from Phase 6 onward
Phase 15 Migration           one-time, parallel-eligible from Phase 9
Phase 16 Cutover              procedural — final
```

Hard dependency rules:
- Phase 5 (orchestrator) cannot start until Phase 4's memory tools exist, because the tool registry needs them.
- Phase 6 (plugins) cannot start until Phase 5 — plugins are agent-loop drivers.
- Phase 9 (approval queue) cannot start until Phase 6 — plugin runs produce approval items.
- Phase 10 (frontend) **can** start scaffolding (build, routing, design system) in parallel from Phase 6, but the page-by-page builds wait for their backing API.

Time estimates are deliberately omitted. Estimates corrode into deadlines and deadlines corrode into shortcuts. We measure progress in completed phase-exit checklists.

---

## 3. Phase 3 — External Service Connectors (DEEP)

**Goal restatement:** Five connectors (Anthropic, Microsoft Graph, XPM, FuseSign, Teams) that are the **only** code paths making raw HTTP calls to external systems. Every credential per-firm. Every write shadow-guarded. Every call audited and rate-limited. PII scrubbed before every Anthropic prompt. Errors normalised.

### 3A. Finish Phase 3 day one — `GET /api/inbox` (5 commits remaining)

**Already done:** Step 0 (preflight), Step 1 (`current_user`), Step 2 (`refresh_access_token`).

| # | Step | Files | Test deliverable |
|---|------|-------|------------------|
| 3 | Token bucket (1000/60s global) + per-mailbox semaphore (4 in-flight) | `coworker/graph/rate_limit.py` (new) | unit: bucket waits past tokens exhausted; semaphore caps concurrency |
| 4 | `graph_context` FastAPI dependency: returns `GraphContext` dataclass with `firm`, `user`, `access_token`, `session`. Re-enters `firm_context` for the request scope. Auto-refreshes if `ms_token_expires_at` within 5-minute buffer. | `coworker/graph/__init__.py` (extend) or new `coworker/graph/context.py` | integration: token within buffer triggers refresh; far-from-expiry does not |
| 5 | `list_inbox(ctx, top=25)` — calls `GET /me/messages?$top=25&$orderby=receivedDateTime desc`. Goes through rate limit. Returns parsed list. Audits `graph.mail.list_inbox`. | `coworker/graph/mail.py` (new) | integration with `respx`: success returns 25 items; 401 → `ConnectorAuthError`; 429 → `ConnectorRateLimited`; 5xx → `ConnectorTransient` |
| 6 | `GET /api/inbox` route — depends on `current_user` and `graph_context`, returns the list. | `coworker/api/routes/mail.py` (new), wired in `coworker/api/main.py` | integration with FastAPI TestClient: signed-in user gets 25 items; no-cookie returns generic 401 |
| 7 | Phase-3-day-1 wrap. Run full pytest. Update `MCS-coworker-V3-spec.md` step table. Single commit closing day one. | — | full suite green |

Pre-flight cleanup (one commit, before Step 3):
- `git rm` `backend/coworker/api/routes/auth.p` (typo) and `auth.py.bak` (leftover) per carry-forward item §10.10.
- Verify `.gitignore` covers `*.bak` — add if missing.

**Sub-phase 3A exit checklist:**
- [ ] `pytest backend/tests` green; new tests for Steps 3–6 included
- [ ] `mypy --strict` clean on touched files
- [ ] `GET /api/inbox` returns 25 messages locally with a real signed-in user (manual verify)
- [ ] No untracked or `.bak` files in `git status`
- [ ] Token refresh observable in audit log on a deliberately-near-expiry user

### 3B. Anthropic connector (highest blast radius — build first after 3A)

Why first: every connector after this one calls Claude. Specialists, classifiers, hybrid-rerank, vision — all gated on this. Ship it now with PII scrubbing and metering, downstream code stops reinventing.

**Files:**
- `coworker/connectors/anthropic_client.py` — single class `AnthropicClient(firm_id)`. Methods: `complete(messages, *, model, max_tokens, system=None, tools=None, thinking=None)`, `embed(texts, *, model)`, `count_tokens(messages, *, model)`.
- `coworker/observability/token_meter.py` — Redis writer keyed `tokens:{firm_id}:{model}:{yyyy-mm-dd}`, 35-day TTL, periodic flush to Postgres.
- `coworker/connectors/__init__.py` — re-export.

**Design points:**
- PII scrub via `coworker.security.pii.PIIScrubber` runs on **every** message + system prompt before send. Placeholders restored on response text. **Black-box test asserts no TFN/ABN/Medicare leaves the process** — this is the load-bearing security guarantee.
- Model strings come from `Settings`. Never hardcoded.
- Extended thinking is opt-in via `thinking={"type": "enabled", "budget_tokens": ...}`. Default budget 16000 (32000 for specialists, set by caller).
- Rate limiting: defer to Anthropic's own headers + tenacity retry on 429 with `Retry-After`.
- Cache **disabled** by default at this layer. Caching is a Phase 4 hybrid-retriever concern.

**Test plan:**
- Unit: PII scrubber wraps every prompt; placeholder restoration round-trips.
- Integration with `respx`: 200 returns parsed text; 401 → `ConnectorAuthError`; 429 with `Retry-After: 1` retries once then succeeds; 5xx → `ConnectorTransient`.
- Black-box: pass a prompt containing a fake TFN; assert outbound HTTP body contains no digits matching TFN format.
- Token meter: run two `complete()` calls, assert Redis counter incremented by sum of input+output tokens.

**3B exit:** Anthropic connector covers complete + embed + count_tokens. Token metering CLI stub returns counters. PII black-box test green.

### 3C. Graph connector — breadth (build out from 3A's foundation)

Promote the day-one functions into the full connector surface. Keep the **module-functions hybrid shape** decided in Step 0 (deliberate divergence from arch §3.3 — see carry-forward §10.7).

**Read methods to add:**
- `get_message(ctx, message_id)`
- `get_attachment(ctx, message_id, attachment_id)`
- `list_calendar_events(ctx, *, start, end)`
- `list_drive_items(ctx, drive_id, item_id=None)`
- `download_drive_item(ctx, drive_id, item_id)` — streams to `tempfile.SpooledTemporaryFile`
- `get_user_profile(ctx)` — already partially in `current_user`; consolidate

**Write methods (shadow-guarded — see 3D):**
- `create_draft(ctx, *, to, subject, body, in_reply_to=None)`
- `mark_as_read(ctx, message_id)`
- `send_teams_message(ctx, channel_id, content)` — Graph chat, not webhook

**Subscription / app-only:**
- `subscribe_change_notifications(ctx, *, resource, expiration)`
- `renew_subscription(ctx, subscription_id, expiration)`
- App-only factory: `graph_app_context(firm)` for service-account workflows (subscriptions, indexer)

**Test plan:** every method gets a `respx`-mocked test for success + 401 + 429 + 5xx. Each write also gets a shadow-mode-blocks-it test (after 3D lands).

### 3D. Shadow-mode guard (cross-cutting)

**Files:** `coworker/connectors/shadow_mode.py` (new), with:
- `class ShadowModeBlocked(Exception)`
- decorator `@guard_writable(action_name)` that wraps connector write methods.
  - Reads `firm.shadow_mode` from the bound firm.
  - Honours `SHADOW_MODE_OVERRIDE_FIRMS` (comma-separated firm IDs from env) — if firm is in override and `shadow_mode=True`, block remains in force; override only applies when admin has run the explicit ceremony to set `shadow_mode=False`. Re-read after this is locked.
  - On block: writes audit `shadow_blocked.{action}` and raises `ShadowModeBlocked`.
- Test: parametric across all five connectors (where applicable).

This is one commit by itself, but **every** connector write method touches it. Land 3D before adding the writes in 3C/3E/3F/3G.

### 3E. XPM connector

**File:** `coworker/connectors/xpm_client.py`.

OAuth 2.0 with 60-day refresh tokens. Per-firm credentials in `firm.xpm_*_ciphertext` (need migration — first item below).

**New migration:** add columns to `firms`:
- `xpm_client_id_ciphertext bytea NULL`
- `xpm_client_secret_ciphertext bytea NULL`
- `xpm_refresh_token_ciphertext bytea NULL`
- `xpm_access_token_ciphertext bytea NULL`
- `xpm_token_expires_at timestamptz NULL`

**Methods:**
- Read: `list_clients(updated_since=None)`, `get_client(id)`, `list_jobs(client_id=None)`, `list_invoices(client_id=None)`, `get_invoice(id)`, `list_relationships(client_id)`
- Write (guarded): `create_client_note(client_id, body)`

**Test plan:** mocked OAuth refresh on near-expiry; 401 on expired; pagination follows `Link: rel=next`.

### 3F. FuseSign connector

**File:** `coworker/connectors/fusesign_client.py`. REST + API key (encrypted on firm row).

**New migration:** `firms.fusesign_api_key_ciphertext`, `firms.fusesign_webhook_secret_ciphertext`.

**Methods:**
- Read: `list_envelopes(status=None)`, `get_envelope(id)`
- Write (guarded): `create_envelope(...)`, `send_reminder(envelope_id)`, `register_webhook(url)`

### 3G. Teams connector

**File:** `coworker/connectors/teams_client.py`.

Two surfaces: legacy webhook URL (one-way notifications, encrypted on firm row) and Graph chat API for richer multi-direction. Most of Graph chat actually lives in 3C — this file holds the webhook surface.

### 3H. Token metering — Postgres flush

After 3B's Redis-only counters work, add daily Postgres flush:
- Migration: `token_usage` table with `(firm_id, model, day, input_tokens, output_tokens, count)`.
- Background flush task (called from APScheduler in Phase 6, but the flush function lands here).
- CLI: `coworker tokens --firm <slug> --month YYYY-MM` produces report.

### 3I. Outbound rate limiting — Redis sliding-window

**File:** `coworker/connectors/rate_limit_redis.py`.

Sliding-window via Redis sorted sets. Three windows configurable per connector method:
- per-plugin per-minute
- per-mailbox per-hour
- per-mailbox per-day

Replaces the in-memory bucket from 3A Step 3 (per CLAUDE.md / spec carry-forward §10.5 — production needs Redis-backed for multi-worker correctness). The in-memory bucket stays for unit tests.

### 3J. Phase 3 exit checklist

- [ ] All five connectors land with the read methods specified
- [ ] Every write method `@guard_writable`-decorated and tested
- [ ] Black-box PII test green: no TFN leaves the process to Anthropic
- [ ] Token metering CLI returns a usage report
- [ ] Rate limiter unit + integration tests cover all three windows
- [ ] Migrations up/down/up green for the three new column-additions and the `token_usage` table
- [ ] Adversarial test: connector method called outside `firm_context` returns zero rows / blocks the call (already true via existing RLS)
- [ ] Architecture doc §3.3 amendment drafted (carry-forward §10.7)
- [ ] Phase-wrap doc written; user signoff

---

## 4. Phase 4 — Memory Architecture (DEEP)

**Goal:** One Postgres database holds vector search, full-text search, knowledge graph, and learned lessons. ChromaDB and SQLite memory are gone. Hybrid retrieval ranks by BM25 + cosine + Sonnet rerank.

### 4A. Schema + migrations

Five tables minimum:
- `client_interactions` — `firm_id`, `client_entity_id`, `subject`, `summary`, `body`, `embedding Vector(1024)`, `tsv TSVECTOR` (weighted A/B/C), `created_at`, etc.
- `lessons` — `firm_id`, `text`, `embedding`, `tsv`, `priority`, `is_active`, `last_validated_at`, `decay_at`, etc.
- `documents` — `firm_id`, `source` (sharepoint|email_attachment|kb), `doc_type`, `extracted_data JSONB`, `embedding`, `tsv`, plus storage pointer to Spaces.
- `entities` — `firm_id`, `entity_type` (individual|company|trust|smsf|partnership), `name`, `display_name`, `xpm_client_id`, `kg_metadata JSONB`.
- `entity_relationships` — `firm_id`, `from_entity`, `to_entity`, `relationship_type`, `provenance JSONB` (source + first_seen + last_validated), `confidence`.

Indexes:
- HNSW on every `embedding` column (`vector_cosine_ops`)
- GIN on every `tsv` column
- Trigger maintains `tsv` with `setweight(to_tsvector('english', subject), 'A') || setweight(...,'B') || ...`
- B-tree on `(firm_id, created_at DESC)` for activity feeds

Plus `jobs` and `deadlines` tables (XPM mirror). Each tenant table gets RLS+FORCE.

### 4B. Embeddings abstraction

`coworker/memory/embeddings.py` — interface `Embedder`, implementations `VoyageEmbedder`, `OpenAIEmbedder`. Pluggable via `Settings.EMBEDDING_PROVIDER`. Default Voyage `voyage-3` 1024-dim.

Cache: Redis keyed by `sha256(model || text)`, 24h TTL.

### 4C. Hybrid retriever

`coworker/memory/retriever.py`:
- BM25 query: `ts_rank_cd(tsv, plainto_tsquery('english', :q))`
- Vector query: `embedding <=> :query_embedding`
- Run both in parallel via `asyncio.gather`
- Merge by RRF (reciprocal rank fusion) with `k=60`
- Pass top 20 to Sonnet rerank; return top N with rerank scores
- Lesson-priority weight multiplier on lessons (priority * 1.2 boost)
- Cache combined result (Redis) keyed on (firm_id, query, context, k)

### 4D. KG populators

- `coworker/knowledge_graph/xpm_sync.py` — nightly job pulls XPM client tree, upserts entities + relationships with provenance `{source: "xpm", synced_at: ...}`.
- `coworker/knowledge_graph/email_extractor.py` — agent-loop extractor invoked from `correspondence_logger` plugin (Phase 6) but landing here as a callable.
- `coworker/knowledge_graph/sharepoint_resolver.py` — folder-name → entity-name fuzzy matcher.

### 4E. SharePoint indexer (server-side)

`coworker/workers/sharepoint_indexer.py`. Graph delta queries; full text → Spaces; snippet → `documents.body` + `documents.embedding`; vision pipeline triggered for PDFs (stub for now, real impl in Phase 7).

Replaces the v2.2 per-install indexer.

### 4F. Phase 4 exit checklist

- [ ] HNSW indexes built; `EXPLAIN ANALYZE` confirms HNSW use on `... ORDER BY embedding <=> $1 LIMIT 25`
- [ ] FTS triggers maintain `tsv` correctly under insert + update
- [ ] `HybridRetriever.retrieve` returns expected order across BM25 + vector + rerank in fixture corpus
- [ ] Cache hit p95 < 50ms (measured)
- [ ] Cross-firm isolation: store lesson for Firm A, query as Firm B, returns zero results (RLS in effect)
- [ ] XPM nightly sync populates entities + relationships against a sandbox tenant
- [ ] SharePoint indexer handles a 1,000-file folder backfill within 30 minutes (measured)
- [ ] Migrations up/down/up clean
- [ ] Phase-wrap doc; user signoff

---

## 5. Phase 5 — Orchestrator (DEEP)

**Goal:** Replace per-plugin Anthropic calls with a unified agent loop using Claude's native tool use. Every step traceable. Cost-bounded.

### 5A. Tool registry

`coworker/orchestrator/tools.py`:
- `class Tool(BaseModel)` declares: `name`, `description`, `category`, `input_model: type[BaseModel]`, `cost_estimate_cents: int`, `side_effect: bool`, `handler: Callable[[ToolInput, AgentContext], Awaitable[ToolResult]]`.
- Registry is a singleton populated at import time via `@register_tool` decorator.
- Plugins enable tool **categories** in their config; the orchestrator filters at runtime.

Categories: `memory`, `kg`, `xpm`, `email`, `calendar`, `fusesign`, `teams`, `vision`, `approval`, `reasoning`.

### 5B. Standard tool catalogue (~40 tools)

Land in their respective categories — each is a thin wrapper around the connector / memory / KG layer:
- Memory: `memory_search_interactions`, `memory_search_lessons`, `memory_get_document`, `memory_recent_for_entity`
- KG: `kg_get_entity`, `kg_relationships_of`, `kg_resolve_name`, `kg_record_observation` (queue for review, never auto-write)
- XPM: `xpm_get_client`, `xpm_list_jobs`, `xpm_get_invoice`, `xpm_create_client_note` (side_effect=True)
- Email: `email_get_message`, `email_get_thread`, `email_create_draft` (side_effect=True), `email_mark_as_read` (side_effect=True)
- Calendar: `calendar_get_user_events`, `calendar_get_firm_availability`
- FuseSign: `fusesign_get_envelope`, `fusesign_create_envelope` (side_effect=True), `fusesign_send_reminder` (side_effect=True)
- Vision: `vision_classify`, `vision_extract` (Phase 7 stubs)
- Approval: `approval_queue_item` (side_effect=True; queues but does not execute)
- Reasoning: `consult_specialist` (side_effect=False, but $$$ — deeply costed)

### 5C. Agent loop

`coworker/orchestrator/engine.py`:

```python
class OrchestratorEngine:
    async def run(self, ctx: AgentContext) -> TraceResult: ...
```

Inside:
1. Resolve enabled tool categories from plugin config
2. Build Anthropic `tools=` argument from registry
3. Loop:
   - Call Anthropic with `messages` + `tools` (+ `thinking` if enabled)
   - If `stop_reason == "tool_use"`: dispatch tool calls; append tool_result blocks; continue
   - If `stop_reason == "end_turn"`: terminate, completion_reason="ended_normally"
   - If iteration count == max_iterations (12): terminate, completion_reason="max_iterations"
   - If accumulated cost > budget: terminate, completion_reason="budget_exhausted"
4. Persist `agent_traces` row + `agent_trace_steps` rows for every model/tool call

### 5D. Trace tables + prompt-version pinning

Migration:
- `agent_traces` — `firm_id`, `plugin_installation_id`, `started_at`, `ended_at`, `completion_reason`, `cost_cents`, `metadata JSONB`
- `agent_trace_steps` — `trace_id`, `step_index`, `kind` (model_call|tool_call), `input JSONB`, `output JSONB`, `duration_ms`, `cost_cents`, `metadata JSONB`

`metadata.specialist_prompt_version_id` is **mandatory** on any step where the engine invoked a specialist — enforced at the engine layer, not by convention. Reproducibility query: given a trace, the exact prompt text used is reconstructable.

### 5E. Extended thinking opt-in

- `AgentContext.extended_thinking: bool` (default False)
- Auto-enable rule: `consult_specialist` invocation, OR `cost_estimate_cents > 50`, OR `plugin.requires_extended_thinking`
- Default budget 16000; specialists override to 32000

### 5F. Cost guards

- Per-context `budget_cents` from plugin config
- Each model call estimates cost from token count + model price; running total
- If exceeded, loop terminates with `completion_reason="budget_exhausted"` and queues current draft (if any) for approval

### 5G. Phase 5 exit checklist

- [ ] `coworker debug replay-trace <id>` reconstructs the full transcript from DB
- [ ] Tool errors don't crash loop — returned as `tool_result` with `is_error=true`; Claude reasons about failure
- [ ] Cross-firm tool isolation: tool invoked in firm A cannot read or write firm B data even if the agent attempts it
- [ ] Specialist invocation path pins `specialist_prompt_version_id` on every step (test fixture covers this)
- [ ] Cost guard test: a deliberately-expensive scripted run terminates with `budget_exhausted` and queues approval
- [ ] Migrations up/down/up clean
- [ ] Phase-wrap doc; user signoff

---

## 6. Phases 6 → 16 — strategic outline

Less detail per phase. Filled out at the boundary.

**Phase 6 — Plugin system.** Server-native plugins that declare goal + tool categories; orchestrator runs them. APScheduler with Redis lock for single-leader. Worker pool consuming `queue:plugin_runs`. Sandbox (subprocess + seccomp + network whitelist) for marketplace plugins. Dry-run mode that intercepts side-effect tools. The 14 builtin plugins (`smart_responder` first, `proactive_intelligence` last). **Risk:** the sandbox is the highest-risk surface in v3. Land it with a deliberately-misbehaving test plugin (network exfil, fork bomb, infinite loop) green.

**Phase 7 — Vision.** PDF triage → render → classify → extract → validate → KG-merge. Pydantic schemas per doc type. **Last-4 only TFN rule** baked into extractor (unit test asserts no full TFN ever stored). Page renders cached in Spaces. Extractors versioned for re-extraction.

**Phase 8 — Specialists.** Six specialists (`gst`, `smsf`, `div7a`, `trust_tax`, `tax_structure`, `audit_review`). Each is a markdown prompt + registry entry. Sub-agent loop with Opus 4.7 + 32k thinking + restricted tools. **Specialist Prompt Management UI** is the substantial piece — version table, draft/active/retired, diff-from-previous, Sonnet diff-safety summary, holdout preview with judge model, two-person promotion for `tax_structure`/`audit_review`. Style learning per user (nightly job analyses sent emails → `User.style_profile` JSONB).

**Phase 9 — Approval queue.** `approval_items` table; self-consistency confidence (sample N=5 at 0.7, pairwise embedding similarity, length-cv penalty); risk-tiered routing (read-only auto, low-risk auto-with-audit, medium gated, high always queued, two-person for engagement_letter / formal demand / new client). SLA per category; expires at SLA × 3. Edit-and-approve persists original + edited; diff feeds reflection.

**Phase 10 — Web frontend.** React 18 + Vite + TanStack Router/Query + Tailwind + shadcn/ui + Reactflow + Recharts + OpenAPI-codegen. 11 pages (Dashboard, Approvals, Plugins, Memory, KG, Activity, Findings, Chat, Settings, Specialist Prompts, Onboarding). WebSocket `/api/ws/live` for push updates. WCAG AA. Lighthouse ≥ 90 on all four axes. Approvals page virtualised for 200+ items. Frontend scaffolding (build, routing, shadcn) can be parallelised earlier — page builds wait for their backing API.

**Phase 11 — Real-time, reflection, proactive.** Graph webhooks (`subscriptions` table; hourly renewal ≥ 12h before expiry); webhook receiver returns 202, enqueues to `queue:graph_events`. Nightly reflection at 02:00 firm-local: trace analysis → lesson candidates → embedding cluster → synthesise → store as draft (auto-active above 0.8); decay stale lessons; refresh style profiles; **verify audit chain → daily anchor email to principal**. Proactive intelligence Mondays 7am — single Outlook draft to principal with 3–7 ranked findings.

**Phase 12 — Mobile / multi-device / calendar.** PWA manifest + service worker + iOS/Android install. Mobile approvals: swipe gestures, FAB, collapsed trace. Web Push (configurable channels per user; principal-only critical channel for audit-chain alerts). Calendar tools (`calendar_get_user_events`, `calendar_get_firm_availability`) so Smart Responder can suggest meeting slots with "Calendar checked" provenance. Multi-device session list + force-logout via JWT revocation list (closes carry-forward §10.2 + §10.3).

**Phase 13 — Onboarding.** First-run wizard, 11 steps, target < 1 hour from "we want to try this" to "first plugin produced first draft." Per-firm Azure AD app: Path A (deep link with pre-filled manifest) or Path B (guided live doc). Default plugin enablement by role. Shadow-mode graduation requires re-auth + 7-day minimum review + audit-chain verify. Ceremony's audit row carries the chain anchor at that moment.

**Phase 14 — Operations.** Prometheus + node/postgres/redis exporters + per-service custom metrics. Grafana dashboards (service health, agent activity, approval queue, token economics, memory growth, DB health, system). External uptime check (Better Stack) — pages on two consecutive failures. pgBackRest to Spaces (Sun full / Mon-Sat diff / continuous WAL); nightly `pgbackrest verify`. **DR runbook** (provision → restore secrets → install stack → restore Postgres → DNS → re-issue TLS → start in order → re-subscribe Graph). **Quarterly DR drill** against staging droplet — non-negotiable. KPI dashboard at `/kpi` for principals.

**Phase 15 — Migration.** One-time forward script: `memory_lessons.sqlite` → `lessons` (re-embed); ChromaDB → `client_interactions` (re-embed; metadata flat); `knowledge_base/` → `documents` `doc_type='knowledge_base'`; `bas_clients.json` → `deadlines`; `*_state.json` discarded (recompute); `approval_history.db` → audit + reflection input; v2.2 chat-generated plugins → manual list, no auto-migrate. Idempotent: dedup on text exact match + embedding distance < 0.05.

**Phase 16 — Cutover.** Procedural, not constructive. **16A** extended shadow (Elio-only week → two-user week, exit on zero severity-≥-medium for 7 days). **16B** single-user pilot (v2.2 disabled on Elio's machine, two weeks zero severity-1). **16C** internal cutover (drain v2.2 queue → disable v2.2 schedulers → subscribe v3 → 4-hour watch → enable side-effect tools via shadow_mode=False ceremony → uninstall v2.2 after 7 days clean). Plugins migrate auto-execute one at a time, ordered by risk: `correspondence_logger` first, `engagement_letter` last (always two-person). **16D** external distribution — second firm onboards with the wizard. Capture friction in `docs/onboarding-playbook.md`. **16E** v2.2 decommission after 90+ days v3 sole.

---

## 7. Cross-cutting concerns (active across all phases)

### 7.1 Security posture
- Every credential `EnvelopeCipher`-encrypted with `firm_id` AAD. New connector ⇒ new ciphertext columns ⇒ new migration ⇒ retest cross-firm AAD binding.
- Every write through a connector method decorated `@guard_writable`.
- Every Anthropic call PII-scrubbed.
- Every audit append goes through `append_audit` (chain integrity preserved).
- Loguru patcher pattern list reviewed at every connector landing — new secret patterns get added to the redaction set.

### 7.2 Multi-tenancy
- New table ⇒ `firm_id NOT NULL FK` + index + RLS+FORCE + 4 policies (SELECT/INSERT/UPDATE/DELETE) — no exceptions.
- New API route ⇒ enters `firm_context` once at the top.
- New worker ⇒ pulls `firm_id` from queue payload, enters `firm_context` before any DB op.

### 7.3 Testing posture
- Unit for pure logic.
- Integration for anything that touches Postgres/Redis/HTTP — `respx` for HTTP, real DB for the rest.
- Adversarial test for every security primitive (cross-firm AAD, audit tamper, RLS pool reuse, etc.).
- Black-box test for PII (no TFN/ABN in outbound HTTP body).

### 7.4 Operational posture
- Structured loguru everywhere. Never `print` in committed code.
- Feature flags via `Settings`, never inline booleans.
- Every long-running operation has a progress signal (loguru.info every N items or % done).

---

## 8. Carry-forward register (open items, decisions, debts)

These are alive between phases. Each line ends with the phase that closes it.

| # | Item | Closes in |
|---|------|-----------|
| 1 | Composite UNIQUE migration on `users.azure_object_id`, `users.upn` | Phase 3 (before any code path assumes cross-firm UPN reuse) |
| 2 | JWT revocation list (Redis revoked-jti) — producer side | Phase 12 (with logout) |
| 3 | Microsoft refresh-token revocation on logout | Phase 12 |
| 4 | `id_token` JWKS verification — only if it becomes load-bearing for authz | Watch indefinitely |
| 5 | Rate limiter Redis migration (replace in-memory bucket) | Phase 3I |
| 6 | Architecture doc §2.6 amendment — `GRAPH_SCOPES` reflects post-`5eda67c` | Phase 3 wrap |
| 7 | Architecture doc §3.3 amendment — formally rescind `GraphClient` class shape | Phase 3 wrap |
| 8 | `alg=none` test for `decode_session_jwt` | Phase 3A |
| 9 | Cross-firm case in byte-identical 401 body test | Phase 3A |
| 10 | Untracked `auth.p` and `auth.py.bak` cleanup | Phase 3A pre-flight |
| 11 | TestClient + asyncio.run pattern documented for sync TestClient + async DB | Already documented; reuse |
| 12 | Dual-site monkeypatching documented | Already documented; reuse |

### 8.1 Decisions deferred to Elio

- **Embedding provider.** Voyage `voyage-3` (default per arch) vs OpenAI `text-embedding-3-large`. Architecture says "side-by-side benchmark in Phase 4." Recommendation: ship with Voyage, add OpenAI adapter, run benchmark mid-Phase-4 against MC&S corpus, pick winner before exit.
- **Specialist prompt distribution model.** Per-firm copies (Phase 8 default) vs shared baseline + per-firm overrides. Recommendation: per-firm copies until firm #2, revisit then.
- **Backup encryption.** Spaces server-side only vs client-side encryption (key in same vault as master). Recommendation: client-side at Phase 14 — defensive depth justifies the operational complexity.
- **Multi-droplet trigger.** Threshold to scale out (>60% CPU business hours OR 5+ firms). Recommendation: hard alert at 60% / 4 firms; soft alert at 50% / 3 firms.

---

## 9. Phase exit ritual (every phase)

1. Run full pytest. Green.
2. Run `mypy --strict`. Clean.
3. Run `ruff check`. Clean.
4. Migration up/down/up if any migration shipped.
5. Update `MCS-coworker-V3-spec.md` status table.
6. Update this plan doc — mark phase complete, add any new carry-forward items.
7. Write phase-wrap commit summary covering: what shipped, what tests cover it, what decisions were made, what carry-forward items moved.
8. **Pause.** User reviews. User signs off in conversation. Then we begin the next phase.

The phase is not "done" until this ritual is complete.
