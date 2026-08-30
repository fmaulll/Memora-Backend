from pydantic import BaseModel, Field, model_validator
from datetime import date
import uuid


class DeckPlanRequest(BaseModel):
    topic: str
    education_level: str
    study_purpose: str
    study_goal: str
    learning_depth: str
    target_date: date | None = None


class ChapterPlan(BaseModel):
    title: str
    description: str
    key_concepts: list[str]
    card_count: int


class DeckPlanResponse(BaseModel):
    title: str = Field(min_length=1)
    subject: str = Field(min_length=1)
    education_level: str = Field(min_length=1)
    chapters: list[ChapterPlan]

    @model_validator(mode="after")
    def validate_plan(self):
        if not self.chapters:
            raise ValueError("Deck plan must contain at least one chapter")

        return self

class GeneratedCard(BaseModel):
    front: str = Field(min_length=1)
    back: str = Field(min_length=1)


class GeneratedChapterCards(BaseModel):
    cards: list[GeneratedCard]


class GeneratedChapter(BaseModel):
    title: str = Field(min_length=1)
    cards: list[GeneratedCard]


class GeneratedDeckResponse(BaseModel):
    title: str = Field(min_length=1)
    subject: str = Field(min_length=1)
    education_level: str = Field(min_length=1)
    chapters: list[GeneratedChapter]


class StudyDay(BaseModel):
    day: int
    date: date
    new_cards: int
    focus: str


class StudyTimeline(BaseModel):
    total_days: int
    total_cards: int
    daily_plan: list[StudyDay]


class GeneratedChapterStatus(BaseModel):
    id: uuid.UUID
    title: str
    generation_status: str



class GeneratedDeckStatus(BaseModel):
    id: uuid.UUID
    title: str
    subject: str
    education_level: str
    generation_status: str

    chapters: list[GeneratedChapterStatus]

class GeneratedDeckWithTimelineResponse(BaseModel):
    deck: GeneratedDeckStatus
    timeline: StudyTimeline | None = None

class GenerateDeckRequest(BaseModel):
    plan: DeckPlanResponse
    study_purpose: str
    target_date: date | None = None