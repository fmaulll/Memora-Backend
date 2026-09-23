# Adaptive Study Timeline — Phase 2C

Phase 2C implements explicit, deterministic adaptation without AI. Current remaining work uses Phase 2B's canonical aggregate over current cards and the owner's non-null `card_progress.learned_at`. Historical performance uses preserved targets plus evidence of accepted learning. No Swift hooks, notification work, automatic progress/content hooks, or health labels are implemented.

## Migration and deployment

New head: `d04a7e3f9b62`, following Phase 2A/2B head `c93f6d2e8b51`.

Before running this application version against an existing database, apply:

```sh
.venv/bin/alembic upgrade head
```

Run with the same database configuration/schema as the API. The implementation was verified using disposable PostgreSQL schemas; the running application's database was not migrated or restarted during this change. Existing users, cards, progress, attempts, plan IDs, item IDs, targets, and dates are preserved by the migration. No historical counts or exam achievements are invented during migration.

Four nullable item columns are added:

| Field | Purpose |
| --- | --- |
| `learning_epoch` | Learning cycle expected by a learning target; stays unchanged when the chapter resets. Null on exam items. |
| `actual_learned_count` | Capped historical credit for a closed learning target; null while open and on exams. |
| `closed_at` | Time an item was snapshotted by explicit recalculation. |
| `achieved_at` | Original exam first-pass timestamp, copied from existing exam progression. |

There is no persisted item status, shortfall, chapter completion, duplicate card-progress table, or mutable competing learned counter. Historical snapshots can gain credit from late accepted uploads but cannot lose it due to reset or deletion. Their original target/date/learning cycle stays fixed.

Existing learning items are backfilled with their chapter's current progress epoch. **Pre-migration resets cannot be reliably assigned to an older intended target cycle**, because Phase 1 never stored that cycle. The migration does not guess it. From this migration onward, new items capture the correct epoch and old items retain it.

Downgrading removes these four history fields, including any captured snapshots. It preserves the remaining plan/item records and learning/exam data. Upgrade/downgrade tests run only in disposable schemas.

## Exact attribution

The small, pure attribution component lives in `services/study_attribution.py`:

1. Read immutable Phase 2A **submission receipts** for the authenticated owner. Only `accepted_card_ids` represent new learned facts; `already_learned_card_ids` and duplicate receipts add no learning.
2. Each historical event carries exact card ID, owning chapter ID, epoch, and the accepted `completed_at`. These receipts preserve evidence after current progress is cleared or its card is deleted. They are historical evidence only; current remaining workload still comes from current card progress.
3. Convert the completion timestamp to the plan's IANA timezone and take its local date. Upload/receipt creation time never determines the study day. The shared conversion is `services/study_calendar.py`.
4. Match targets by **local date + chapter + learning epoch**. Sort targets by date/position/ID, and facts by completion timestamp/card ID/epoch. Credit each distinct `(epoch, card_id)` at most once, up to each matching target's count.
5. Excess same-day learning does not become fabricated learning on a later date. It reduces current outstanding chapter work. Learning on a day without a matching target also reduces outstanding work without creating retroactive historical targets.
6. When recalculation closes a past item, snapshot its attributed count. Later recalculation may increase that count when newly accepted offline evidence matches its original date/cycle. It never lowers the snapshot and never changes the historical target to make the result look better.

For Monday target 10 and 4 learned Monday, Monday remains `4 / 10`, shortfall 6. If the upload arrives Wednesday, the accepted completion time still credits Monday; Wednesday gets no false learning credit. If 15 unique cards were learned Monday, Monday's target credit is `10 / 10`; all 15 reduce outstanding work. The extra 5 are not credited again Tuesday.

A reset starts a new learning cycle. The same card can legitimately satisfy a new initial-learning target after being learned again in that new epoch. Stale pre-reset submissions remain rejected by Phase 2A. An accepted acknowledgement replay does not restore current facts or count twice.

Retention requirement: the existing immutable progress receipts must remain available for historical attribution, including late uploads after content changes. This phase adds no receipt deletion policy.

## Calendar and item lifecycle

All scheduled dates are local calendar dates. For example, `2026-09-25T00:30:00+07:00` belongs to September 25 in Asia/Jakarta, even though its UTC date is September 24. DST conversion also uses the IANA zone.

On explicit recalculation, learning/exam projections dated **before today** are closed. Today remains revisable; the backend does not close an unfinished calendar day just because recalculation was requested. Exam achievements may be snapshotted immediately.

