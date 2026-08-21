import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict


class CardCreate(BaseModel):
    id: uuid.UUID
    front: str
    back: str
    front_image_url: str | None = None
    back_image_url: str | None = None

class BulkCardCreate(BaseModel):
    cards: list[CardCreate]

class CardUpdate(BaseModel):
    front: str | None = None
    back: str | None = None
    front_image_url: str | None = None
    back_image_url: str | None = None


class CardResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    deck_id: uuid.UUID
    front: str
    back: str
    front_image_url: str | None
    back_image_url: str | None
    created_at: datetime
    updated_at: datetime