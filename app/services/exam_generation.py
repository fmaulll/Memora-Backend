import uuid

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.ai.deepseek import DeepSeekService
from app.models.card import Card
from app.models.exam import Exam, ExamQuestion
from app.models.user import User
from app.schemas.exam import ExamType
from app.services.exam import ExamService


class ExamGenerationService:

    def __init__(self, ai_service=None):
        self.ai_service = ai_service or DeepSeekService()
        self.exam_service = ExamService()

    def _validate_unlocked(
        self,
        exam: Exam,
        db: Session,
        current_user: User,
    ):
        progression = self.exam_service.get_status(
            exam.deck_id,
            db,
            current_user,
        )
        current_status = next(
            item for item in progression["exams"]
            if item["exam_id"] == exam.id
        )

        if current_status["status"] == "locked":
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="This exam is locked.",
            )

    def _validate_question(self, question, selected_card_ids):
        if question.question_type not in {"multiple_choice", "true_false"}:
            raise ValueError(
                f"Unsupported question type: {question.question_type}"
            )

        if question.question_type == "multiple_choice":
            if len(question.options) < 2 or len(set(question.options)) != len(question.options):
                raise ValueError("Multiple-choice options must be unique and contain at least two values")
        elif question.options != ["True", "False"]:
            raise ValueError("True/false questions must use True and False options")

        if question.options.count(question.correct_answer) != 1:
            raise ValueError("The correct answer must occur exactly once in options")

        if question.source_card_id is not None and question.source_card_id not in selected_card_ids:
            raise ValueError("Question source card is not part of this exam")

    async def generate(
        self,
        exam_id: uuid.UUID,
        db: Session,
        current_user: User,
    ):
        exam = db.scalar(
            select(Exam)
            .join(Exam.deck)
            .where(
                Exam.id == exam_id,
                Exam.deck.has(user_id=current_user.id),
            )
        )

        if exam is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Exam not found",
            )

        self._validate_unlocked(exam, db, current_user)
        existing = db.scalars(
            select(ExamQuestion)
            .where(ExamQuestion.exam_id == exam.id)
            .order_by(ExamQuestion.position.asc())
        ).all()
        if existing:
            return exam, existing

        parent_deck = self.exam_service.get_parent_deck(
            exam.deck_id,
            db,
            current_user,
        )
        selected_deck_ids, cards = self.exam_service.get_selected_cards(
            parent_deck,
            ExamType(exam.exam_type),
            db,
            current_user,
        )
        unique_cards = list({card.id: card for card in cards}.values())

        if not unique_cards:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="This exam has no source cards.",
            )

        if exam.question_count <= 0:
            exam.question_count = min(10, len(unique_cards))

        selected_cards = unique_cards[:exam.question_count]
        try:
            generated = await self.ai_service.generate_exam_questions(
                exam,
                selected_cards,
                exam.question_count,
            )
        except Exception:
            db.rollback()
            raise

        if not generated.questions:
            raise ValueError("AI returned no exam questions")
        if len(generated.questions) > exam.question_count:
            raise ValueError("AI returned more questions than requested")

        selected_card_ids = {card.id for card in selected_cards}
        questions = []
        seen_questions = set()
        try:
            for position, generated_question in enumerate(generated.questions, start=1):
                self._validate_question(generated_question, selected_card_ids)
                normalized = generated_question.question.strip().casefold()
                if normalized in seen_questions:
                    raise ValueError("AI returned duplicate questions")
                seen_questions.add(normalized)
                questions.append(
                    ExamQuestion(
                        exam_id=exam.id,
                        question_type=generated_question.question_type,
                        question=generated_question.question,
                        options=generated_question.options,
                        correct_answer=generated_question.correct_answer,
                        explanation=generated_question.explanation,
                        source_card_id=generated_question.source_card_id,
                        position=position,
                    )
                )

            db.add_all(questions)
            db.commit()
        except Exception:
            db.rollback()
            raise

        return exam, questions