GET never writes snapshots. An elapsed item awaiting recalculation is displayed as historical using receipt evidence; `closed_at: null` distinguishes it from a persisted snapshot. Closed results stay at their stored values until an explicit recalculation accepts additional late evidence.

Read-only item fields:

| Field | Meaning |
| --- | --- |
| `actual_learned_count` | Integer for learning items; null for exams. Closed items use snapshots; open current/future items use current canonical facts in the target's epoch. |
| `shortfall_count` | `max(target - actual, 0)` for learning; null for exams. On current/future items this means outstanding target work, not a missed deadline. |
| `status` | `upcoming`, `active`, `completed`, `partial`, or `missed`. This is timeline performance, never exam eligibility. |
| `period` | `historical`, `current`, or `future`, based on local date and closure. |
| `closed_at` | Nullable UTC snapshot timestamp. |
| `achieved_at` | Nullable UTC exam first-pass timestamp. |

An unfulfilled historical learning target is `partial` with some credit, otherwise `missed`; a fully credited one is `completed`. An open, uncredited target is `active` today or `upcoming` in the future. A past unpassed exam projection is `missed`; a recorded achievement is `completed`. No status field is writable by the client.

## Adaptive scheduling

`services/adaptive_study_timeline.py` is pure Python: no SQLAlchemy, FastAPI, AI, or database reads. Persistence supplies ordered chapter remaining counts, local start date, preferences, today's current learned counts, and existing exam pass facts.

- Preserve canonical `(position, id)` chapter order and the shared ceiling-half splitter.
- Start at the later of today and the original plan start. Past targets remain separate historical rows.
- Allocate current unlearned cards sequentially across eligible weekdays. Earlier outstanding chapters take priority over later outstanding chapters.
- On an eligible current day, work already done consumes today's preferred capacity. Today may exceed the preferred limit through actual overachievement, but newly assigned work does not. Current-day targets may be rebuilt; closed targets never are.
- Off-day learning reduces remaining counts without inventing a scheduled learning target on an excluded weekday.
- Reuse Phase 1's `daily_card_limit`. A feasible requested deadline may spread future work more gently; an infeasible deadline does not silently raise this preferred cap.
- Binary search the same deterministic projection to calculate the minimum future daily workload needed to meet the unchanged requested date. The required value may exceed the preferred cap. If milestone days cannot fit, it is null; zero means no future card workload is needed. No requested deadline also yields null.
- Recompute estimated finish from preserved history and the newly projected schedule. The requested target date is never rewritten, including when already expired.

`remaining_card_count` is the current canonical total minus learned total, not the sum of historical shortfalls. This prevents rescheduling deficits that were already made up elsewhere or eliminated by deletion.

Generation must be `completed` for every chapter before explicit adaptive recalculation; otherwise return HTTP 409 `study_plan_content_not_ready`. Planned generation estimates cannot reliably represent actual remaining work. Completed-generation empty chapters can be processed, but block their unpassed half-exam forecast: `projection_blocked: true` and `target_achievable: false` when a deadline exists. Dates/workload in that case are conditional projections; adding valid content or resolving generation is necessary. An already-passed half does not become blocked merely because its cards were later deleted.

## Exam history versus projections

The existing ExamService exclusively owns runtime availability. Calendar dates and timeline states never unlock exams or mutate attempts, scores, or pass flags.

Unpassed milestones are projected after the remaining work in their canonical half, on dedicated eligible days; final follows the applicable half milestones. With one chapter there is no projected second-half exam. Already-passed milestones are omitted from mandatory current/future projections even when new cards require additional learning.

For a pass with a known timestamp:

- If its scheduled item matches the actual local pass date, preserve that item and attach the achievement.
- If an early pass fulfilled a projection that is already past, preserve that past scheduled date and ID, mark it completed, and expose the actual earlier `achieved_at`. Do not falsely label that fulfilled projection missed.
- Otherwise, retain genuinely missed earlier projections and add a historical achievement on the actual local pass date. Remove the outstanding current/future projection.
- Once an achievement is stored, reset/content changes do not move it or create a second mandatory exam.

A legacy pass with no timestamp suppresses the outstanding milestone but does not receive an invented achievement date. Its historical pass remains visible through the existing exam-status endpoint. A retained old missed projection describes that old schedule target, not the user's current exam entitlement.

