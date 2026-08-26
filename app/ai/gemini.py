from google import genai

from app.schemas.ai import (
    DeckPlanRequest,
    DeckPlanResponse,
    GeneratedDeckResponse,
)


class GeminiService:

    def __init__(self):
        self.client = genai.Client()

    async def generate_deck_plan(
        self,
        request: DeckPlanRequest,
    ) -> DeckPlanResponse:

        prompt = f"""
            Create a learning plan for a flashcard deck.

            Topic:
            {request.topic}

            Education level:
            {request.education_level}

            Study goal:
            {request.study_goal}

            Requested number of flashcards:
            {request.card_count}

            Requirements:

            - Create a logical learning progression.
            - Adapt the difficulty to the education level.
            - Adapt the curriculum to the study goal.
            - Divide the topic into meaningful chapters.
            - Distribute exactly {request.card_count} flashcards across the chapters.
            - Do not create unnecessary chapters.
            - Each chapter must contain at least 1 flashcard.
            - Return only the requested structured output.
        """

        response = self.client.models.generate_content(
            model="gemini-3.5-flash-lite",
            contents=prompt,
            config={
                "response_mime_type": "application/json",
                "response_schema": DeckPlanResponse,
            },
        )

        plan = DeckPlanResponse.model_validate_json(
            response.text
        )

        total_cards = sum(
            chapter.card_count
            for chapter in plan.chapters
        )

        if total_cards != request.card_count:
            raise ValueError(
                f"AI generated {total_cards} cards, "
                f"but {request.card_count} were requested"
            )

        return plan

    async def generate_deck(
        self,
        plan: DeckPlanResponse,
    ) -> GeneratedDeckResponse:

        chapter_requirements = "\n".join(
            f"- {chapter.title}: exactly {chapter.card_count} cards"
            for chapter in plan.chapters
        )

        prompt = f"""
            Create flashcards for a learning deck.

            Deck title:
            {plan.title}

            Subject:
            {plan.subject}

            Education level:
            {plan.education_level}

            Chapters and required card counts:
            {chapter_requirements}

            Requirements:

            - Generate exactly the requested number of cards for every chapter.
            - Every chapter must contain exactly its requested card count.
            - Follow the chapter order.
            - Cover the chapter's important concepts.
            - Adapt the difficulty to the education level.
            - Each flashcard must test one clear concept.
            - The front should contain a clear question or prompt.
            - The back should contain an accurate, concise answer.
            - Avoid duplicate questions.
            - Do not include information unrelated to the chapter.
            - Do not add or remove chapters.
            - Return only the requested structured output.
        """

        response = self.client.models.generate_content(
            model="gemini-3.5-flash-lite",
            contents=prompt,
            config={
                "response_mime_type": "application/json",
                "response_schema": GeneratedDeckResponse,
            },
        )

        deck = GeneratedDeckResponse.model_validate_json(
            response.text
        )

        if len(deck.chapters) != len(plan.chapters):
            raise ValueError(
                "AI generated an unexpected number of chapters"
            )

        for expected, generated in zip(
            plan.chapters,
            deck.chapters,
        ):
            if expected.title != generated.title:
                raise ValueError(
                    f"Chapter mismatch: "
                    f"expected '{expected.title}', "
                    f"got '{generated.title}'"
                )

            if len(generated.cards) != expected.card_count:
                raise ValueError(
                    f"Chapter '{expected.title}' generated "
                    f"{len(generated.cards)} cards, "
                    f"expected {expected.card_count}"
                )

        return deck
        
