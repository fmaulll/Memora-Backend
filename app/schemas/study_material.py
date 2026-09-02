import uuid

from pydantic import BaseModel, ConfigDict


class StudyMaterialResponse(BaseModel):
    model_config = ConfigDict(
        from_attributes=True,
    )

    id: uuid.UUID
    filename: str


class StudyMaterialUploadResponse(BaseModel):
    materials: list[StudyMaterialResponse]