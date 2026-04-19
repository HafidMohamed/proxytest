"""
Background scheduler for automatic translation crawls.

Uses APScheduler (AsyncIOScheduler) so it runs inside the same uvicorn
event loop — no separate process needed.

Job logic:
  - Every minute: check TranslationConfig rows where next_crawl <= now
  - For each due config: run the full crawl+translate cycle
  - Update next_crawl after completion
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from apscheduler.schedulers.asyncio import AsyncIOScheduler

logger = logging.getLogger("scheduler")

_scheduler: AsyncIOScheduler | None = None


def get_scheduler() -> AsyncIOScheduler:
    global _scheduler
    if _scheduler is None:
        _scheduler = AsyncIOScheduler(timezone="UTC")
    return _scheduler


def start_scheduler(db_session_factory, deepl_api_key: str) -> None:
    """Start the scheduler. Call once at FastAPI startup."""
    sched = get_scheduler()
    sched.add_job(
        _tick,
        "interval",
        minutes=1,
        id="crawl_tick",
        replace_existing=True,
        kwargs={"db_session_factory": db_session_factory, "deepl_api_key": deepl_api_key},
    )
    sched.start()
    logger.info("Scheduler started — checking for due crawls every 60s")


def stop_scheduler() -> None:
    sched = get_scheduler()
    if sched.running:
        sched.shutdown(wait=False)
        logger.info("Scheduler stopped")


async def _tick(db_session_factory, deepl_api_key: str) -> None:
    """Called every minute. Finds due configs and runs their crawl."""
    from .models import TranslationConfig, Domain, CrawlFrequency
    from .seo_crawler import run_crawl, next_crawl_time

    db = db_session_factory()
    try:
        now = datetime.now(timezone.utc)
        due = (
            db.query(TranslationConfig)
            .filter(
                TranslationConfig.next_crawl <= now,
                TranslationConfig.frequency != CrawlFrequency.MANUAL,
            )
            .all()
        )
        if not due:
            return

        logger.info("Scheduler tick: %d config(s) due", len(due))

        for cfg in due:
            domain_obj: Domain = cfg.domain
            if not domain_obj or domain_obj.status.value != "active":
                continue

            langs = [l.strip().upper() for l in cfg.languages.split(",") if l.strip()]
            extra = [u.strip() for u in (cfg.extra_urls or "").splitlines() if u.strip()]

            # Set next_crawl immediately so concurrent ticks don't double-run
            cfg.next_crawl = next_crawl_time(cfg.frequency.value)
            db.commit()

            # Run crawl in background (don't block the tick)
            asyncio.create_task(
                run_crawl(
                    config_id=str(cfg.id),
                    domain=domain_obj.domain,
                    backend_url=domain_obj.backend_url,
                    languages=langs,
                    extra_urls=extra,
                    deepl_api_key=deepl_api_key,
                    db_session_factory=db_session_factory,
                )
            )

    except Exception as exc:
        logger.exception("Scheduler tick error: %s", exc)
    finally:
        db.close()
