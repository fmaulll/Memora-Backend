import uuid
from datetime import datetime, timezone
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator

MAX_SUBMISSION_CARDS = 5000
MAX_SUBMISSION_DECKS = 100


class LearnedTransition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    card_id: uuid.UUID
    phase: Literal["review"]
    answer: Literal["got_it"]


class DeckEpoch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    deck_id: uuid.UUID
    progress_epoch: uuid.UUID


class DeckLearnedTransitions(DeckEpoch):
    learned_cards: list[LearnedTransition] = Field(min_length=1, max_length=MAX_SUBMISSION_CARDS)

    @field_validator("learned_cards")
    @classmethod
    def normalize_cards(cls, value: list[LearnedTransition]) -> list[LearnedTransition]:
        by_id = {transition.card_id: transition for transition in value}
        return [by_id[card_id] for card_id in sorted(by_id)]


class StudyProgressSubmission(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session_id: uuid.UUID
    completed_at: AwareDatetime
    decks: list[DeckLearnedTransitions] = Field(min_length=1, max_length=MAX_SUBMISSION_DECKS)

    @field_validator("completed_at")
    @classmethod
    def normalize_time(cls, value: datetime) -> datetime:
        return value.astimezone(timezone.utc)

    @field_validator("decks")
    @classmethod
    def normalize_decks(cls, value: list[DeckLearnedTransitions]) -> list[DeckLearnedTransitions]:
        if len({item.deck_id for item in value}) != len(value):
            raise ValueError("Each deck must occur once per submission")
        if sum(len(item.learned_cards) for item in value) > MAX_SUBMISSION_CARDS:
            raise ValueError(f"Submit at most {MAX_SUBMISSION_CARDS} unique cards per request")
        return sorted(value, key=lambda item: item.deck_id)


class ProgressResetRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reset_id: uuid.UUID
    expected_decks: list[DeckEpoch] = Field(min_length=1)

    @field_validator("expected_decks")
    @classmethod
    def normalize_decks(cls, value: list[DeckEpoch]) -> list[DeckEpoch]:
        if len({item.deck_id for item in value}) != len(value):
            raise ValueError("Each deck must occur once per reset")
        return sorted(value, key=lambda item: item.deck_id)


class DeckSubmissionResult(DeckEpoch):
    accepted_card_ids: list[uuid.UUID]
    already_learned_card_ids: list[uuid.UUID]


class StudyProgressSubmissionResponse(BaseModel):
    session_id: uuid.UUID
    completed_at: datetime
    submitted_at: datetime
    decks: list[DeckSubmissionResult]


class DeckResetResult(DeckEpoch):
    previous_epoch: uuid.UUID
    cleared_card_count: int


class ProgressResetResponse(BaseModel):
    reset_id: uuid.UUID
    deck_id: uuid.UUID
    reset_at: datetime
    decks: list[DeckResetResult]


class CardLearningFact(BaseModel):
    card_id: uuid.UUID
    learned_at: datetime | None


class ChapterProgress(BaseModel):
    learned_card_count: int
    total_card_count: int
    completion_percentage: float
    completed: bool


class DeckLearningFacts(DeckEpoch, ChapterProgress):
    title: str
    parent_deck_id: uuid.UUID | None
    position: int
    generation_status: str
    cards: list[CardLearningFact]


class ProgressSummary(BaseModel):
    total_deck_count: int
    completed_deck_count: int
    learned_card_count: int
    total_card_count: int


class StudyProgressResponse(BaseModel):
    deck_id: uuid.UUID
    decks: list[DeckLearningFacts]
    summary: ProgressSummary
