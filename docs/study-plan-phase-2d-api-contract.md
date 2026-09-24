# Phase 2D exact API contract for Swift S1

This handoff describes the current implementation, without changing any API behavior.

- [Exact OpenAPI 3.1 export](examples/phase-2d/openapi.json): ten relevant paths and all 29 referenced component schemas, exported unchanged from `app.main.app.openapi()`. Includes persistent plan creation as well as the requested read/recalculate endpoints.
- [Captured exchange index](examples/phase-2d/index.json): 25 actual FastAPI TestClient exchanges, with separate raw request and response JSON files. The index is documentation metadata, **not an API response wrapper**.
- Captures use disposable SQLite fixtures, authenticated test users, and a scheduler clock fixed at September 22, 2026. Receipt/creation timestamps use the actual capture clock. They are not responses from the running API database. Exam questions were seeded; the generation example returns already-persisted questions without an AI request.

All endpoints below require `Authorization: Bearer <access_token>`. Send request JSON with `Content-Type: application/json`. Listed successful operations return HTTP 200 except plan creation, which returns 201. Success bodies are the named response object directly, without a `data` or `timeline` wrapper.

## Endpoint/schema mapping and raw examples

Schema names refer to `components.schemas` in the exported OpenAPI file.

| Endpoint | Request schema | Response schema | Exact captured JSON |
| --- | --- | --- | --- |
| `GET /decks/{deck_id}/study-progress` | No body | `StudyProgressResponse` | [Partial progress](examples/phase-2d/progress.response.json), [completed chapter](examples/phase-2d/progress-chapter-completed.response.json), [after reset](examples/phase-2d/progress-after-reset.response.json) |
| `POST /study/progress/submissions` | `StudyProgressSubmission` | `StudyProgressSubmissionResponse` | [Request](examples/phase-2d/submission.request.json), [response](examples/phase-2d/submission.response.json), [same receipt replay](examples/phase-2d/submission-replay.response.json), [already-learned-only result](examples/phase-2d/submission-already-learned.response.json) |
| `POST /decks/{deck_id}/study-progress/reset` | `ProgressResetRequest` | `ProgressResetResponse` | [Request](examples/phase-2d/reset.request.json), [response](examples/phase-2d/reset.response.json), [same receipt replay](examples/phase-2d/reset-replay.response.json) |
| `GET /decks/{deck_id}/study-plan` | No body | `StudyPlanResponse` | [Response](examples/phase-2d/plan.response.json) |
| `POST /decks/{deck_id}/study-plan/recalculate` | No body | `StudyPlanResponse` | [Response](examples/phase-2d/recalculate.response.json) |
| `GET /decks/{parent_deck_id}/exams` | No body | `ExamProgressionResponse` | [Locked](examples/phase-2d/exams-locked.response.json), [unlocked](examples/phase-2d/exams-unlocked.response.json), [passed](examples/phase-2d/exams-passed.response.json), [one chapter / second half not applicable](examples/phase-2d/exams-one-chapter.response.json) |
| `GET /decks/{parent_deck_id}/exams/{exam_type}` | No body | `ExamResponse` | [Source cards](examples/phase-2d/exam-cards.response.json) |
| `POST /decks/{parent_deck_id}/exams/{exam_type}/generate` | No body | `ExamQuestionsResponse` | [Existing generated questions](examples/phase-2d/exam-generate-existing.response.json) |
| `GET /exams/{exam_id}` | No body | `ExamQuestionsResponse` | [Questions](examples/phase-2d/exam-questions.response.json) |
| `POST /exams/{exam_id}/submit` | `ExamSubmissionRequest` | `ExamSubmissionResponse` | [Request](examples/phase-2d/exam-submission.request.json), [response](examples/phase-2d/exam-submission.response.json) |

The two exam GET routes serve different purposes: the deck/type route returns source flashcards; the exam-ID route returns persisted exam questions. Neither public question response exposes the correct answer or explanation.

## Required fields, types, and nullability

OpenAPI 3.1 expresses nullable values using `anyOf` with a `{"type":"null"}` branch, not `nullable: true`. A field can be both required and nullable. **All fields in the success response schemas listed above are required**, including nullable fields. Arrays are present, possibly empty; they are not null. UUIDs are strings in UUID format, never integer identifiers or empty strings.

Swift should decode JSON `integer` as `Int`, `number` as `Double`, `boolean` as `Bool`, UUID strings as `UUID`, and nullable values as optional values. Do not infer a number's type from a particular example that happens to contain a whole number.

