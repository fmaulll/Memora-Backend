# Chapter progress and exam eligibility — Phase 2B

Phase 2B extends the existing progress and exam reads. It adds no tables, migration, writable completion flag, or adaptive scheduling. The database must already have the Phase 2A migration `c93f6d2e8b51`. Phase 1 schedule items/dates/revisions remain projections; learning and passing exams do not reschedule them. Phase 2C requires separate approval.

## Canonical rules

`services/chapter_progress.py::get_chapter_progress` is the shared batched aggregate. It counts current cards and joins only the authenticated owner's `card_progress.learned_at` facts. Missing/null learned facts mean unlearned; hard-deleted cards disappear from both counts. Planned `Deck.card_count`, sessions, exam attempts, queues, and timeline items never supply learning counts.

For each chapter:

- `total_card_count`: current persisted cards, including zero.
- `learned_card_count`: current cards with this user's non-null `learned_at`.
- `completion_percentage`: learned / total × 100, rounded to two decimal places; zero for an empty deck. Decode as `Double`. It measures learned cards, not generation readiness.
- `completed`: generation status is exactly `completed`, total > 0, and learned == total. Pending, generating, failed, unknown, and empty chapters are incomplete, even if their percentage is 100.

Adding an unlearned card makes a completed chapter incomplete. Deleting the last unlearned card can complete a nonempty chapter. Reset clears the canonical facts and changes epochs; derived aggregates then reflect the reset. There is no separate completion mutation. Study All reports contribute to each exact card's owning chapter automatically.

`services/chapters.py::ordered_chapters` supplies `(position, id)` ordering to plans, progress, and exams. `services/exam_groups.py::split_chapters` remains the single half splitter, also used by the pure timeline scheduler. First half gets the extra chapter: 12 → 6/6; 5 → 3/2; 1 → 1/0.

## Swift: current progress

`GET /decks/{deck_id}/study-progress`, authenticated, returns the object directly. Existing card facts, deck metadata, and progress epochs remain present. The aggregate fields and `summary` are additive. Example: a single-chapter parent whose only card has been learned:

```json
{
  "deck_id": "10000000-0000-4000-8000-000000000001",
  "decks": [
    {
      "deck_id": "20000000-0000-4000-8000-000000000001",
      "title": "Introduction",
      "parent_deck_id": "10000000-0000-4000-8000-000000000001",
      "position": 0,
      "generation_status": "completed",
      "progress_epoch": "30000000-0000-4000-8000-000000000001",
      "learned_card_count": 1,
      "total_card_count": 1,
      "completion_percentage": 100.0,
      "completed": true,
      "cards": [
        {
          "card_id": "40000000-0000-4000-8000-000000000001",
          "learned_at": "2026-09-24T02:00:00Z"
        }
      ]
    }
  ],
  "summary": {
    "total_deck_count": 1,
    "completed_deck_count": 1,
    "learned_card_count": 1,
    "total_card_count": 1
  }
}
```

Scope matches Phase 2A: a chapter returns itself; a root with children returns its owned direct children (excluding cards directly on the root); a standalone root returns itself. Summary counts cover exactly the returned `decks`, so `total_deck_count` / `completed_deck_count` are chapter counts for a parent and 1 / 0-or-1 for a standalone deck. Counts and positions are integers, completion is Boolean, UUIDs are UUID strings, `parent_deck_id` and `learned_at` may be null. All fields shown are present.

Schemas: `ChapterProgress`, `DeckLearningFacts`, `ProgressSummary`, `StudyProgressResponse` in `app/schemas/study_progress.py`.

There is **no new chapter-completion request or endpoint**. Continue `POST /study/progress/submissions` with Phase 2A's exact card IDs, `phase: "review"`, `answer: "got_it"`, captured epochs, session UUID, and completion timestamp. Continue the existing reset contract. Do not send client-derived `completed` or counts. See [Phase 2A request/error contracts](study-progress-phase-2a.md).

## Swift: exam status and gates

`GET /decks/{parent_deck_id}/exams` remains the authoritative status endpoint. Existing `status`, `passed`, `best_score`, `attempt_count`, and `completed_at` remain. New fields: `applicable`, `available`, `completed`, and ordered `chapter_ids`. `exam_id` is now nullable, and `status` gains `not_applicable`.

| Exam | Eligibility if not already passed |
| --- | --- |
| First half | Nonempty first group; every chapter completed |
| Second half | Nonempty second group; every chapter completed, independently of first-half pass |
| Final, two or more chapters | Second-half exam passed; first-half pass is not additionally required |
| Final, one chapter | First-half exam passed |
| Any exam with no applicable chapters | Unavailable |

For applicable exams, a historical pass overrides current learning prerequisites: `status: "completed"`, `completed: true`, `passed: true`, `available: true`. Retakes are allowed after added content or a reset. A failed retake does not erase the previous pass, best score, or first-pass timestamp. An attempted but never-passed exam is not completed.

