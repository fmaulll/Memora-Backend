import uuid
import logging

from sqlalchemy import select

from app.ai.deepseek import DeepSeekService
from app.db.database import SessionLocal
from app.models.deck import Deck
from app.models.card import Card
from app.schemas.ai import ChapterPlan, DeckPlanResponse
from app.services.study_plan import reconcile_generated_plan
from app.services.study_progress import lock_progress_user

logger = logging.getLogger(__name__)


class DeckGenerationService:

    max_attempts = 3

    async def _generate_chapter_with_retries(
        self,
        ai_service,
        plan,
        chapter_plan,
    ):
        last_error = None

        for attempt in range(1, self.max_attempts + 1):
            try:
                return await ai_service.generate_chapter(
                    plan,
                    chapter_plan,
                )
            except Exception as error:
                last_error = error
                logger.warning("chapter_generation_retry attempt=%s max_attempts=%s error_type=%s",
                               attempt, self.max_attempts, type(error).__name__)

        raise last_error

    async def generate_deck(
        self,
        parent_deck_id: uuid.UUID,
        plan: DeckPlanResponse,
    ):
        db = SessionLocal()

        try:
            parent_deck = db.get(Deck, parent_deck_id)

            if not parent_deck or parent_deck.generation_status == "completed":
                return

            user_id = parent_deck.user_id
            ai_service = DeepSeekService()

            # Generate chapters one by one
            for chapter_plan in plan.chapters:
                lock_progress_user(db, user_id)
                chapter_deck = (
                    db.query(Deck)
                    .filter(
                        Deck.parent_deck_id == parent_deck_id,
                        Deck.user_id == user_id,
                        Deck.title == chapter_plan.title,
                    )
                    .populate_existing()
                    .first()
                )

                if not chapter_deck:
                    db.commit()
                    continue

                if chapter_deck.generation_status == "completed":
                    db.commit()
                    continue

                chapter_plan = ChapterPlan(
                    title=chapter_deck.title,
                    description=chapter_plan.description,
                    key_concepts=(
                        chapter_deck.key_concepts
                        if chapter_deck.key_concepts is not None
                        else chapter_plan.key_concepts
                    ),
                    card_count=(
                        chapter_deck.card_count
                        if chapter_deck.card_count is not None
                        else chapter_plan.card_count
                    ),
                )

                # Mark chapter as generating
                chapter_id = chapter_deck.id
                chapter_deck.generation_status = "generating"
                db.commit()

                try:
                    generated_chapter = await self._generate_chapter_with_retries(
                        ai_service,
                        plan,
                        chapter_plan,
                    )

                    lock_progress_user(db, user_id)
                    chapter_deck = db.get(Deck, chapter_id, populate_existing=True)
                    if chapter_deck is None or chapter_deck.generation_status == "completed":
                        db.commit()
                        continue
                    for generated_card in generated_chapter.cards:
                        card = Card(
                            deck_id=chapter_deck.id,
                            front=generated_card.front,
                            back=generated_card.back,
                        )

                        db.add(card)

                    chapter_deck.generation_status = "completed"
                    db.commit()

                except Exception:
                    db.rollback()
                    lock_progress_user(db, user_id)
                    chapter_deck = db.get(
                        Deck,
                        chapter_id,
                        populate_existing=True,
                    )

                    if chapter_deck and chapter_deck.generation_status != "completed":
                        chapter_deck.generation_status = "failed"
                    db.commit()

            self._finalize(db, parent_deck_id, user_id)

        finally:
            db.close()

    def _finalize(self, db, parent_deck_id, user_id):
        """Short final transaction; chapter data is already durable, with no AI lock held."""
        try:
            lock_progress_user(db, user_id)
            parent = db.get(Deck, parent_deck_id, populate_existing=True)
            if parent is None or parent.generation_status == "completed":
                db.commit()
                return
            unfinished = db.scalar(select(Deck.id).where(
                Deck.parent_deck_id == parent_deck_id, Deck.user_id == user_id,
                Deck.generation_status != "completed",
            ).limit(1))
            parent.generation_status = "failed" if unfinished else "completed"
            if unfinished is None:
                reconcile_generated_plan(db, parent)
            db.commit()
        except Exception as error:
            db.rollback()
            # Keep completed chapter/card commits. The existing worker records this
            # failure on its job; retry skips those chapters and retries finalization.
            lock_progress_user(db, user_id)
            parent = db.get(Deck, parent_deck_id, populate_existing=True)
            if parent is not None:
                parent.generation_status = "failed"
            db.commit()
            logger.error("generation_finalization_failed parent_deck_id=%s error_type=%s",
                         parent_deck_id, type(error).__name__)
            raise RuntimeError("Study plan finalization failed; completed chapters are retained. "
                               "Retry generation to reconcile without regenerating completed chapters.") from error
