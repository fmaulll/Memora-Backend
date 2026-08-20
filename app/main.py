from fastapi import FastAPI

from app.db.database import Base, engine
from app.routers.auth import router as auth_router
from app.routers.decks import router as deck_router

# Import models so SQLAlchemy knows about them
from app.models.user import User
from app.models.deck import Deck


app = FastAPI(
    title="Memora API",
    version="0.1.0",
)


Base.metadata.create_all(bind=engine)


app.include_router(auth_router)
app.include_router(deck_router)


@app.get("/health")
def health_check():
    return {
        "status": "ok"
    }