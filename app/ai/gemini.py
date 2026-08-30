from google import genai

from app.schemas.ai import (
    DeckPlanRequest,
    DeckPlanResponse,
    GeneratedDeckResponse,
    GeneratedChapter,
    GeneratedChapterCards,
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
                "response_schema": GeneratedChapterCards,
            },
        )

        generated_cards = GeneratedChapterCards.model_validate_json(
            response.text
        )

        if len(generated_cards.cards) != chapter.card_count:
            raise ValueError(
                f"Chapter '{chapter.title}' generated "
                f"{len(generated.cards)} cards, "
                f"expected {chapter.card_count}"
            )

        return GeneratedChapter(
            title=chapter.title,
            cards=generated_cards.cards,
        )

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

            Study purpose:
            {request.study_purpose}

            Study goal:
            {request.study_goal}

            Learning depth:
            {request.learning_depth}

            Target date:
            {request.target_date if request.target_date else "No target date provided"}

            Your task is to design the curriculum first and determine how many
            flashcards are necessary to adequately cover the important knowledge
            within each chapter.

            The curriculum must be adapted to the user's study purpose.

            Study purpose guidance:

            - If the purpose is "Learn from Scratch", prioritize strong foundational
            understanding and logical progression from beginner concepts.

            - If the purpose is "Expand My Knowledge", focus on broadening and
            deepening the user's understanding of the topic.

            - If the purpose is "Prepare for an Exam", prioritize concepts that are
            important for exam preparation and ensure the curriculum efficiently
            covers the required knowledge.

            - If the purpose is "Prepare for a Certification", prioritize knowledge,
            concepts, terminology, and practical understanding relevant to the
            certification goal.

            Target date guidance:

            - If a target date is provided, consider the available preparation time
            when designing the learning plan.
            - The deck should remain realistic to study before the target date.
            - Do not unnecessarily reduce important knowledge just to fit the deadline.
            - Instead, prioritize the most important concepts when the available
            preparation time is limited.
            - If no target date is provided, create the curriculum based primarily
            on the requested learning depth and study purpose.

            Requirements:

            - Create a logical learning progression from foundational concepts to
            more advanced concepts.
            - Adapt the difficulty to the education level.
            - Adapt the curriculum to the study purpose.
            - Adapt the curriculum to the study goal.
            - Adapt the amount of content to the requested learning depth.
            - Divide the topic into meaningful chapters.
            - Do not create unnecessary chapters.
            - Each chapter must contain enough flashcards to properly cover its
            important concepts.
            - Do not force every chapter to have the same number of flashcards.
            - Simple chapters may require fewer flashcards.
            - Broad or concept-heavy chapters may require significantly more flashcards.
            - Determine the appropriate flashcard count for each chapter based on
            the amount of knowledge that should be covered.
            - Avoid artificially limiting chapters to a small fixed number of cards.
            - Avoid creating redundant flashcards just to increase the card count.
            - Each chapter should normally contain between 3 and 60 flashcards.
            - The total deck should normally remain below 300 flashcards.
            - These are safety limits, not targets.
            - Do not add cards simply to reach a limit.
            - Return only the requested structured output.

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