| Response location | Nullable fields | All other fields |
| --- | --- | --- |
| `StudyProgressResponse` root, `summary` | None | Required, non-null |
| `StudyProgressResponse.decks[]` | `parent_deck_id: UUID?` | Required, non-null |
| `StudyProgressResponse.decks[].cards[]` | `learned_at: date-time?` | `card_id` is required UUID |
| Submission response, nested `decks[]` | None | Required, non-null |
| Reset response, nested `decks[]` | None | Required, non-null |
| Plan root | `requested_target_date: date?`, `required_daily_card_count: Int?`, `target_achievable: Bool?` | Required, non-null, including plan/root UUIDs and `estimated_finish_date` |
| Plan `chapters[]` | None | Required, non-null |
| Plan `items[]` | `chapter_id: UUID?`, `target_card_count: Int?`, `actual_learned_count: Int?`, `shortfall_count: Int?`, `closed_at: date-time?`, `achieved_at: date-time?` | Required, non-null |
| Exam progression `exams[]` | `exam_id: UUID?`, `best_score: Int?`, `completed_at: date-time?` | Required, non-null |
| Exam submission response | `next_exam_type: ExamType?` | Required, non-null; `score` and `passing_score` are Double |
| Exam questions and question entries | None | Required, non-null; `options` is `[String]` |
| Exam source `cards[]` (`CardResponse`) | `front_image_url: String?`, `back_image_url: String?` | Required, non-null |

### Timestamp distinction

- Calendar fields `start_date`, `requested_target_date`, `estimated_finish_date`, and `items[].scheduled_date` use `YYYY-MM-DD`. Treat these as local calendar dates in the plan timezone, not UTC instants.
- Submission `completed_at` **must include a timezone**, for example `2026-09-21T18:00:00+07:00` or `2026-09-21T11:00:00Z`. It is normalized to UTC. Future times beyond five minutes are rejected.
- Progress fact timestamps, submission/reset response timestamps, and plan timestamps are emitted with UTC timezone information. Accept optional fractional seconds.
- **Existing exam/card datetime fields are not normalized by their Pydantic response models.** The captured exam `completed_at` is a naive UTC string such as `2026-09-24T17:15:54.792701`, without `Z`; PostgreSQL-backed reads can include an offset. A decoder assuming every backend timestamp always ends in `Z` will fail on the current contract. For these legacy exam/card fields, accept both offset-bearing timestamps and the existing naive UTC form. The exact schema is `format: date-time`; the naive example is an existing implementation inconsistency, not a new Phase 2D format guarantee. No timestamp normalization change is included in this documentation task.

## Progress read and chapter completion

The root is `{ "deck_id": UUID, "decks": [...], "summary": {...} }`. Counts are **flat inside each `decks[]` object**, alongside `progress_epoch`; there is no nested `progress` object.

Each deck has `learned_card_count: Int`, `total_card_count: Int`, `completion_percentage: Double` (0–100, not 0–1), and `completed: Bool`. Completion is derived: generation must be `completed`, total cards must be greater than zero, and every current card must have a learned fact. Empty chapters are not complete. There is no client-writable chapter-completed flag.

A parent with children returns the ordered immediate chapters; its direct root cards are excluded. A child request returns that child. A standalone root without children returns itself. `summary` contains `total_deck_count`, `completed_deck_count`, `learned_card_count`, and `total_card_count`, all Int.

## Submission request and receipt

Required nesting:

```text
session_id: UUID
completed_at: timezone-aware date-time
decks: [
  {
    deck_id: UUID,
    progress_epoch: UUID,
    learned_cards: [{card_id: UUID, phase: "review", answer: "got_it"}]
  }
]
```

`progress_epoch` is an opaque, non-null **UUID per deck**, not a numeric revision or timestamp. Read it from `decks[].progress_epoch` and retain it with the offline operation. `session_id` is the client-generated idempotency UUID for this immutable submission. A Study All submission can contain several deck groups under one session ID.

The response root contains `session_id`, `completed_at`, `submitted_at`, and `decks`. Each result deck contains exactly `deck_id`, `progress_epoch`, `accepted_card_ids: [UUID]`, and `already_learned_card_ids: [UUID]`. It contains no timeline, chapter counts, or plan revisions.

Use 1–100 distinct deck groups and 1–5000 learned transitions per group, with at most 5000 unique cards overall. Duplicate cards within one group are normalized; duplicate deck groups are rejected. Unknown fields are rejected at all request levels. Only the literal transition `phase: "review"`, `answer: "got_it"` is accepted. Do not send `new`, `again`, a percentage, a completed Boolean, or an inferred count.

