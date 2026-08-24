from google import genai

from app.schemas.ai import DeckPlanRequest, DeckPlanResponse


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

        return DeckPlanResponse.model_validate_json(
            response.text
        )