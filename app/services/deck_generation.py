import uuid

from app.ai.deepseek import DeepSeekService
from app.db.database import SessionLocal
from app.models.deck import Deck
from app.models.card import Card
from app.schemas.ai import ChapterPlan, DeckPlanResponse


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
                print(
                    f"Failed generating chapter "
                    f"'{chapter_plan.title}' "
                    f"(attempt {attempt}/{self.max_attempts}): {error}"
                )

        raise last_error

    async def generate_deck(
        self,
        parent_deck_id: uuid.UUID,
        plan: DeckPlanResponse,
    ):
        db = SessionLocal()

        try:
            parent_deck = db.get(Deck, parent_deck_id)

            if not parent_deck:
                return

            ai_service = DeepSeekService()

            # Generate chapters one by one
            for chapter_plan in plan.chapters:

                chapter_deck = (
                    db.query(Deck)
                    .filter(
                        Deck.parent_deck_id == parent_deck_id,
                        Deck.title == chapter_plan.title,
                    )
                    .first()
                )

                if not chapter_deck:
                    continue

                if chapter_deck.generation_status == "completed":
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
                chapter_deck.generation_status = "generating"
                db.commit()

                try:
                    generated_chapter = await self._generate_chapter_with_retries(
                        ai_service,
                        plan,
                        chapter_plan,
                    )

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

                    chapter_deck = db.get(
                        Deck,
                        chapter_deck.id,
                    )

                    if chapter_deck:
                        chapter_deck.generation_status = "failed"
                        db.commit()

            # Check final status
            remaining = (
                db.query(Deck)
                .filter(
                    Deck.parent_deck_id == parent_deck_id,
                    Deck.generation_status != "completed",
                )
                .count()
            )

            parent_deck = db.get(
                Deck,
                parent_deck_id,
            )

            if parent_deck:
                if remaining == 0:
                    parent_deck.generation_status = "completed"
                else:
                    parent_deck.generation_status = "failed"

                db.commit()

        finally:
            db.close()