Retry with the same operation UUID and same content. Submission and reset UUIDs share the user's receipt namespace; generate a distinct UUID for every new operation across both kinds. Reusing an operation UUID with different content yields `idempotency_conflict`.

A new accepted fact triggers automatic plan adaptation; replay and already-learned-only submissions do not. A replay can contain an old epoch after a later reset: it is the original acknowledgement, **not a current progress snapshot**. Fetch current progress instead of using a receipt to overwrite a newer local epoch.

## Reset request and receipt

Required nesting:

```text
reset_id: UUID
expected_decks: [{deck_id: UUID, progress_epoch: UUID}]
```

Read the current scope with `GET /decks/{deck_id}/study-progress`, then include every returned deck's ID and epoch in `expected_decks`. A child reset expects only that child; a parent reset expects all immediate children; a standalone reset expects itself. The list must be nonempty with unique deck IDs. Extra request fields are rejected.

The response root contains `reset_id`, `deck_id` (the requested path ID), `reset_at`, and `decks`. Every result entry contains `deck_id`, `previous_epoch`, `progress_epoch` (**the new UUID**), and `cleared_card_count: Int`. Even a zero-cleared deck rotates epoch. There is no root-level epoch.

Reset clears current learned facts and adapts existing plans; it preserves exam achievements and historical targets. Replaying the same reset returns the original response without another rotation or recalculation. On `stale_progress_epoch`, refetch progress and discard/reconcile obsolete local work; never relabel pre-reset offline learning with the new epoch.

## Persistent plan read/recalculate

Both operations return the same `StudyPlanResponse`. The persistent plan root has `chapters` and `items`; **there is no `daily_plan`, `focus`, `intensity`, `total_days`, or `total_cards` field**. The old preview object is a different contract and must not be reused for the persistent plan decoder.

`items[].item_type` is exactly `learn`, `first_half_exam`, `second_half_exam`, or `final_exam`. There are no rest-day items: gaps between scheduled dates represent days without a scheduled item. `status` is `upcoming`, `active`, `completed`, `missed`, or `partial`; `period` is `historical`, `current`, or `future`. These describe timeline performance, not exam permission.

For a learning item, chapter/count fields are populated and `achieved_at` is null. For an exam item, `chapter_id`, `target_card_count`, `actual_learned_count`, and `shortfall_count` are null. `closed_at` and `achieved_at` are nullable history timestamps.

`revision` is an Int schedule revision, separate from UUID progress epochs. `count_source` is `planned` or `actual`; `algorithm_version` is an opaque String. `generation_status` on chapter/progress records is a String in the schema, not a declared enum.

`study_weekdays` uses Monday=0 through Sunday=6. `remaining_card_count` is canonical current outstanding work. Chapter `scheduled_card_count` includes preserved historical targets and is not a replacement for remaining work. `target_achievable` is null without a requested date. `required_daily_card_count` is null without a deadline or when milestone days cannot fit; zero is a meaningful non-null value.

Recalculate takes **no body** and uses saved preferences. GET is read-only. For calendar rollover or explicit recovery, POST recalculate. Successful progress/reset/exam hooks do not embed a refreshed plan; GET the plan when the view needs it. `projection_blocked: true` is a successful plan result, not an error.

## Exam semantics for S1

`GET /decks/{parent_deck_id}/exams` remains authoritative for eligibility and has root `{ "deck_id": UUID, "exams": [...] }`. It returns `status` and `passed` as well as `applicable`, `available`, and `completed`.

- `exam_type`: `first_half`, `second_half`, or `final`.
- `status`: `locked`, `unlocked`, `completed`, or `not_applicable`.
- Use `available` to enable taking an exam. A passed exam can remain available for retakes; it has status `completed`, not `unlocked`.
- On the status entry, `completed` and `passed` both represent the persisted milestone pass. `completed_at` is the first-pass timestamp, nullable for never-passed or legacy pass records.
- `best_score` is currently `Int?`; submission `score` and `passing_score` are Double percentages. `attempt_count` and `attempt_number` are Int.
- For an odd number of chapters, the **first half gets the extra chapter**. With one chapter, second half is not applicable, `chapter_ids` is empty, and `exam_id` can be null. Final requires the last applicable half's pass.
- First/second half eligibility uses completed chapters in its own half. Second half does not additionally require first-half pass in the current implementation. Follow server `available` rather than imposing another client sequence.

Exam answers use `{ "answers": [{ "question_id": UUID, "answer": String }] }`. Send the actual selected option text, not an option index or letter unless that is the stored option text. The server trims and case-folds for comparison. Every persisted question must be answered once; empty, duplicate, foreign, or incomplete answers are rejected. `question_type` is a String in the schema; generation currently supports `multiple_choice` and `true_false` (options `True`/`False`).

