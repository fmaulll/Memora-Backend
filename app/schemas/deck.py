import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict


class DeckCreate(BaseModel):
    id: uuid.UUID | None = None
    title: str
    subject: str
    education_level: str
    is_favorite: bool = False
    parent_deck_id: uuid.UUID | None = None


class DeckUpdate(BaseModel):
    title: str | None = None
    subject: str | None = None
    education_level: str | None = None
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
    is_favorite: bool

    created_at: datetime
    updated_at: datetime