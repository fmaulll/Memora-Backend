# Exact-card learning progress — Phase 2A

Phase 2B is now implemented; see [chapter aggregates and exam eligibility](study-progress-phase-2b.md) for the additive read fields and current exam rules. This document records the Phase 2A synchronization contract; its later-phase notes below describe the boundary at the end of 2A.

Implemented scope: durable learned facts, retry-safe submissions, explicit resets, and stale-offline-write protection. Chapter completion/exam gates (2B), adaptive scheduling (2C), and integration hooks (2D) are deliberately separate steps. Phase 1 plan items, revisions, scheduling, and current exam eligibility are unchanged by submissions and resets in 2A. No AI calls, spaced-repetition state, queue state, or health classifications were added.

## Learning contract

A qualifying transition is **Got It accepted while that exact card is in REVIEW**. Swift sends the exact card ID with `phase: "review"` and `answer: "got_it"`. Both fields are required; NEW/Got It and all Again reports are rejected by this endpoint. This explicit client report is the synchronization boundary: the backend validates the declared transition and ownership but does not recreate or independently observe Swift's queues.

Swift must capture the phase **before** applying its local transition. Never derive learned facts from `correctCount`, `reviewCount`, session counts, queue absence, percentages, or a Mastered label. Send only qualifying transitions, after a meaningful batch/session boundary. If there are no qualifying transitions, there is nothing to submit.

A first qualifying report sets `learned_at` to the request's `completed_at`. Later reports of the same learned card do not increment anything or overwrite that timestamp. A later Again remains a Swift-only review-quality event. Learned status is cleared only by an explicit reset (or removal of the underlying card).

## Storage and transactions

- `decks.progress_epoch`: opaque UUID, populated for every existing/new deck. Reset replaces it for each affected card-owning deck. It is independent of `StudyPlan.revision` and does not advance with ordinary learning.
- `card_progress`: composite primary key `(user_id, card_id)`, nullable `learned_at`, UTC creation/update timestamps. A missing row and a row with `learned_at: null` both mean unlearned. There is no backend NEW/REVIEW/DONE enum.
- `study_progress_receipts`: composite primary key `(user_id, operation_id)`, operation kind, canonical request hash, original response JSON, creation timestamp. Session and reset UUIDs share this namespace. Reusing a UUID for different content or a different operation returns 409.

Receipts retain exact accepted/already-learned card IDs or reset results without introducing a result row per card. JSON is an immutable acknowledgement, not the source of current progress. Card deletion cascades the current progress row; receipts survive card/deck deletion. Deleting the user cascades their progress and receipts. The existing backend hard-deletes cards; there is no separate active/deleted flag to interpret.

Progress mutations and snapshot reads acquire the authenticated user's PostgreSQL row lock. Submissions additionally lock owned decks and referenced cards; resets lock their resolved scope. Validation, mutations, and receipt insertion occur in one caller-owned transaction. A failed request leaves no partial learning/reset or success receipt. Different users do not share this lock.

UUID epochs also distinguish a newly created deck from a deleted deck that happened to use the same deck UUID. An old numeric generation starting over at zero would not provide that guarantee.

## Read current facts

`GET /decks/{deck_id}/study-progress` → HTTP 200, authenticated.

The server resolves the same scope used by reset:

- Child chapter: that chapter only.
- Root with direct children: those owned, existing children, ordered by `(position, id)`; direct root cards are excluded.
- Standalone root: that deck.

Example response for a standalone deck with one unlearned card:

```json
{
  "deck_id": "10000000-0000-4000-8000-000000000001",
  "decks": [
    {
      "deck_id": "10000000-0000-4000-8000-000000000001",
      "progress_epoch": "20000000-0000-4000-8000-000000000001",
      "title": "Biology",
      "parent_deck_id": null,
      "position": 0,
      "generation_status": "completed",
      "cards": [
        {"card_id": "30000000-0000-4000-8000-000000000001", "learned_at": null}
      ]
    }
  ]
}
```

