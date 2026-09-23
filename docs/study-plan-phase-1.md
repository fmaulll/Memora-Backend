# Persistent Study Plans — Phase 1

This is a saved **planned schedule**. It does not record learning, reviews, session completion, mastery, health, or notifications. Exam dates are projections; the existing ExamService remains the runtime eligibility authority.

## Create and fetch

Both endpoints require the existing bearer access token and deck ownership:

- `POST /decks/{deck_id}/study-plan` → `201 StudyPlanResponse`
- `GET /decks/{deck_id}/study-plan` → `200 StudyPlanResponse`

They return the plan directly, with no `data` or `timeline` wrapper. GET never creates or recalculates a plan. Repeating POST returns `409 study_plan_exists`; it does not replace the current plan. Concurrent creation is serialized on the parent row, backed by a unique constraint.

Example create request:

```json
{
  "start_date": "2026-09-21",
  "requested_target_date": null,
  "timezone": "Asia/Jakarta",
  "study_weekdays": [0, 1, 2, 3, 4],
  "daily_card_limit": 20
}
```

- `timezone` is required and must be a valid IANA name.
- Omitted/null `start_date` means today's calendar date in that timezone.
- `requested_target_date` is optional and cannot precede the resolved start date. Explicit historical date ranges are permitted; they do not imply historical study completion.
- Weekdays are unique integers, Monday `0` to Sunday `6`; default all seven days. At least one is required.
- `daily_card_limit` defaults to 20, accepts 1–1000, and limits learning cards per study day. This is a workload preference, not a validated mastery estimate. Phase 1 does not accept an `intensity` string.
- Unknown fields, including client-written completion counts, are rejected.

Complete illustrative response for one chapter containing eight actual cards:

```json
{
  "id": "10000000-0000-4000-8000-000000000001",
  "parent_deck_id": "20000000-0000-4000-8000-000000000001",
  "start_date": "2026-09-21",
  "requested_target_date": null,
  "estimated_finish_date": "2026-09-23",
  "timezone": "Asia/Jakarta",
  "study_weekdays": [0, 1, 2, 3, 4],
  "daily_card_limit": 20,
  "required_daily_card_count": null,
  "target_achievable": null,
  "revision": 1,
  "algorithm_version": "chapter-plan-v1",
  "count_source": "actual",
  "created_at": "2026-09-21T02:00:00Z",
  "updated_at": "2026-09-21T02:00:00Z",
  "chapters": [
    {
      "id": "30000000-0000-4000-8000-000000000001",
      "title": "Foundations",
      "position": 0,
      "generation_status": "completed",
      "scheduled_card_count": 8
    }
  ],
  "items": [
    {
      "id": "40000000-0000-4000-8000-000000000001",
      "scheduled_date": "2026-09-21",
      "item_type": "learn",
      "chapter_id": "30000000-0000-4000-8000-000000000001",
      "target_card_count": 8,
      "position": 0
    },
    {
      "id": "40000000-0000-4000-8000-000000000002",
      "scheduled_date": "2026-09-22",
      "item_type": "first_half_exam",
      "chapter_id": null,
      "target_card_count": null,
      "position": 1
    },
    {
      "id": "40000000-0000-4000-8000-000000000003",
      "scheduled_date": "2026-09-23",
      "item_type": "final_exam",
      "chapter_id": null,
      "target_card_count": null,
      "position": 2
    }
  ]
}
```