For a one-chapter parent, no new second-half definition is created. The status list still contains a second-half entry with `applicable: false`, `available: false`, `exam_id: null`, and `status: "not_applicable"`. If a legacy definition exists, its UUID/history remains. If that legacy exam passed, its status stays `completed` and its `passed`/`completed` fields remain true, but it is still not applicable and unavailable for retakes. A legacy second-half pass does not substitute for first-half pass in the one-chapter final gate. An empty parent similarly creates no empty exam definitions.

Example for the learned, one-chapter parent above, before any exams passed:

```json
{
  "deck_id": "10000000-0000-4000-8000-000000000001",
  "exams": [
    {
      "exam_id": "50000000-0000-4000-8000-000000000001",
      "exam_type": "first_half",
      "status": "unlocked",
      "applicable": true,
      "available": true,
      "completed": false,
      "passed": false,
      "chapter_ids": ["20000000-0000-4000-8000-000000000001"],
      "best_score": null,
      "attempt_count": 0,
      "completed_at": null
    },
    {
      "exam_id": null,
      "exam_type": "second_half",
      "status": "not_applicable",
      "applicable": false,
      "available": false,
      "completed": false,
      "passed": false,
      "chapter_ids": [],
      "best_score": null,
      "attempt_count": 0,
      "completed_at": null
    },
    {
      "exam_id": "50000000-0000-4000-8000-000000000002",
      "exam_type": "final",
      "status": "locked",
      "applicable": true,
      "available": false,
      "completed": false,
      "passed": false,
      "chapter_ids": ["20000000-0000-4000-8000-000000000001"],
      "best_score": null,
      "attempt_count": 0,
      "completed_at": null
    }
  ]
}
```

Schemas: `ExamStatusResponse`, `ExamProgressionResponse` in `app/schemas/exam.py`. Status values are `locked`, `unlocked`, `completed`, `not_applicable`; types remain `first_half`, `second_half`, `final`. `completed_at` remains the existing nullable exam timestamp, not a date-only field.

Swift should use `available` to enable exam actions, `applicable` to show/hide the second-half action, and `passed` / `completed` for achievement. Consume `decks` / `chapter_ids` in their returned order; do not reorder ties by title or recompute prerequisites. Refresh both reads after learning submission, reset, card edits/deletions, generation completion, and exam submission. Schedule dates do not unlock exams.

All protected exam actions enforce the same eligibility:

- `GET /decks/{parent_deck_id}/exams/{exam_type}` (source cards)
- `POST /decks/{parent_deck_id}/exams/{exam_type}/generate`
- `GET /exams/{exam_id}` (persisted questions)
- `POST /exams/{exam_id}/submit`

Unavailable actions return HTTP 403 with `{"detail":{"code":"exam_locked","message":"This exam is locked."}}`. Nonapplicable actions return HTTP 403 with `{"detail":{"code":"exam_not_applicable","message":"This exam is not applicable."}}`. Missing/foreign decks or exam IDs remain HTTP 404. Authentication and answer-validation requirements are unchanged.

Exam answer requests, score calculation, passing thresholds, persisted questions, attempts, and historical achievements are unchanged. Submission `passed` describes that particular attempt. Submission `completed` retains its existing meaning: the overall final exam has passed. `next_exam_type` skips a nonapplicable second half (first → final for one chapter), and `next_exam_unlocked` reflects the same authoritative availability calculation, not simply whether the current attempt passed. Fetch the status endpoint for all three entries.

## Transactions and validation

Exam status evaluation shares Phase 2A's authenticated-user PostgreSQL row lock. Initial exam definitions/progression are flushed and committed by callers, not committed midway through eligibility checks. Submission holds the lock through recording the attempt and updating progression. Reset and concurrent attempts therefore serialize. Generation releases these locks during the external AI request, then rechecks eligibility before persisting questions; if another request already saved questions, it reuses them.

The aggregate calculation uses one grouped query for any number of chapters, plus existing batched card-fact reads where needed. Only the owner's facts count. No new AI call is required for progress or eligibility.

Validation: 103 tests passed, with no skips when `TEST_DATABASE_URL` points to the disposable PostgreSQL test server: 89 SQLite/pure tests and 14 PostgreSQL tests. Phase 2B adds 29 unit/API tests (including grouped cases) and 4 PostgreSQL tests. Coverage includes all 27 requested scenarios, generation states, endpoint enforcement, retakes, nonapplicable legacy data, query count, concurrent initialization/attempt numbering, and both reset/submission orderings. All database migrations were exercised in disposable schemas; the running application's database was not modified by Phase 2B.

```sh
.venv/bin/python -W ignore::DeprecationWarning -m unittest discover -s tests -v
TEST_DATABASE_URL='postgresql+psycopg://USER@HOST:PORT/TEST_DB' .venv/bin/python -W ignore::DeprecationWarning -m unittest discover -s tests -v
```

Use a dedicated test database/account that permits creating and dropping test schemas. PostgreSQL tests skip when `TEST_DATABASE_URL` is absent.