All existing cards in the resolved scope are returned. Pending chapters can contain card facts, but this response does not assert chapter completion. The endpoint performs no writes. Swift should keep backend chapter order rather than sorting tied positions by title. Card ordering in this payload is stable by ID and is **not** an instruction for local queue ordering.

## Submit qualifying learning

`POST /study/progress/submissions` → HTTP 200, authenticated.

A global route allows Study All to send exact-card transitions from multiple decks in one transaction and one idempotency receipt. It uses the same progress system as a single-deck session. Each group's `deck_id` must be the card's actual owning deck/chapter, not its ancestor.

```json
{
  "session_id": "40000000-0000-4000-8000-000000000001",
  "completed_at": "2026-09-24T02:00:00Z",
  "decks": [
    {
      "deck_id": "10000000-0000-4000-8000-000000000001",
      "progress_epoch": "20000000-0000-4000-8000-000000000001",
      "learned_cards": [
        {
          "card_id": "30000000-0000-4000-8000-000000000001",
          "phase": "review",
          "answer": "got_it"
        }
      ]
    }
  ]
}
```

`session_id` identifies one immutable upload batch. If a local Swift session produces several uploads, give **each upload a fresh UUID**; retries of that upload retain its UUID, completed time, epochs, and content. The backend does not synchronize session resume or queues.

Response:

```json
{
  "session_id": "40000000-0000-4000-8000-000000000001",
  "completed_at": "2026-09-24T02:00:00Z",
  "submitted_at": "2026-09-24T02:00:02Z",
  "decks": [
    {
      "deck_id": "10000000-0000-4000-8000-000000000001",
      "progress_epoch": "20000000-0000-4000-8000-000000000001",
      "accepted_card_ids": ["30000000-0000-4000-8000-000000000001"],
      "already_learned_card_ids": []
    }
  ]
}
```

Limits/rules:

- At most 100 deck groups and 5,000 unique transitions per request; each group is nonempty. Split larger uploads into immutable batches.
- Duplicate card IDs within a group are deduplicated. Duplicate deck groups are rejected. Reordering groups/cards or using an equivalent timestamp offset does not change idempotency identity.
- `completed_at` must include an offset. It is normalized to UTC and may be historical for offline synchronization. Values more than five minutes ahead of server time are rejected.
- All deck epochs must match. One stale epoch rejects the entire new submission, including otherwise valid groups; nothing partially applies.
- Deleted, foreign, or misgrouped cards reject the entire new submission. Added cards start unlearned. Existing cards may be reported during pending generation, but only 2B will decide whether their chapter is eligible for completion.
- Client-supplied user IDs, aggregate completion counts, and other undeclared fields are rejected. Authentication supplies the user.

## Reset

`POST /decks/{deck_id}/study-progress/reset` → HTTP 200, authenticated.

Obtain the scope/epochs from GET. Include exactly those decks in `expected_decks`; the body cannot expand the reset to another deck.

```json
{
  "reset_id": "50000000-0000-4000-8000-000000000001",
  "expected_decks": [
    {
      "deck_id": "10000000-0000-4000-8000-000000000001",
      "progress_epoch": "20000000-0000-4000-8000-000000000001"
    }
  ]
}
```

Response:

```json
{
  "reset_id": "50000000-0000-4000-8000-000000000001",
  "deck_id": "10000000-0000-4000-8000-000000000001",
  "reset_at": "2026-09-24T03:00:00Z",
  "decks": [
    {
      "deck_id": "10000000-0000-4000-8000-000000000001",
      "previous_epoch": "20000000-0000-4000-8000-000000000001",
      "progress_epoch": "20000000-0000-4000-8000-000000000002",
      "cleared_card_count": 1
    }
  ]
}
```

Reset clears canonical learned timestamps for the current cards and rotates the affected epochs atomically. Empty/pending decks also receive new epochs. Other chapters and standalone decks retain their epochs and facts. Swift separately handles its existing local queue/statistics reset.

If membership changed since GET, return `progress_scope_changed`; if an epoch changed, return `stale_progress_epoch`. Refetch before making a new reset decision. A retry with the same reset UUID and original content returns its original receipt and cannot clear learning recorded afterward.

## Offline/reset races and receipt replay

