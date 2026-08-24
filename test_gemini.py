from google import genai
from pydantic import BaseModel, Field
from typing import List


class Chapter(BaseModel):
    title: str
    card_count: int = Field(ge=1)


class LearningPlan(BaseModel):
    title: str
    subject: str
    recommended_card_count: int
    chapters: List[Chapter]


client = genai.Client()

response = client.models.generate_content(
    model="gemini-3.5-flash-lite",
    contents="""
    Create a learning plan for:

    Topic: Python programming from scratch
    Education level: University
    Study goal: Learn from scratch
    Requested cards: 50

    Create a sensible progression from beginner concepts
    toward more advanced concepts.

    Distribute the 50 cards across the chapters.
    """,
    config={
        "response_mime_type": "application/json",
        "response_schema": LearningPlan,
    },
)

plan = LearningPlan.model_validate_json(response.text)

print()
print("========== GEMINI LEARNING PLAN ==========")
print()
print("TITLE:", plan.title)
print("SUBJECT:", plan.subject)
print("RECOMMENDED CARDS:", plan.recommended_card_count)
print()
print("CHAPTERS:")

for index, chapter in enumerate(plan.chapters, start=1):
    print(f"{index}. {chapter.title} → {chapter.card_count} cards")

print()
print("TOTAL CARDS:", sum(c.card_count for c in plan.chapters))
print()
print("RAW RESPONSE:")
print(response.text)