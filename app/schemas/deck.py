import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict


class DeckCreate(BaseModel):
    id: uuid.UUID | None = None
    title: str
    subject: str
    education_level: str
    learning_language: str
    is_favorite: bool = False
    parent_deck_id: uuid.UUID | None = None
    generation_status: str = "completed"


class DeckUpdate(BaseModel):
    title: str | None = None
    subject: str | None = None
    education_level: str | None = None
    learning_language: str | None = None
    is_favorite: bool | None = None
    parent_deck_id: uuid.UUID | None = None

class DeckResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    user_id: uuid.UUID

    parent_deck_id: uuid.UUID | None

    title: str
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