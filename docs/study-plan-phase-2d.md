# Study Timeline — Phase 2D mutation integration

For Swift S1 field nesting, UUID epochs, nullability, captured JSON, and the exported OpenAPI schemas, see the [exact API contract](study-plan-phase-2d-api-contract.md).

Existing Study Plans now adapt automatically after meaningful progress, reset, content, generation, and exam mutations. Every hook calls Phase 2C's `recalculate_plan`; this phase adds no scheduling algorithm, AI adaptation, migration, endpoint, response field, event bus, listener, or background job.

## Hook locations and transaction boundaries

| Mutation | Hook location | Trigger and transaction |
| --- | --- | --- |
| Learning submission | `app/services/study_progress.py`, `submit_progress` | After newly accepted facts and the immutable receipt are flushed, before the router commits. Only decks with newly accepted card IDs contribute roots. Progress, receipt, and all affected plans commit or roll back together. |
| Progress reset | `app/services/study_progress.py`, `reset_progress` | After facts are cleared, epochs rotated, and the reset receipt flushed. Recalculate resolved chapter roots before the same commit. |
| Manual card creation | `app/routers/cards.py`, `create_card` | Lock the user before reading the owned deck; insert, recalculate, and commit together. The new card is unlearned. |
| Manual card deletion | `app/routers/cards.py`, `delete_card` | Lock the user before reading the owned card; resolve its deck before deletion, delete, recalculate, and commit together. |
| Bulk card replacement | `app/routers/cards.py`, `create_or_update_cards_bulk` | User lock covers the whole batch. After all insertions/deletions, adapt once before commit. Text/image-only updates and unchanged membership skip adaptation. |
| Generation completion | `app/services/deck_generation.py`, `_finalize`, through `app/services/study_plan.py`, `reconcile_generated_plan` | Commit each completed chapter's cards in a short transaction. Once every chapter is complete, final parent state and canonical adaptation share one final transaction. No hook per generated card or chapter. |
| First exam achievement | `app/services/exam_submission.py` | After flushing an attempt and its first successful milestone transition, adapt before the existing commit. A failed attempt or already-passed retake skips adaptation. |

All paths retain the existing user-first locking discipline. Canonical recalculation then owns its existing parent/plan/chapter locking and historical snapshot behavior. The helper flushes pending mutations so recalculation sees this transaction's facts and receipt, including late-learning attribution. An error on a later affected plan rolls back earlier plan changes in that same mutation.

## Orchestration and deduplication

`app/services/study_plan_hooks.py` contains `recalculate_affected_plans(db, user_id, root_ids, reason=...)`. It deduplicates explicit root IDs, finds only existing owned plans, processes them in stable root order, and delegates each once to `recalculate_plan`. It never commits or creates a plan. It returns plan/root IDs, old/new revisions, a `changed` property, and whether readiness caused deferral for internal use.

Study All across eight chapters of one parent invokes one canonical recalculation; different parents each receive one call. Bulk card membership changes also invoke one call per affected plan. Unchanged recalculation results retain the existing revision semantics: invoking a hook alone does not force a revision bump.

Submission and reset receipt replays return before the hook, even on a later calendar date. A new progress receipt containing only already-learned cards also skips adaptation. Stored receipt responses remain immutable.

Current plans cover a root's immediate child chapters. Direct root cards and standalone cards remain outside those plans. Reset resolves a parent to its child chapters using the existing scope rules. Missing plans are skipped; no implicit plan or standalone-plan support is introduced.

There is **no existing study-plan preference update endpoint**. Phase 2D does not add one, so preference-update hooks and the four requested preference-endpoint tests are not applicable. A future preference endpoint should update saved preferences and call this same canonical service in one transaction.

## Failure behavior and generation recovery

- Only the expected HTTP 409 `study_plan_content_not_ready` is deferred by automatic hooks. Phase 2C checks readiness before changing schedule/history rows. The otherwise valid primary mutation may commit; the existing plan waits for completed generation or a later recalculation.
- `projection_blocked: true` is a valid plan result and commits normally, including when deleting all cards from a required chapter.
- Other synchronous adaptation failures propagate and roll back progress, receipts, resets, card mutations, or exam attempts/achievements together with timeline changes. They are not silently treated as successful adaptation.
- Historical targets, dates, IDs, and learning cycles follow Phase 2C preservation rules. Reset/deletion cannot lower captured historical credit; newly accepted late evidence can increase matching credit. Today's open projections remain revisable.

