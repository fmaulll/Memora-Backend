import math
import uuid

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.card import Card
from app.models.deck import Deck
from app.models.exam import Exam, UserExamProgression
from app.models.user import User
from app.db.database import settings
from app.schemas.exam import ExamType


class ExamService:

    exam_types = (
        ExamType.first_half,
        ExamType.second_half,
        ExamType.final,
    )

    def get_parent_deck(
        self,
        parent_deck_id: uuid.UUID,
        db: Session,
        current_user: User,
    ):
        parent_deck = db.scalar(
            select(Deck).where(
                Deck.id == parent_deck_id,
                Deck.user_id == current_user.id,
            )
        )

        if parent_deck is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Deck not found",
            )

        if parent_deck.parent_deck_id is not None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Exams can only be created for parent decks.",
            )

        return parent_deck

    def get_or_create_definitions(
        self,
        parent_deck: Deck,
        db: Session,
    ):
        sub_deck_ids = db.scalars(
            select(Deck.id)
            .where(Deck.parent_deck_id == parent_deck.id)
            .order_by(Deck.position.asc(), Deck.id.asc())
        ).all()

        midpoint = math.ceil(len(sub_deck_ids) / 2)
        groups = {
            ExamType.first_half: sub_deck_ids[:midpoint],
            ExamType.second_half: sub_deck_ids[midpoint:],
            ExamType.final: sub_deck_ids,
        }

        definitions = {}
        for exam_type in self.exam_types:
            exam = db.scalar(
                select(Exam).where(
                    Exam.deck_id == parent_deck.id,
                    Exam.exam_type == exam_type.value,
                )
            )

            if exam is None:
                exam = Exam(
                    deck_id=parent_deck.id,
                    exam_type=exam_type.value,
                    question_count=0,
                    passing_score=settings.exam_passing_score,
                )
                db.add(exam)
                db.flush()

            definitions[exam_type] = exam

        db.commit()
        return definitions

    def get_status(
        self,
        parent_deck_id: uuid.UUID,
        db: Session,
        current_user: User,
    ):
        parent_deck = self.get_parent_deck(
            parent_deck_id,
            db,
            current_user,
        )
        definitions = self.get_or_create_definitions(parent_deck, db)
        progression = db.scalar(
            select(UserExamProgression).where(
                UserExamProgression.user_id == current_user.id,
                UserExamProgression.deck_id == parent_deck.id,
            )
        )

        if progression is None:
            progression = UserExamProgression(
                user_id=current_user.id,
                deck_id=parent_deck.id,
            )
            db.add(progression)
            db.commit()

        passed = {
            ExamType.first_half: progression.first_half_passed,
            ExamType.second_half: progression.second_half_passed,
            ExamType.final: progression.final_passed,
        }
        best_scores = {
            ExamType.first_half: progression.first_half_best_score,
            ExamType.second_half: progression.second_half_best_score,
            ExamType.final: progression.final_best_score,
        }
        attempt_counts = {
            ExamType.first_half: progression.first_half_attempt_count,
            ExamType.second_half: progression.second_half_attempt_count,
            ExamType.final: progression.final_attempt_count,
        }
        completed_at = {
            ExamType.first_half: progression.first_half_completed_at,
            ExamType.second_half: progression.second_half_completed_at,
            ExamType.final: progression.final_completed_at,
        }

        statuses = []
        for index, exam_type in enumerate(self.exam_types):
            is_unlocked = all(
                passed[previous_exam_type]
                for previous_exam_type in self.exam_types[:index]
            )
            statuses.append({
                "exam_id": definitions[exam_type].id,
                "exam_type": exam_type,
                "status": "completed" if passed[exam_type]
                else "unlocked" if is_unlocked else "locked",
                "passed": passed[exam_type],
                "best_score": best_scores[exam_type],
                "attempt_count": attempt_counts[exam_type],
                "completed_at": completed_at[exam_type],
            })

        return {
            "deck_id": parent_deck.id,
            "exams": statuses,
        }

    def get_exam(
        self,
        parent_deck_id: uuid.UUID,
        exam_type: ExamType,
        db: Session,
        current_user: User,
    ):
        parent_deck = self.get_parent_deck(
            parent_deck_id,
            db,
            current_user,
        )

        selected_deck_ids, cards = self.get_selected_cards(
            parent_deck,
            exam_type,
            db,
            current_user,
        )

        return {
            "parent_deck_id": parent_deck.id,
            "exam_type": exam_type,
            "sub_deck_ids": selected_deck_ids,
            "cards": cards,
        }

    def get_selected_cards(
        self,
        parent_deck: Deck,
        exam_type: ExamType,
        db: Session,
        current_user: User,
    ):
        sub_decks = db.scalars(
            select(Deck)
            .where(
                Deck.parent_deck_id == parent_deck.id,
                Deck.user_id == current_user.id,
            )
            .order_by(Deck.position.asc(), Deck.id.asc())
        ).all()

        if not sub_decks:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="The parent deck has no sub-decks for an exam.",
            )

        midpoint = math.ceil(len(sub_decks) / 2)

        if exam_type == ExamType.first_half:
            selected_decks = sub_decks[:midpoint]
        elif exam_type == ExamType.second_half:
            selected_decks = sub_decks[midpoint:]
        else:
            selected_decks = sub_decks

        if not selected_decks:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    "This exam has no sub-decks. A deck with one "
                    "sub-deck supports first_half and final only."
                ),
            )

        selected_deck_ids = [deck.id for deck in selected_decks]

        cards = db.scalars(
            select(Card)
            .where(Card.deck_id.in_(selected_deck_ids))
            .order_by(Card.created_at.asc(), Card.id.asc())
        ).all()

        deck_order = {
            deck_id: index
            for index, deck_id in enumerate(selected_deck_ids)
        }
        cards.sort(
            key=lambda card: (
                deck_order[card.deck_id],
                card.created_at,
                card.id,
            )
        )

        return selected_deck_ids, cards