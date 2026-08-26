from pydantic import BaseModel, Field, model_validator


class DeckPlanRequest(BaseModel):
    topic: str
    education_level: str
    study_goal: str
    learning_depth: str


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


class GeneratedChapter(BaseModel):
    title: str = Field(min_length=1)
    cards: list[GeneratedCard]


class GeneratedDeckResponse(BaseModel):
    title: str = Field(min_length=1)
    subject: str = Field(min_length=1)
    education_level: str = Field(min_length=1)
    chapters: list[GeneratedChapter]