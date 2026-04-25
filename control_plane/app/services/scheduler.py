"""
Background scheduler for automatic translation crawls.  (v2)
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
    sched = get_scheduler()
    sched.add_job(
        _tick, "interval", minutes=1, id="crawl_tick", replace_existing=True,
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
    from ..models import TranslationConfig, Domain, CrawlFrequency
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

            langs      = [l.strip().upper() for l in cfg.languages.split(",") if l.strip()]
            extra      = [u.strip() for u in (cfg.extra_urls or "").splitlines() if u.strip()]
            routing    = cfg.routing_mode.value if cfg.routing_mode else "subdirectory"

            # Update next_crawl immediately to prevent double-run
            cfg.next_crawl = next_crawl_time(cfg.frequency.value)
            db.commit()

            asyncio.create_task(
                run_crawl(
                    config_id          = str(cfg.id),
                    domain             = domain_obj.domain,
                    domain_id          = str(domain_obj.id),
                    customer_id        = str(domain_obj.customer_id),
                    backend_url        = domain_obj.backend_url,
                    languages          = langs,
                    extra_urls         = extra,
                    deepl_api_key      = deepl_api_key,
                    db_session_factory = db_session_factory,
                    routing_mode       = routing,
                )
            )

    except Exception as exc:
        logger.exception("Scheduler tick error: %s", exc)
    finally:
        db.close()
