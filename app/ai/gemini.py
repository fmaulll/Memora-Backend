from google import genai

from app.schemas.ai import (
    DeckPlanRequest,
    DeckPlanResponse,
    GeneratedDeckResponse,
    GeneratedChapter,
)


class GeminiService:

    def __init__(self):
        self.client = genai.Client()

    async def generate_chapter(
        self,
        plan: DeckPlanResponse,
        chapter,
    ) -> GeneratedChapter:

        key_concepts = "\n".join(
            f"- {concept}"
            for concept in chapter.key_concepts
        )

        prompt = f"""
            Create flashcards for one chapter of a learning deck.

            Deck title:
            {plan.title}

            Subject:
            {plan.subject}

            Education level:
            {plan.education_level}

            Chapter:
            {chapter.title}

            Chapter description:
            {chapter.description}

            Key concepts that must be covered:
            {key_concepts}

            Required number of cards:
            {chapter.card_count}

            Requirements:

            - Generate exactly {chapter.card_count} flashcards.
            - The chapter MUST contain exactly {chapter.card_count} cards.
            - Cover all important key concepts.
            - Distribute cards according to the importance and complexity
            of each concept.
            - Do not create redundant cards simply to reach the required count.
            - Adapt the difficulty to the education level.
            - Each flashcard must test one clear concept.
            - The front should contain a clear question or prompt.
            - The back should contain an accurate, concise answer.
            - Avoid duplicate questions.
            - Avoid asking essentially the same question in different wording.
            - Do not include information unrelated to this chapter.
            - Return only the requested structured output.
        """

        response = self.client.models.generate_content(
            model="gemini-3.5-flash-lite",
            contents=prompt,
            config={
                "response_mime_type": "application/json",
                "response_schema": GeneratedChapter,
            },
        )

        generated = GeneratedChapter.model_validate_json(
            response.text
        )

        if generated.title != chapter.title:
            raise ValueError(
                f"Chapter mismatch: "
                f"expected '{chapter.title}', "
                f"got '{generated.title}'"
            )

        if len(generated.cards) != chapter.card_count:
            raise ValueError(
                f"Chapter '{chapter.title}' generated "
                f"{len(generated.cards)} cards, "
                f"expected {chapter.card_count}"
            )

        return generated

    async def generate_deck_plan(
        self,
        request: DeckPlanRequest,
    ) -> DeckPlanResponse:

        prompt = f"""
            Create a comprehensive learning plan for a flashcard deck.

            Topic:
            {request.topic}

            Education level:
            {request.education_level}

            Study goal:
            {request.study_goal}

            Learning depth:
            {request.learning_depth}

            Your task is to design the curriculum first and determine how many
            flashcards are necessary to adequately cover the important knowledge
            within each chapter.

            

            Requirements:

            - Create a logical learning progression from foundational concepts to more advanced concepts.
            - Order chapters so that prerequisites are introduced before concepts that depend on them.
            - Adapt the difficulty to the education level.
            - Adapt the curriculum to the study goal.
            - Adapt the amount of content to the requested learning depth.
            - Divide the topic into meaningful chapters.
            - Do not create unnecessary chapters.

            For every chapter:

            - Provide a clear and specific chapter title.
            - Provide a concise description explaining what the learner will learn.
            - Provide a list of the key concepts that must be covered in the chapter.
            - Determine the appropriate number of flashcards based on the breadth and complexity of those key concepts.
            - Ensure the card count is sufficient to properly cover the important knowledge in the chapter.
            - Do not force every chapter to have the same number of flashcards.
            - Simple chapters may require fewer flashcards.
            - Broad or concept-heavy chapters may require significantly more flashcards.

            Flashcard quantity:

            - Do not distribute a fixed number of flashcards across chapters.
            - Do not artificially limit chapters to a small number of cards.
            - Do not create redundant concepts simply to increase the card count.
            - Each chapter should normally contain between 3 and 60 flashcards.
            - The total deck should normally remain below 300 flashcards.
            - These are safety limits, not targets.

            Output:

            - Return only the requested structured output.
            - Every chapter must contain its title, description, key concepts, and estimated card count.
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

        return plan

    async def generate_deck(
        self,
        plan: DeckPlanResponse,
    ) -> GeneratedDeckResponse:

        generated_chapters = []

        for chapter in plan.chapters:
            generated_chapter = await self.generate_chapter(
                plan,
                chapter,
            )

            generated_chapters.append(
                generated_chapter
            )

        return GeneratedDeckResponse(
            title=plan.title,
            subject=plan.subject,
            education_level=plan.education_level,
            chapters=generated_chapters,
        )
