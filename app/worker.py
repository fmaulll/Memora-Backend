import asyncio
from datetime import datetime, timedelta

from sqlalchemy import select

from app.db.database import SessionLocal
from app.models.deck import Deck
from app.models.generation_job import GenerationJob
from app.schemas.ai import DeckPlanResponse
from app.services.deck_generation import DeckGenerationService


class GenerationWorker:

    stale_after = timedelta(minutes=10)
    poll_interval = 5

    def recover_stale_jobs(self, db):
        cutoff = datetime.utcnow() - self.stale_after
        stale_jobs = db.scalars(
            select(GenerationJob).where(
                GenerationJob.status == "running",
                GenerationJob.locked_at < cutoff,
            )
        ).all()

        for job in stale_jobs:
            job.status = "pending"
            job.locked_at = None

        if stale_jobs:
            db.commit()

    def claim_job(self):
        with SessionLocal() as db:
            self.recover_stale_jobs(db)
            job = db.scalar(
                select(GenerationJob)
                .where(GenerationJob.status == "pending")
                .order_by(GenerationJob.created_at.asc())
                .with_for_update(skip_locked=True)
            )

            if job is None:
                return None

            job.status = "running"
            job.locked_at = datetime.utcnow()
            job.attempt_count += 1
            db.commit()
            return job.id

    async def process_job(self, job_id):
        with SessionLocal() as db:
            job = db.get(GenerationJob, job_id)
            if job is None:
                return

            parent_deck_id = job.parent_deck_id
            plan_json = job.plan_json

        try:
            plan = DeckPlanResponse.model_validate(plan_json)
            await DeckGenerationService().generate_deck(
                parent_deck_id,
                plan,
            )
        except Exception as error:
            with SessionLocal() as db:
                job = db.get(GenerationJob, job_id)
                if job:
                    job.status = "failed"
                    job.last_error = str(error)[:2000]
                    job.locked_at = None
                    db.commit()
            return

        with SessionLocal() as db:
            job = db.get(GenerationJob, job_id)
            parent_deck = db.get(Deck, parent_deck_id)
            if job:
                job.status = "completed" if parent_deck and parent_deck.generation_status == "completed" else "failed"
                job.last_error = None if job.status == "completed" else "Chapter generation failed"
                job.locked_at = None
                job.completed_at = datetime.utcnow() if job.status == "completed" else None
                db.commit()

    async def run_once(self):
        job_id = self.claim_job()
        if job_id is not None:
            await self.process_job(job_id)

    async def run_forever(self):
        while True:
            await self.run_once()
            await asyncio.sleep(self.poll_interval)


if __name__ == "__main__":
    asyncio.run(GenerationWorker().run_forever())