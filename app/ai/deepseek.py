import os
import json
import asyncio

from openai import OpenAI
from app.models.study_material import StudyMaterial

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

            Learning language:
            {plan.learning_language}

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
            - The flashcards must be written in the specified learning language.

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
        materials: list[StudyMaterial] | None = None,
    ) -> DeckPlanResponse:

        material_section = ""

        if materials:
            material_text = "\n\n".join(
                f"Source: {material.filename}\n"
                f"{material.extracted_text}"
                for material in materials
            )

            material_section = f"""
        STUDY MATERIALS PROVIDED BY THE USER:

        {material_text}

        IMPORTANT:
        - Use these materials as supplemental context.
        - Prioritize relevant information from these materials.
        - Do not mention the uploaded files in the generated response.
        """
        prompt = f"""
            Create a high-quality learning plan for a flashcard deck.

            Topic:
            {request.topic}

            Learning language:
            {request.learning_language}

            Education level:
            {request.education_level}

            Study purpose:
            {request.study_purpose}

            Additional preparation details:
            {request.preparation_details}

            Target date:
            {
                request.target_date
                if request.target_date
                else "No target date provided"
            }

            {material_section}

            Your task is to design the curriculum and determine
            how many flashcards are necessary to adequately cover
            the important knowledge within each chapter.

            Learning language guidance:
            - The deck must be written in the specified learning language.
            - The flashcards must be written in the specified learning language.

            Study purpose guidance:

            - If the purpose is "Learn from Scratch", prioritize
            strong foundational understanding and a logical
            progression from beginner concepts.

            - If the purpose is "Expand My Knowledge", focus on
            broadening and deepening understanding beyond the basics.

            - If the purpose is "Prepare for an Exam", prioritize
            concepts most likely to be important for examination
            and ensure efficient coverage of required knowledge.

            - If the purpose is "Prepare for a Certification",
            prioritize certification-relevant concepts,
            terminology, and practical understanding.

            - If the purpose is "Career", prioritize practical,
            job-relevant knowledge and concepts that are useful
            in real-world work.

            Target date guidance:

            - If a target date is provided, consider the available
            preparation time.
            - Prioritize the most important knowledge when time
            is limited.
            - Do not unnecessarily remove important knowledge
            solely to fit a deadline.
            - The curriculum should remain realistic to study
            before the target date.

            Requirements:

            - Create a logical learning progression from foundational
            concepts to more advanced concepts.
            - Introduce prerequisites before dependent concepts.
            - Adapt difficulty to the education level.
            - Adapt the curriculum to the study purpose.
            - Use preparation details when relevant.
            - Use provided study materials as supplemental context.
            - Divide the topic into meaningful chapters.
            - Do not create unnecessary chapters.
            - Do not create duplicate chapters.

            For every chapter:

            - Provide a clear and specific chapter title.
            - Provide a concise description explaining what the
            learner will learn.
            - Provide a list of important key concepts.
            - Determine an appropriate number of flashcards based
            on breadth and complexity.
            - Do not force every chapter to have the same number
            of flashcards.

            Flashcard quantity:

            - Simple chapters may require fewer flashcards.
            - Broad or concept-heavy chapters may require more.
            - Each chapter should normally contain between
            3 and 60 flashcards.
            - The total deck should normally remain below
            300 flashcards.
            - These are safety limits, not targets.
            - Do not create redundant concepts simply to increase
            the card count.

            Return valid JSON only.

            The JSON must have this structure:

            {{
                "title": "string",
                "subject": "string",
                "education_level": "string",
                "learning_language": "string",
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

    def _build_material_context(
        self,
        materials: list[StudyMaterial] | None,
        max_chars_per_material: int = 12_000,
        max_total_chars: int = 30_000,
    ) -> str:

        if not materials:
            return ""

        sections = []
        total_chars = 0

        for material in materials:

            text = material.extracted_text.strip()

            if not text:
                continue

            remaining_chars = (
                max_total_chars - total_chars
            )

            if remaining_chars <= 0:
                break

            allowed_chars = min(
                max_chars_per_material,
                remaining_chars,
            )

            truncated_text = text[:allowed_chars]

            section = f"""
    Study material: {material.filename}

    {truncated_text}
    """

            sections.append(section)

            total_chars += len(truncated_text)

        if not sections:
            return ""

        return "\n\n".join(sections)