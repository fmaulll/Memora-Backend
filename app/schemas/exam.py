import uuid
from datetime import datetime
from enum import Enum

from pydantic import BaseModel, ConfigDict

from app.schemas.card import CardResponse


class ExamType(str, Enum):
    first_half = "first_half"
    second_half = "second_half"
    final = "final"


class ExamResponse(BaseModel):
    parent_deck_id: uuid.UUID
    exam_type: ExamType
    sub_deck_ids: list[uuid.UUID]
    cards: list[CardResponse]


class ExamStatusResponse(BaseModel):
    exam_id: uuid.UUID
    exam_type: ExamType
    status: str
    passed: bool
    best_score: int | None
    attempt_count: int
    completed_at: datetime | None


class ExamProgressionResponse(BaseModel):
    deck_id: uuid.UUID
    exams: list[ExamStatusResponse]


class ExamQuestionResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    position: int
    question_type: str
    question: str
    options: list[str]


class ExamQuestionsResponse(BaseModel):
    exam_id: uuid.UUID
    exam_type: ExamType
    question_count: int
    questions: list[ExamQuestionResponse]


class ExamAnswer(BaseModel):
    question_id: uuid.UUID
    answer: str


class ExamSubmissionRequest(BaseModel):
    answers: list[ExamAnswer]


class ExamSubmissionResponse(BaseModel):
    exam_id: uuid.UUID
    exam_type: ExamType
    total_questions: int
    correct_answers: int
    incorrect_answers: int
    score: float
    passing_score: float
    passed: bool
    attempt_number: int
    next_exam_type: ExamType | None
    next_exam_unlocked: bool
    completed: bool