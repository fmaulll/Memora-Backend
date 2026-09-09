"""Deck API regression tests using an isolated in-memory database."""
import unittest
import uuid
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.core.auth import get_current_user
from app.db.database import Base, get_db
from app.routers.decks import router
from app.schemas.ai import GeneratedDeckStatus


class DeckAPITests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine(
            "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
        )
        Base.metadata.create_all(self.engine)
        self.db = Session(self.engine)
        self.user = SimpleNamespace(id=uuid.uuid4())
        app = FastAPI()
        app.include_router(router)
        app.dependency_overrides[get_db] = lambda: self.db
        app.dependency_overrides[get_current_user] = lambda: self.user
        self.client = TestClient(app)
        self.payload = dict(
            title="Biology", subject="Science", education_level="High school",
            learning_language="English", position=3, key_concepts=["Cells"],
            card_count=12, generation_status="pending",
        )

    def tearDown(self):
        self.client.close()
        self.db.close()
        self.engine.dispose()

    def test_metadata_round_trip_and_partial_update(self):
        response = self.client.post("/decks", json=self.payload)
        self.assertEqual(response.status_code, 201, response.text)
        deck_id = response.json()["id"]
        for key, value in self.payload.items():
            self.assertEqual(response.json()[key], value)
        self.assertEqual(self.client.get(f"/decks/{deck_id}").json(), response.json())
        self.assertEqual(self.client.get("/decks").json(), [response.json()])
        update = dict(position=0, key_concepts=[], card_count=0, generation_status="completed")
        response = self.client.put(f"/decks/{deck_id}", json=update)
        self.assertEqual(response.status_code, 200, response.text)
        for key, value in update.items():
            self.assertEqual(response.json()[key], value)
        self.assertEqual(response.json()["title"], "Biology")
        response = self.client.put(f"/decks/{deck_id}", json={"card_count": None, "key_concepts": None})
        self.assertIsNone(response.json()["card_count"])
        self.assertIsNone(response.json()["key_concepts"])

    def test_defaults_and_invalid_metadata(self):
        payload = {k: v for k, v in self.payload.items() if k not in (
            "position", "key_concepts", "card_count", "generation_status"
        )}
        response = self.client.post("/decks", json=payload)
        self.assertEqual(response.status_code, 201, response.text)
        self.assertEqual(response.json()["position"], 0)
        self.assertEqual(response.json()["generation_status"], "completed")
        deck_id = response.json()["id"]
        for update in ({"position": -1}, {"card_count": -1}, {"position": None}, {"generation_status": None}):
            with self.subTest(update=update):
                self.assertEqual(self.client.put(f"/decks/{deck_id}", json=update).status_code, 422)

    def test_other_users_cannot_update_metadata(self):
        deck_id = self.client.post("/decks", json=self.payload).json()["id"]
        self.user.id = uuid.uuid4()
        self.assertEqual(self.client.put(f"/decks/{deck_id}", json={"position": 1}).status_code, 404)

    def test_ai_status_retains_language(self):
        result = GeneratedDeckStatus(
            id=uuid.uuid4(), title="Biology", subject="Science",
            education_level="High school", learning_language="English",
            generation_status="generating", chapters=[],
        )
        self.assertEqual(result.model_dump()["learning_language"], "English")


if __name__ == "__main__":
    unittest.main()