In `ExamSubmissionResponse`, `passed` describes this attempt; `completed` describes whether final has been passed for the parent. `next_exam_type` can be null; `next_exam_unlocked` comes from current server eligibility. **Exam submission has no idempotency UUID or receipt contract**: posting it again records another attempt. Phase 2D skips adaptation on a passed retake but does not deduplicate the exam attempt itself.

## Errors: three JSON shapes, plus unexpected server failures

The current OpenAPI export declares FastAPI `HTTPValidationError` responses but does **not** declare the custom business error responses. Do not infer a single error shape from generated OpenAPI alone. No uniform error schema or exception handler exists in this phase.

Business error example, HTTP 409 ([captured response](examples/phase-2d/error-stale-epoch.response.json)):

```json
{
  "detail": {
    "code": "stale_progress_epoch",
    "message": "Progress was reset; old offline learning must not be relabeled with a new epoch"
  }
}
```

Validation example, HTTP 422 ([captured response](examples/phase-2d/error-validation.response.json)):

```json
{
  "detail": [
    {
      "type": "timezone_aware",
      "loc": ["body", "completed_at"],
      "msg": "Input should have timezone info",
      "input": "2026-09-21T18:00:00"
    }
  ]
}
```

Validation `loc` entries can be strings or integers (array indices). Runtime entries can additionally include `input`, `ctx`, or other validation metadata beyond the minimal OpenAPI `ValidationError` properties. Decode the common `type`, `loc`, and `msg`, allowing extra keys. Validation issues have `type`, **not a business `code`**.

Legacy/authentication example, HTTP 401 ([captured response](examples/phase-2d/error-authentication.response.json)):

```json
{"detail": "Invalid authentication token"}
```

Exam HTTP 404 example: `{"detail":"Exam not found"}`. Existing exam 400 errors also use string `detail`, including `Answers cannot be empty.`, `This exam has no persisted questions.`, `Duplicate question IDs are not allowed.`, `One or more question IDs do not belong to this exam.`, and `Every exam question must be answered.` Generation can return HTTP 502 with string detail. There is no machine-readable `code` on these legacy errors.

Unexpected exceptions can produce HTTP 500 with a non-JSON body such as `Internal Server Error`. Check HTTP status before decoding success, and allow a text fallback if error JSON decoding fails. A 500 is not a stale-epoch or authentication response; retain pending progress/reset operations for a retry with their original UUID and payload.

### Current structured business codes

`detail.code` and `detail.message` are required non-null strings for the structured errors below. Codes are listed from current service call sites, not synthesized OpenAPI declarations.

| HTTP | Code | Where / handling |
| --- | --- | --- |
| 404 | `deck_not_found` | Progress or plan operations; missing/foreign resource |
| 404 | `user_not_found` | Shared mutation lock cannot find the user |
| 409 | `idempotency_conflict` | Existing submission/reset UUID used with different content; do not silently create another operation to bypass it |
| 409 | `progress_scope_changed` | Reset deck set no longer matches current scope; refetch progress |
| 409 | `stale_progress_epoch` | Submission/reset epoch mismatch; refetch, do not relabel old learning |
| 422 | `card_not_in_deck` | Submitted card missing/deleted or in another deck |
| 422 | `invalid_completion_time` | Submission more than five minutes in the future |
| 400 | `root_deck_required` | Plan endpoint was called for a child |
| 404 | `study_plan_not_found` | Owned parent has no existing plan |
| 409 | `study_plan_content_not_ready` | Explicit recalculate requires completed chapter generation; automatic hooks defer only this condition |
| 422 | `invalid_study_schedule` | Plan scheduling constraints cannot be processed; can propagate through a synchronous mutation hook, rolling back that mutation |
| 403 | `exam_locked` | Exam question/source/generation/submission access unavailable |
| 403 | `exam_not_applicable` | Requested milestone does not apply, such as second half with one chapter |

Plan creation additionally has 409 codes `study_plan_exists`, `chapters_required`, `chapter_count_unknown`, and `chapter_status_unknown`. Related chapter-structure mutations can return 409 `study_plan_structure_locked`. Exam missing-deck errors use legacy string detail even though plan/progress missing-deck errors use a structured object.

Exact code sources: `app/services/study_progress.py`, `app/services/study_plan.py`, `app/services/exam.py`, `app/services/exam_submission.py`, and `app/core/auth.py`. Exact request/response model sources: `app/schemas/study_progress.py`, `app/schemas/study_plan.py`, and `app/schemas/exam.py`.