Pydantic definitions: `app/schemas/study_plan.py`. UUIDs are UUID strings (or null for an exam's chapter reference). Calendar fields are `YYYY-MM-DD`; creation/update timestamps are timezone-aware instants. Items have zero-based positions and are returned in that order. There is no `daily_plan`, `focus`, completion count, or item status in this new contract. This is distinct from the legacy AI `timeline` summary.

## Scheduling rules

The pure `generate_schedule` function accepts ordered chapter/count inputs and explicit local dates. It has no database, clock, AI, or HTTP calls.

1. Only direct children owned by the parent deck's user participate, ordered by `(position, id)`.
2. Completed generation uses actual persisted card counts, ignoring the old planned count. Pending/generating/failed chapters use a nonnegative planned `card_count`. Missing planned counts or unknown generation states return a conflict instead of pretending the chapter is empty.
3. Chapters are filled sequentially. Adjacent chapters in the same half may share a date when daily capacity remains. Their item positions preserve the study order.
4. The shared exam grouping function gives the first half the extra chapter when the count is odd.
5. Each nonempty half gets an exam on the next eligible study date. Final follows the last applicable half-exam on another eligible date. All milestones occupy separate study days; no exam duration or card count is invented.
6. With one chapter, the projection is learning → first half → final. There is no second-half event. Truly empty chapters have no learning items. An entirely empty half has no exam projection; an entirely empty deck-with-chapters has no items and its estimated finish equals its start. A parent with no chapters is rejected.
7. Without a deadline, use the daily card limit. With a deadline, find the minimum integer daily workload that fits the learning and milestone days. Use that workload if it is below the limit; otherwise retain the limit and report the later finish. Do not silently move the requested target.
8. `required_daily_card_count` is null without a deadline. It is also null when even unlimited learning capacity cannot fit the separate milestone days. In that case `target_achievable` is false. Empty workloads have a required count of zero when a target is supplied.
9. `target_achievable` compares the estimated finish with the requested target, or is null without a target. It is scheduling feasibility, not plan health or a promise of mastery.
10. The scheduler rejects schedules longer than 3,660 calendar days instead of generating unbounded output. Non-study dates have no items; the client may render gaps/rest days without assigning backend work to them.

**Runtime exams are intentionally unchanged.** Currently the first exam is unlocked immediately, the second requires passing the first, and final requires passing both. In particular, the existing one-chapter final eligibility limitation still exists even though its final projection is displayed. Swift must use `GET /decks/{id}/exams` for actual availability; never unlock from a projected item/date. Chapter-learning gates await the Phase 2 Swift progress review.

## Optional AI generation integration

Add a `study_plan` object with the create-request fields to the existing `POST /ai/decks/generate` request:

```json
{
  "plan": {
    "title": "Biology",
    "subject": "Biology",
    "education_level": "High school",
    "learning_language": "English",
    "chapters": [
      {"title": "Cells", "description": "Cell basics", "key_concepts": ["Structure"], "card_count": 8}
    ]
  },
  "study_purpose": "Learn from Scratch",
  "study_plan": {
    "timezone": "Asia/Jakarta",
    "study_weekdays": [0, 1, 2, 3, 4],
    "daily_card_limit": 20
  }
}
```

The existing `deck` and `timeline` response fields remain. A new `study_plan` field contains the persisted response above, initially `count_source: "planned"`. The legacy `timeline` is an aggregate of these same items, so it does not run a second scheduler. All decks, plan/items, and generation job commit together.

Omitting `study_plan` preserves generation without saving a plan (`study_plan: null`). Its transient timeline uses UTC, all weekdays, and the default workload; it now also supports no target date. Supply `study_plan.timezone` for an explicit local calendar schedule. Legacy `timeline` still uses `total_days`, `total_cards`, and `daily_plan` with string `focus`; its estimates now follow chapter order and reserve milestone days rather than dividing cards evenly over every calendar day. No review scheduling is implied.

If the legacy top-level `target_date` is supplied, it fills an omitted nested target. Conflicting targets return 422. `/ai/decks/plan` and the AI prompt are unchanged; existing chapter counts already supply the necessary planning information. No extra AI calls are introduced.

After every chapter completes, the existing generation worker performs one planned→actual correction, using the original dates and actual counts. It sets `count_source: "actual"` and increments revision to 2. If the schedule is unchanged, item IDs remain stable; otherwise all items are atomically replaced. Subsequent worker retries do not rebuild it again. Failed/incomplete generation retains the planned forecast. Retry responses retain their previous behavior; fetch the saved plan separately.

This correction may revise past **projections**, never actual history (none exists in Phase 1). Manual card edits after creation do not continuously rebuild the schedule. `scheduled_card_count` and `count_source` describe the saved forecast, not a live progress measurement.

## Persistence and structure edits

`study_plans` stores preferences and the current projection metadata; `study_plan_items` stores relational chapter/date/count targets. Weekday preferences use seven integer bits internally (Monday=bit 0), exposed as an ordinary list in the API. No scheduling state is stored as JSON. The plan revision applies to all its items, so item revisions and speculative completion/status fields are omitted.

One plan per parent is enforced by a unique constraint. Parent/user foreign keys cascade plan deletion, and plan deletion cascades items. Learning item chapter references restrict direct chapter deletion. Database checks enforce item shape and positive learning counts.

Until schedule editing exists, chapter additions, deletions, reparenting, and reordering on planned decks return `409 study_plan_structure_locked`. Renaming is allowed. Deleting a parent through the existing deck endpoint deletes its plan/items alongside the existing parent-deletion behavior. No standalone plan deletion/replacement endpoint is introduced.

## Errors

Domain errors have `{"detail":{"code":"study_plan_exists","message":"This deck already has a study plan"}}`. Pydantic request validation retains FastAPI's standard 422 `detail` array.

| HTTP | Code | Meaning |
|---|---|---|
| 404 | `deck_not_found` | Missing deck or another user's deck |
| 404 | `study_plan_not_found` | Owned parent has no saved plan |
| 400 | `root_deck_required` | A child chapter was used as the plan's parent |
| 409 | `study_plan_exists` | Repeated create; fetch the existing plan |
| 409 | `chapters_required` | Parent has no direct chapters |
| 409 | `chapter_count_unknown` | Unfinished chapter lacks a valid planned count |
| 409 | `chapter_status_unknown` | Unrecognized generation status |
| 409 | `study_plan_structure_locked` | Chapter structure change would invalidate the saved plan |
| 422 | `invalid_study_schedule` | Invalid resolved dates or planning horizon exceeded |

Authentication errors follow the existing authentication dependency.

## Implementation map and rollout

- `models/study_plan.py`: two models and constraints; `models/deck.py` adds the owned plan relationship.
- `schemas/study_plan.py`: explicit request/response contract.
- `services/study_timeline.py`: pure scheduling and the legacy response adapter.
- `services/exam_groups.py`: the one shared chapter split, reused by the existing exam service without altering eligibility.
- `services/study_plan.py`: ownership, input loading, persistence, serialization, and one-time generation correction; callers own transactions.
- `routers/study_plan.py`: only create/fetch endpoints, registered in `main.py`.
- AI schema/router/worker: optional creation and explicit completion hook.
- Existing deck router: protects a saved schedule from structural edits.

Migration `b82e5c9d1a40` follows the verified sole previous head `afc18ff984f6`. Existing migrations are unchanged. It adds only the two study tables and their indexes/constraints. Downgrade removes those tables, including their saved schedules, while retaining existing user/deck/card data.

Deploy the migration to the API's configured database before starting the updated API and generation worker:

```sh
.venv/bin/alembic upgrade head
```

No development data reset is required. This implementation was tested against an isolated PostgreSQL database; the development database was not migrated as part of this change.

Run the tests with the existing unittest runner:

```sh
.venv/bin/python -m unittest discover -s tests -v
```

Set `TEST_DATABASE_URL` to a disposable PostgreSQL database to additionally run migration upgrade/downgrade, constraint/cascade, and concurrent-create tests. Those tests use fresh random schemas and remove only their own schemas. SQLite/API tests mock AI generation and require no AI network calls.

Swift can display this plan in the preview/details flow when available, but should present it as a planned path. It must not infer completed chapters, mastered cards, exam unlocks, or actionable notifications from these projections. Mapping Again/Got It, queues, batches, Study All, and resume behavior remains Phase 2 work after inspecting Swift.
