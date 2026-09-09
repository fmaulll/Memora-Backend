"""Real PostgreSQL locking tests. Set TEST_DATABASE_URL to a disposable database.

Each test creates and removes its own UUID-named schema, never application tables.
"""
import os
import asyncio
from unittest.mock import patch
import threading
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor

from fastapi import HTTPException
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.orm import Session
from alembic.migration import MigrationContext
from alembic.operations import Operations

from app.db.database import Base
from app.models.user import User
from app.models.generation_job import GenerationJob
from app.models.subscription import AIGenerationRequest
from app.routers.ai import generate_deck
from app.schemas.ai import GenerateDeckRequest
from test_subscriptions import generation_payload


@unittest.skipUnless(os.getenv("TEST_DATABASE_URL"), "Set TEST_DATABASE_URL for PostgreSQL concurrency tests")
class PostgreSQLSubscriptionTests(unittest.TestCase):
    def setUp(self):
        self.schema = "nudge_test_" + uuid.uuid4().hex
        self.admin = create_engine(os.environ["TEST_DATABASE_URL"])
        with self.admin.begin() as conn:
            conn.execute(text(f'CREATE SCHEMA "{self.schema}"'))
        self.engine = create_engine(os.environ["TEST_DATABASE_URL"], connect_args={"options": f"-csearch_path={self.schema}"})
        Base.metadata.create_all(self.engine)
        with Session(self.engine) as db:
            user = User(name="Guest", email="guest@example.com", password_hash="unused", is_anonymous=True)
            db.add(user)
            db.commit()
            self.user_id = user.id

    def tearDown(self):
        self.engine.dispose()
        with self.admin.begin() as conn:
            conn.execute(text(f'DROP SCHEMA "{self.schema}" CASCADE'))
        self.admin.dispose()

    def race(self, keys):
        barrier = threading.Barrier(len(keys))
        def generate(key):
            with Session(self.engine) as db:
                user = db.get(User, self.user_id)
                barrier.wait(timeout=10)
                try:
                    response = generate_deck(GenerateDeckRequest.model_validate(generation_payload()), key, db, user)
                    return 200, str(response.deck.id)
                except HTTPException as error:
                    db.rollback()
                    return error.status_code, error.detail
        with ThreadPoolExecutor(max_workers=len(keys)) as pool:
            return list(pool.map(generate, keys))

    def test_concurrent_different_keys_only_one_free_deck(self):
        results = self.race([uuid.uuid4() for _ in range(4)])
        self.assertEqual(sorted(status for status, _ in results), [200, 402, 402, 402])
        with Session(self.engine) as db:
            self.assertEqual(db.scalar(select(func.count()).select_from(GenerationJob)), 1)
            self.assertEqual(db.scalar(select(func.count()).select_from(AIGenerationRequest)), 1)
            self.assertTrue(db.get(User, self.user_id).free_ai_deck_used)

    def test_concurrent_same_key_returns_same_deck(self):
        results = self.race([uuid.uuid4()] * 4)
        self.assertEqual([status for status, _ in results], [200] * 4)
        self.assertEqual(len({deck_id for _, deck_id in results}), 1)
        with Session(self.engine) as db:
            self.assertEqual(db.scalar(select(func.count()).select_from(GenerationJob)), 1)

    def test_worker_does_not_overlap_same_job(self):
        from app.worker import GenerationWorker
        worker = GenerationWorker()
        started, release = threading.Event(), threading.Event()
        calls = []
        async def process(job_id):
            calls.append(job_id)
            started.set()
            await asyncio.to_thread(release.wait, 10)
        job_id = uuid.uuid4()
        with patch("app.worker.engine", self.engine), patch.object(worker, "_process_job", process):
            with ThreadPoolExecutor(max_workers=2) as pool:
                first = pool.submit(asyncio.run, worker.process_job(job_id))
                try:
                    self.assertTrue(started.wait(5))
                    second = pool.submit(asyncio.run, worker.process_job(job_id))
                    second.result(timeout=5)
                    self.assertEqual(calls, [job_id])
                finally:
                    release.set()
                first.result(timeout=5)

    def test_migration_backfills_identity_and_existing_usage(self):
        # Load repository migration without shadowing the installed alembic package.
        from importlib.util import spec_from_file_location, module_from_spec
        spec = spec_from_file_location("subscription_migration", "alembic/versions/e6a9b2c4d7f0_add_apple_subscriptions.py")
        migration = module_from_spec(spec)
        spec.loader.exec_module(migration)
        with self.engine.begin() as conn:
            context = MigrationContext.configure(conn)
            with Operations.context(context):
                # ORM-created constraint name differs, so emulate its migration name.
                conn.execute(text("ALTER TABLE users RENAME CONSTRAINT users_app_account_token_key TO uq_users_app_account_token"))
                migration.downgrade()
                conn.execute(text("UPDATE users SET email = 'anonymous-' || CAST(id AS text) || '@memoraapp.com'"))
                deck_id = uuid.uuid4()
                conn.execute(text("INSERT INTO decks (id,user_id,title,subject,education_level,learning_language,is_favorite,generation_status,position,created_at,updated_at) VALUES (:id,:user_id,'x','x','x','x',false,'completed',0,now(),now())"), {"id": deck_id, "user_id": self.user_id})
                conn.execute(text("INSERT INTO generation_jobs (id,parent_deck_id,plan_json,status,attempt_count,created_at,updated_at) VALUES (:id,:deck_id,'{}','completed',1,now(),now())"), {"id": uuid.uuid4(), "deck_id": deck_id})
                migration.upgrade()
        with Session(self.engine) as db:
            user = db.get(User, self.user_id)
            self.assertTrue(user.is_anonymous)
            self.assertTrue(user.free_ai_deck_used)
            self.assertEqual(user.app_account_token, user.id)
