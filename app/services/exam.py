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
from app.services.exam_groups import split_chapters
from app.services.chapters import ordered_chapters
from app.services.chapter_progress import get_chapter_progress
from app.services.study_progress import lock_progress_user


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
        applicable_types: set[ExamType],
    ):
        # The caller holds the user lock through commit, including first creation.
        # Retain legacy definitions even when their group is no longer applicable.
        definitions = {ExamType(exam.exam_type): exam for exam in db.scalars(
            select(Exam).where(Exam.deck_id == parent_deck.id)
        ).all()}
        for exam_type in self.exam_types:
            if exam_type in applicable_types and exam_type not in definitions:
                exam = Exam(
                    deck_id=parent_deck.id,
                    exam_type=exam_type.value,
                    question_count=0,
                    passing_score=settings.exam_passing_score,
                )
                db.add(exam)
                definitions[exam_type] = exam
        db.flush()
        return definitions

    def get_status(
        self,
        parent_deck_id: uuid.UUID,
        db: Session,
        current_user: User,
    ):
        # Shared with learning/reset; submission retains this lock until its attempt
        # and progression are committed. Status callers must also commit/rollback.
        lock_progress_user(db, current_user.id)
        parent_deck = self.get_parent_deck(
            parent_deck_id,
            db,
            current_user,
        )
        chapters = ordered_chapters(db, parent_deck, lock=True)
        first_half, second_half = split_chapters(chapters)
        groups = {
            ExamType.first_half: first_half,
            ExamType.second_half: second_half,
            ExamType.final: chapters,
        }
        progress = get_chapter_progress(db, current_user.id, chapters)
        definitions = self.get_or_create_definitions(
            parent_deck, db, {exam_type for exam_type, group in groups.items() if group},
        )
        progression = db.scalar(
            select(UserExamProgression).where(
                UserExamProgression.user_id == current_user.id,
                UserExamProgression.deck_id == parent_deck.id,
            ).execution_options(populate_existing=True)
        )

        if progression is None:
            progression = UserExamProgression(
                user_id=current_user.id,
                deck_id=parent_deck.id,
            )
            db.add(progression)
            db.flush()

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

        prerequisites = {
            ExamType.first_half: bool(first_half) and all(progress[chapter.id].completed for chapter in first_half),
            ExamType.second_half: bool(second_half) and all(progress[chapter.id].completed for chapter in second_half),
            # With one chapter, first_half is the last applicable half exam.
            ExamType.final: passed[ExamType.second_half if second_half else ExamType.first_half],
        }
        statuses = []
        for exam_type in self.exam_types:
            applicable = bool(groups[exam_type])
            available = applicable and (passed[exam_type] or prerequisites[exam_type])
            exam = definitions.get(exam_type)
            statuses.append({
                "exam_id": exam.id if exam else None,
                "exam_type": exam_type,
                "status": "completed" if passed[exam_type]
                else "not_applicable" if not applicable
                else "unlocked" if available else "locked",
                "applicable": applicable,
                "available": available,
                "completed": passed[exam_type],
                "passed": passed[exam_type],
                "chapter_ids": [chapter.id for chapter in groups[exam_type]],
                "best_score": best_scores[exam_type],
                "attempt_count": attempt_counts[exam_type],
                "completed_at": completed_at[exam_type],
            })

        return {
            "deck_id": parent_deck.id,
            "exams": statuses,
        }

    @staticmethod
    def require_available(progression_status: dict, exam_type: ExamType) -> dict:
        item = next(item for item in progression_status["exams"] if item["exam_type"] == exam_type)
        if not item["available"]:
            raise HTTPException(status_code=403, detail={
                "code": "exam_not_applicable" if not item["applicable"] else "exam_locked",
                "message": "This exam is not applicable." if not item["applicable"] else "This exam is locked.",
            })
        return item

    def get_exam(
        self,
        parent_deck_id: uuid.UUID,
        exam_type: ExamType,
        db: Session,
        current_user: User,
    ):
        self.require_available(self.get_status(parent_deck_id, db, current_user), exam_type)
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
        if parent_deck.user_id != current_user.id:
            raise HTTPException(status_code=404, detail="Deck not found")
        sub_decks = ordered_chapters(db, parent_deck)

        if not sub_decks:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="The parent deck has no sub-decks for an exam.",
            )

        first_half, second_half = split_chapters(sub_decks)

        if exam_type == ExamType.first_half:
            selected_decks = first_half
        elif exam_type == ExamType.second_half:
            selected_decks = second_half
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
