from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker
from pydantic_settings import BaseSettings
from pydantic import Field


class Settings(BaseSettings):
    database_url: str
    jwt_secret: str
    deepseek_api_key: str
    exam_passing_score: int = 70
    apple_bundle_id: str = ""
    apple_app_id: int | None = None
    apple_environment: str = "production"
    apple_product_ids: list[str] = []
    apple_root_certificate_paths: list[str] = []
    apple_private_key_path: str = ""
    apple_key_id: str = ""
    apple_issuer_id: str = ""
    subscription_max_staleness_seconds: int = Field(default=3600, gt=0)

    class Config:
        env_file = ".env"


settings = Settings()

engine = create_engine(
    settings.database_url,
    pool_pre_ping=True,
)

SessionLocal = sessionmaker(
    bind=engine,
    autoflush=False,
    autocommit=False,
)


class Base(DeclarativeBase):
    pass


def get_db():
    db = SessionLocal()

    try:
        yield db
    finally:
        db.close()