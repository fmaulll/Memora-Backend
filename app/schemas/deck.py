import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, model_validator


class DeckCreate(BaseModel):
    id: uuid.UUID | None = None
    title: str
    subject: str
    education_level: str
    learning_language: str
    is_favorite: bool = False
    parent_deck_id: uuid.UUID | None = None
    position: int = Field(default=0, ge=0)
    key_concepts: list[str] | None = None
    card_count: int | None = Field(default=None, ge=0, description="Planned number of cards for generation")
    generation_status: str = "completed"


class DeckUpdate(BaseModel):
    title: str | None = None
    subject: str | None = None
    education_level: str | None = None
    learning_language: str | None = None
    is_favorite: bool | None = None
    parent_deck_id: uuid.UUID | None = None

    position: int | None = Field(default=None, ge=0)
    key_concepts: list[str] | None = None
    card_count: int | None = Field(default=None, ge=0, description="Planned number of cards for generation")
    generation_status: str | None = None

    @model_validator(mode="after")
    def reject_null_required_fields(self):
        for field in (
            "title", "subject", "education_level", "learning_language",
            "is_favorite", "position", "generation_status",
        ):
            if field in self.model_fields_set and getattr(self, field) is None:
                raise ValueError(f"{field} cannot be null")
        return self


class DeckResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    user_id: uuid.UUID

    parent_deck_id: uuid.UUID | None

    title: str
    position: int
    key_concepts: list[str] | None
    card_count: int | None
    subject: str
    education_level: str
    learning_language: str
    is_favorite: bool

    generation_status: str

    created_at: datetime
    updated_at: datetime

class ChapterGenerationStatus(BaseModel):
    id: uuid.UUID
    title: str
    generation_status: str
    card_count: int


class DeckGenerationStatusResponse(BaseModel):
    deck_id: uuid.UUID
    generation_status: str
    chapters: list[ChapterGenerationStatus]
