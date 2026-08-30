import uuid

from app.ai.deepseek import DeepSeekService
from app.db.database import SessionLocal
from app.models.deck import Deck
from app.models.card import Card
from app.schemas.ai import DeckPlanResponse


class DeckGenerationService:

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

                # Mark chapter as generating
                chapter_deck.generation_status = "generating"
                db.commit()

                try:
                    # Generate chapter cards
                    generated_chapter = (
                        await ai_service.generate_chapter(
                            plan,
                            chapter_plan,
                        )
                    )

                    # Save generated cards
                    for generated_card in generated_chapter.cards:
                        card = Card(
                            deck_id=chapter_deck.id,
                            front=generated_card.front,
                            back=generated_card.back,
                        )

                        db.add(card)

                    # Mark chapter as completed
                    chapter_deck.generation_status = "completed"

                    db.commit()

                except Exception as error:
                    db.rollback()

                    chapter_deck = db.get(
                        Deck,
                        chapter_deck.id,
                    )

                    if chapter_deck:
                        chapter_deck.generation_status = "failed"
                        db.commit()

                    print(
                        f"Failed generating chapter "
                        f"'{chapter_plan.title}': {error}"
                    )

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