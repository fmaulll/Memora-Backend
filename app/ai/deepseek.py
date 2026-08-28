import os
import json
import asyncio

from openai import OpenAI

from app.schemas.ai import (
    ChapterPlan,
    DeckPlanRequest,
    DeckPlanResponse,
    GeneratedDeckResponse,
    GeneratedChapter,
    GeneratedChapterCards,
)

class DeepSeekService:

    def __init__(self):
        self.client = OpenAI(
            api_key=os.environ.get('DEEPSEEK_API_KEY'),
            base_url="https://api.deepseek.com",
        )

    async def generate_chapter(
        self,
        plan: DeckPlanResponse,
        chapter: ChapterPlan,
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
            - Cover all important key concepts.
            - Adapt the difficulty to the education level.
            - Each flashcard must test one clear concept.
            - The front must contain a clear question or prompt.
            - The back must contain an accurate and concise answer.
            - Avoid duplicate questions.
            - Do not include unrelated information.
            - Return only the flashcards.
            - Do not return the chapter title.
            - Do not return the deck title.
            - Do not return any metadata.

            Return valid JSON only.

            The JSON must have this structure:

            {{
                "cards": [
                    {{
                        "front": "string",
                        "back": "string"
                    }}
                ]
            }}
        """

        response = await asyncio.to_thread(
            self.client.chat.completions.create,
            model="deepseek-v4-flash",
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You generate high-quality educational flashcards. "
                        "Return valid JSON only."
                    ),
                },
                {
                    "role": "user",
                    "content": prompt,
                },
            ],
            response_format={
                "type": "json_object"
            },
        )

        content = response.choices[0].message.content

        if not content:
            raise ValueError(
                f"DeepSeek returned an empty response "
                f"for chapter '{chapter.title}'"
            )

        generated_cards = GeneratedChapterCards.model_validate_json(
            content
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

            Return valid JSON only.

            The JSON must have this structure:

            {{
            "title": "string",
            "subject": "string",
            "education_level": "string",
            "chapters": [
                {{
                "title": "string",
                "description": "string",
                "key_concepts": [
                    "string"
                ],
                "card_count": 10
                }}
            ]
            }}
        """

        response = await asyncio.to_thread(
            self.client.chat.completions.create,
            model="deepseek-v4-flash",
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are an AI curriculum designer. "
                        "Return valid JSON only."
                    ),
                },
                {
                    "role": "user",
                    "content": prompt,
                },
            ],
            response_format={
                "type": "json_object"
            },
        )

        content = response.choices[0].message.content

        if not content:
            raise ValueError(
                "DeepSeek returned an empty response"
            )

        plan = DeckPlanResponse.model_validate_json(
            content
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