Generation releases database locks before awaiting AI. After AI returns, it reacquires the user lock and rereads chapter state before inserting cards, skipping insertion if another worker already completed that chapter. Completed chapter/card data is committed independently of finalization.

If some chapter generation fails, completed chapters remain stored, parent/job report failure through existing behavior, and adaptation waits until all chapters are ready. If final adaptation fails, its transaction rolls back, the parent is marked failed, and the existing generation worker records a recoverable job error:

```text
Study plan finalization failed; completed chapters are retained. Retry generation to reconcile without regenerating completed chapters.
```

The existing authenticated `POST /ai/decks/{deck_id}/retry` atomically resets parent/job retry state under the user lock. Generation skips completed chapters; it regenerates only unfinished chapters and retries finalization. If every chapter was already complete, retry performs no AI requests. A completed generation rerun performs no additional reconciliation or card insertion.

Explicit study-plan recalculation can repair a plan once chapter content is ready, but does not finalize the generation job/parent status; use generation retry to recover that lifecycle state.

## API and later Swift handoff

**Request and response schemas are unchanged.** Progress/reset responses remain compact and preserve exact receipt replay; no full timelines or affected-plan revision arrays are embedded. Swift can fetch the current plan after a successful relevant mutation:

```http
GET /decks/{deck_id}/study-plan
```

This GET remains read-only. Automatic hooks reduce the need for client-orchestrated recalculation, but do not run just because a new day begins or an unchanged receipt is replayed. The existing endpoint remains available for recovery, manual refresh, debugging, and local-calendar rollover:

```http
POST /decks/{deck_id}/study-plan/recalculate
```

It remains authenticated, takes no request body, and returns the updated plan directly. Its existing errors, including readiness conflicts, are unchanged; readiness deferral applies only to automatic mutation hooks. See [Phase 2C's API contract](study-plan-phase-2c.md#explicit-api) and [example response](examples/study-plan-recalculate.json).

No Swift integration, notification scheduling, daily cron, or health classification was started. Those remain subsequent work requiring user confirmation.

## Observability

Enable `app.services.study_plan_hooks` at INFO in the host's logging configuration to capture:

```text
study_plan_adapted_pending_commit plan_id=... reason=... old_revision=... new_revision=...
study_plan_adaptation_deferred plan_id=... reason=... revision=... content_not_ready
```

The pending-commit name deliberately identifies an in-transaction result, not a durability guarantee: the caller may still roll back. Reasons are `learning_progress`, `progress_reset`, `card_added`, `card_deleted`, `cards_replaced`, `generation_completed`, and `exam_passed`. Logs contain no card contents or request payloads. Generation retry/finalization failure logs include attempt counts or parent ID and exception type, without generated content.

## Validation and deployment

Full suite: **190 tests passed, no skips** — 163 SQLite/pure/API tests and 27 PostgreSQL tests. Phase 2D adds 36 tests: 29 in `tests/test_study_plan_hooks.py` and seven in `tests/test_study_plan_hooks_postgres.py`. Earlier assertions that required no automatic mutation were updated to the new contract while retaining history and exam-rule coverage.

Coverage includes qualifying progress, receipt replay, already-learned-only submissions, same/different-root deduplication, multi-plan rollback, chapter/parent resets, stale epochs, historical preservation, card add/delete/bulk replacement, text-only skipping, ownership, missing plans, readiness deferral, first/failed/repeated/second-half/final exam results, generation batching, partial failure, finalization recovery without repeated AI, and concise logging.

PostgreSQL tests exercise duplicate concurrent submissions, both progress/reset orderings, card creation racing explicit recalculation, both exam/reset orderings, and a second database session acquiring the user lock during an AI await. The full suite also retains existing real migration, revision, and locking tests.

```sh
.venv/bin/python -W ignore::DeprecationWarning -m unittest discover -s tests -v
TEST_DATABASE_URL='postgresql+psycopg://USER@HOST:PORT/TEST_DB' .venv/bin/python -W ignore::DeprecationWarning -m unittest discover -s tests -v
```

Without `TEST_DATABASE_URL`, 27 PostgreSQL tests skip. Use a disposable PostgreSQL database/account permitted to create/drop isolated test schemas.

No new migration is needed. The database must already have Phase 2C head `d04a7e3f9b62`. Validation used an isolated local PostgreSQL server and disposable schemas; the running API database was not migrated, modified, or restarted during Phase 2D.
