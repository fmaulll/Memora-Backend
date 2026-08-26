from fastapi import APIRouter, Depends

from app.ai.gemini import GeminiService
from app.schemas.ai import DeckPlanRequest, DeckPlanResponse, GeneratedDeckResponse


router = APIRouter(
    prefix="/ai",
    tags=["AI"],
)


@router.post(
    "/decks/plan",
    response_model=DeckPlanResponse,
)
async def generate_deck_plan(
    request: DeckPlanRequest,
):
    service = GeminiService()

    return await service.generate_deck_plan(request)

@router.post(
    "/decks/generate",
    response_model=GeneratedDeckResponse,
)
async def generate_deck(
    plan: DeckPlanResponse,
):
    service = GeminiService()

    return await service.generate_deck(plan)