Swift must save the epoch with the local study context **before** recording qualifying transitions. Do not fetch a new epoch and attach it to old unsynced events. Do not infer/backfill progress from existing Swift counters during rollout. Devices need an initial epoch fetch before using this synchronization contract; pre-integration learning requires a separately agreed migration policy.

For a new submission racing a reset:

1. If submission commits first, reset clears it.
2. If reset commits first, the old-epoch submission gets 409 and creates no fact or receipt.

An **already accepted** submission replayed after a reset/deletion returns its original acknowledgement and does not touch current facts. Consequently, a receipt is not a fresh state snapshot. Its `accepted_card_ids` describes the original operation. Swift must not restore local learned state from a receipt with an old epoch; fetch current facts when reconciling. UUID epochs are opaque—compare equality, never numeric or lexical order.

After a rejected Study All submission, Swift can refetch and make a new upload for unaffected valid transitions, retaining the epochs under which those transitions actually occurred. It must discard/reconcile stale transitions rather than relabel them. Changed upload content should use a new UUID.

## Errors

Domain failures use `{"detail":{"code":"stale_progress_epoch","message":"..."}}`. Pydantic shape/literal validation uses FastAPI's normal 422 `detail` array. Authentication follows the existing dependency.

| HTTP | Code | Meaning |
|---|---|---|
| 404 | `deck_not_found` | Missing deck or deck owned by another user |
| 404 | `user_not_found` | User disappeared before the transaction acquired its lock |
| 409 | `idempotency_conflict` | UUID already accepted with another payload/operation |
| 409 | `stale_progress_epoch` | A supplied epoch no longer matches |
| 409 | `progress_scope_changed` | Reset scope differs from the expected deck set |
| 422 | `card_not_in_deck` | Card missing/deleted or grouped under the wrong owned deck |
| 422 | `invalid_completion_time` | Completion timestamp too far in the future |

## Next sub-phases

- **2B:** derive chapter completion once, from generation readiness, a nonempty active card set, and all cards learned. Reuse `(position, id)` and `split_chapters` for exam halves. The proposed one-chapter rule is final unlocked after first-half pass, with second half not applicable. That rule is not yet enabled by 2A.
- **2C:** use a hybrid history model: current work derives from exact-card facts; closed past targets retain snapshots so content deletion or reset cannot rewrite past performance. Define late-offline historical attribution before implementing it. No mutable timeline completion counters were added here.
- **2D:** explicit mutation hooks adapt plans after content/generation/exam changes. No hidden SQLAlchemy event listeners or per-answer recalculation.

## Files and migration

- `models/deck.py`: epoch on the existing card-owning entity, avoiding a separate epoch table.
- `models/study_progress.py`: current facts and receipt storage.
- `schemas/study_progress.py`: strict transition/reset requests and compact responses.
- `services/study_progress.py`: validation, scope/epochs, receipts, and transaction-local mutations; no scheduler or exam imports.
- `routers/study_progress.py`: three endpoints; explicit commit/rollback on mutations.
- Model/router registration in `models/__init__.py`, `main.py`, and `alembic/env.py`.
- `tests/test_study_progress.py` and `tests/test_study_progress_postgres.py`: API semantics, rollback, real concurrent duplicates/reset races, deletion, and migration checks.

Migration `c93f6d2e8b51` follows the verified sole Phase 1 head `b82e5c9d1a40`. It backfills a distinct UUID epoch for each existing deck without inferring any learning, then adds the two progress tables. Existing migrations and plan tables are unchanged. Downgrade removes Phase 2A facts/receipts/epochs but preserves users, decks, cards, plans, and exams.

This change was tested in disposable PostgreSQL schemas. The development database was not migrated. Apply the migration before starting the updated API/worker, since Deck now maps the epoch column:

```sh
.venv/bin/alembic upgrade head
```

Tests:

```sh
.venv/bin/python -m unittest discover -s tests -v
```

Provide `TEST_DATABASE_URL` for the PostgreSQL cases. Those tests only create/drop their own random schemas. No Swift files were modified and no Swift queue behavior is claimed as tested here.
