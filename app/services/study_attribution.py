"""Pure attribution of accepted facts to targets; receipts preserve deleted/reset facts."""
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime
from uuid import UUID

from app.services.study_calendar import as_utc, local_date


@dataclass(frozen=True)
class LearningEvent:
    card_id: UUID
    chapter_id: UUID
    epoch: UUID
    learned_at: datetime


@dataclass(frozen=True)
class LearningTarget:
    id: UUID
    chapter_id: UUID
    epoch: UUID | None
    day: date
    count: int
    position: int


def receipt_events(receipts: list[dict], chapter_ids: set[UUID]) -> list[LearningEvent]:
    events = {}
    for receipt in receipts:
        completed_at = as_utc(datetime.fromisoformat(receipt["completed_at"].replace("Z", "+00:00")))
        for group in receipt["decks"]:
            chapter_id = UUID(group["deck_id"])
            if chapter_id not in chapter_ids:
                continue
            epoch = UUID(group["progress_epoch"])
            # Already-learned IDs are acknowledgements, never new learning events.
            for value in group["accepted_card_ids"]:
                card_id = UUID(value)
                key = (epoch, card_id)
                event = LearningEvent(card_id, chapter_id, epoch, completed_at)
                if key not in events or completed_at < events[key].learned_at:
                    events[key] = event
    return sorted(events.values(), key=lambda event: (event.learned_at, event.card_id, event.epoch))


def attribute_learning(targets: list[LearningTarget], events: list[LearningEvent], timezone_name: str) -> dict[UUID, int]:
    """Same local day/chapter/epoch only; deterministic capped allocation, once per fact."""
    buckets = defaultdict(list)
    for target in sorted(targets, key=lambda item: (item.day, item.position, item.id)):
        buckets[(target.day, target.chapter_id, target.epoch)].append(target)
    actual = {target.id: 0 for target in targets}
    used = set()
    for event in sorted(events, key=lambda item: (as_utc(item.learned_at), item.card_id, item.epoch)):
        key = (event.epoch, event.card_id)
        if key in used:
            continue
        used.add(key)
        for target in buckets.get((local_date(event.learned_at, timezone_name), event.chapter_id, event.epoch), []):
            if actual[target.id] < target.count:
                actual[target.id] += 1
                break
    return actual
