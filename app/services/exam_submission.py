import uuid
from datetime import datetime

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.exam import Exam, ExamAttempt, ExamQuestion, UserExamProgression
from app.models.user import User
from app.schemas.exam import ExamSubmissionRequest, ExamSubmissionResponse, ExamType
from app.services.exam import ExamService


class ExamSubmissionService:

    def submit(
        self,
        exam_id: uuid.UUID,
        request: ExamSubmissionRequest,
        db: Session,
        current_user: User,
    ) -> ExamSubmissionResponse:
        if not request.answers:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Answers cannot be empty.",
            )

        exam = db.scalar(
            select(Exam).where(
                Exam.id == exam_id,
                Exam.deck.has(user_id=current_user.id),
            )
        )
        if exam is None:
            raise HTTPException(status_code=404, detail="Exam not found")

        exam_service = ExamService()
        parent_deck = exam_service.get_parent_deck(
            exam.deck_id,
            db,
            current_user,
        )
        progression_status = exam_service.get_status(
            parent_deck.id,
            db,
            current_user,
        )
        exam_service.require_available(progression_status, ExamType(exam.exam_type))

        questions = db.scalars(
            select(ExamQuestion)
            .where(ExamQuestion.exam_id == exam.id)
            .order_by(ExamQuestion.position.asc())
        ).all()
        if not questions:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="This exam has no persisted questions.",
            )

        submitted_ids = [answer.question_id for answer in request.answers]
        if len(submitted_ids) != len(set(submitted_ids)):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Duplicate question IDs are not allowed.",
            )

        question_by_id = {question.id: question for question in questions}
        unknown_ids = set(submitted_ids) - question_by_id.keys()
        if unknown_ids:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="One or more question IDs do not belong to this exam.",
            )

        if set(submitted_ids) != set(question_by_id):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Every exam question must be answered.",
            )

        answer_by_id = {
            answer.question_id: answer.answer.strip().casefold()
            for answer in request.answers
        }
        correct_count = sum(
            answer_by_id[question.id]
            == question.correct_answer.strip().casefold()
            for question in questions
        )
        total_questions = len(questions)
        score = round(correct_count / total_questions * 100, 2)
        passed = score >= exam.passing_score

        try:
            progression = db.scalar(
                select(UserExamProgression)
                .where(
                    UserExamProgression.user_id == current_user.id,
                    UserExamProgression.deck_id == parent_deck.id,
                )
                .with_for_update()
            )
            if progression is None:
                progression = UserExamProgression(
                    user_id=current_user.id,
                    deck_id=parent_deck.id,
                )
                db.add(progression)
                db.flush()

            attempt_number = self._attempt_count(progression, exam.exam_type) + 1
            attempt = ExamAttempt(
                user_id=current_user.id,
                exam_id=exam.id,
                attempt_number=attempt_number,
                total_questions=total_questions,
                correct_answers=correct_count,
                score=score,
                passed=passed,
            )
            db.add(attempt)
            self._update_progression(
                progression,
                exam.exam_type,
                score,
                passed,
            )
            db.flush()
            updated_status = exam_service.get_status(parent_deck.id, db, current_user)
            next_exam_type, next_unlocked, completed = self._next_status(exam.exam_type, updated_status)
            db.commit()
        except Exception:
            db.rollback()
            raise

        return ExamSubmissionResponse(
            exam_id=exam.id,
            exam_type=ExamType(exam.exam_type),
            total_questions=total_questions,
            correct_answers=correct_count,
            incorrect_answers=total_questions - correct_count,
            score=score,
            passing_score=float(exam.passing_score),
            passed=passed,
            attempt_number=attempt_number,
            next_exam_type=next_exam_type,
            next_exam_unlocked=next_unlocked,
            completed=completed,
        )

    def _attempt_count(self, progression, exam_type):
        return {
            "first_half": progression.first_half_attempt_count,
            "second_half": progression.second_half_attempt_count,
            "final": progression.final_attempt_count,
        }[exam_type]

    def _update_progression(self, progression, exam_type, score, passed):
        fields = {
            "first_half": ("first_half_passed", "first_half_best_score", "first_half_attempt_count", "first_half_completed_at"),
            "second_half": ("second_half_passed", "second_half_best_score", "second_half_attempt_count", "second_half_completed_at"),
            "final": ("final_passed", "final_best_score", "final_attempt_count", "final_completed_at"),
        }[exam_type]
        passed_field, score_field, count_field, completed_field = fields
        setattr(progression, count_field, getattr(progression, count_field) + 1)
        best_score = getattr(progression, score_field)
        if best_score is None or score > best_score:
            setattr(progression, score_field, score)
        if passed and not getattr(progression, passed_field):
            setattr(progression, passed_field, True)
            setattr(progression, completed_field, datetime.utcnow())

    def _next_status(self, exam_type, progression_status):
        exams = [item for item in progression_status["exams"] if item["applicable"]]
        current_index = next(i for i, item in enumerate(exams) if item["exam_type"] == exam_type)
        completed = next(item["passed"] for item in exams if item["exam_type"] == ExamType.final)
        if current_index == len(exams) - 1:
            return None, False, completed
        next_exam = exams[current_index + 1]
        return next_exam["exam_type"], next_exam["available"], completed
