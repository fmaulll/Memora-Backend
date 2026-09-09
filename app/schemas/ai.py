from pydantic import BaseModel, Field, model_validator
from datetime import date
import uuid


class DeckPlanRequest(BaseModel):
    topic: str
    education_level: str
    study_purpose: str
    learning_language: str

    preparation_details: str | None = None

    target_date: date | None = None

    study_material_ids: list[uuid.UUID] = Field(
        default_factory=list
    )


class ChapterPlan(BaseModel):
    title: str
    description: str
    key_concepts: list[str]
    card_count: int


class DeckPlanResponse(BaseModel):
    title: str = Field(min_length=1)
    subject: str = Field(min_length=1)
    education_level: str = Field(min_length=1)
    learning_language: str = Field(min_length=1)
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


class GeneratedExamQuestion(BaseModel):
    question_type: str
    question: str = Field(min_length=1)
    options: list[str] = Field(default_factory=list)
    correct_answer: str = Field(min_length=1)
    explanation: str = Field(min_length=1)
    source_card_id: uuid.UUID | None = None


class GeneratedExamQuestions(BaseModel):
    questions: list[GeneratedExamQuestion]


class GeneratedChapter(BaseModel):
    title: str = Field(min_length=1)
    cards: list[GeneratedCard]


class GeneratedDeckResponse(BaseModel):
    title: str = Field(min_length=1)
    subject: str = Field(min_length=1)
    education_level: str = Field(min_length=1)
    learning_language: str = Field(min_length=1)
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
    learning_language: str
    generation_status: str

    chapters: list[GeneratedChapterStatus]

class GeneratedDeckWithTimelineResponse(BaseModel):
    deck: GeneratedDeckStatus
    timeline: StudyTimeline | None = None

class GenerateDeckRequest(BaseModel):
    plan: DeckPlanResponse
    study_purpose: str
    target_date: date | None = None

    @model_validator(mode="after")
    def validate_generation_limits(self):
        if len(self.plan.chapters) > 20 or any(not 1 <= chapter.card_count <= 100 for chapter in self.plan.chapters):
            raise ValueError("Generation allows 1–20 chapters with 1–100 cards per chapter")
        return self