## Explicit API

`POST /decks/{deck_id}/study-plan/recalculate`

- Authentication required; `deck_id` must be the owned parent/root with an existing plan.
- No request body or new preferences. Uses the plan's saved preferences and server-derived current local date.
- HTTP 200 returns the updated plan directly, in the same shape as GET.
- HTTP 400 `root_deck_required` for a child; HTTP 404 `deck_not_found` or `study_plan_not_found` for missing/foreign resources; HTTP 409 `study_plan_content_not_ready` for unfinished generation; HTTP 422 `invalid_study_schedule` for unsupported horizon/range constraints. Existing authentication errors remain unchanged.
- Application errors use `{"detail":{"code":"...","message":"..."}}`.

Existing `GET /decks/{deck_id}/study-plan` remains read-only and returns the additive fields described above. Plan-level additions are `remaining_card_count` (integer) and `projection_blocked` (Boolean). Existing `requested_target_date`, `estimated_finish_date`, `revision`, `daily_card_limit`, `required_daily_card_count`, and `target_achievable` remain.

Dates are `YYYY-MM-DD`; nullable dates/timestamps/UUIDs use JSON null. Snapshot/achievement timestamps are UTC ISO strings. `learning_epoch` stays an internal attribution field; Phase 2A's progress endpoint continues to expose the epochs needed for submissions.

[Complete example response](examples/study-plan-recalculate.json) was captured from a successful API test using fixture data: Monday retained `4 / 10`, Tuesday receives the remaining first-chapter work, and subsequent milestones move forward. It is not a response from the running production API.

Current counts and open-item progress can change on GET without a revision change. Estimated dates and required workload are the last persisted schedule calculation until the explicit POST runs; no hidden recalculation occurs on GET. Chapter `scheduled_card_count` retains its meaning as the sum of that chapter's targets in the returned timeline, now including preserved history, so it is not a current outstanding-work count.

## Revisions and transactions

Creation, learning, resets, exam operations, and recalculation use compatible user-first lock ordering. Recalculation additionally locks the owned parent, plan, and chapters, then closes history and replaces current/future rows atomically. PostgreSQL's item-position uniqueness remains enforced; temporary positions avoid collisions during replacement.

Unchanged rows retain their IDs. Revision advances only when persisted item content/history or schedule metrics change. Merely running a newer algorithm, repeating an identical request, or displaying more live progress does not force a revision bump. Algorithm provenance changes when a schedule change is actually persisted. Plan revision remains independent from progress epochs.

If generation finishes before history exists, the existing Phase 1 initial forecast correction still works. If that callback would overwrite past/closed targets, it now defers to explicit adaptive recalculation. No new automatic hooks were added.

Reset-before-recalculation and recalculation-before-reset are serialized with Phase 2A's user lock. Recalculation observes committed canonical facts; a reset after a completed recalculation requires another explicit recalculation to refresh projections. History survives in either ordering.

## Validation and remaining scope

Final result: **154 tests passed, no skips**, including 134 SQLite/pure/API tests and 20 PostgreSQL tests. Phase 2C adds 51 tests to the existing 103-test suite.

The suite covers attribution, exact-card deduplication, late uploads, local-midnight/DST boundaries, partial/missed/multiple-missed days, overachievement, ordered halves, weekdays, infeasible/expired deadlines, current-day capacity, stable IDs/revisions, reset/delete/add history, early/late exam passes, legacy passes, content readiness, ownership, read-only GET, and rollback.

PostgreSQL checks cover migration/backfill/reversibility, real UTC/local-date attribution, unique item positions, concurrent recalculation producing one revision, rollback after rows have been replaced, and both reset/recalculation orderings. Existing Phase 1/2A/2B tests remain in the full suite.

```sh
.venv/bin/python -W ignore::DeprecationWarning -m unittest discover -s tests -v
TEST_DATABASE_URL='postgresql+psycopg://USER@HOST:PORT/TEST_DB' .venv/bin/python -W ignore::DeprecationWarning -m unittest discover -s tests -v
```

Use a disposable PostgreSQL database/account permitted to create/drop test schemas. Production tests skip if `TEST_DATABASE_URL` is absent.

Phase 2D is not started. Automatic mutation hooks, Swift integration, notifications, and health classification remain separate work requiring confirmation. A later small health step can consume remaining work, preferred/required daily workload, deadline feasibility, and blocked projections without changing canonical learning truth.
