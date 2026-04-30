"""
Background scheduler: runs daily ingest + sync at 19:00 Taipei time.
Started automatically when FastAPI launches.
"""
import logging
from datetime import date
from pathlib import Path

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from app.daily_update import run_daily_update

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).parent.parent


def daily_update() -> None:
    logger.info("=== Daily update start ===")
    result = run_daily_update(str(date.today()))
    if not result.get("success", False):
        logger.error("Daily update finished with errors")
    logger.info("=== Daily update done ===")


def startup_sync() -> None:
    """Sync cache from R2 on server startup (populates persistent volume)."""
    logger.info("[startup] Syncing cache from R2 ...")
    from app.daily_update import _run_module

    ok, output = _run_module("scripts.sync_cache", "--all")
    if output.strip():
        logger.info(output.rstrip())
    if not ok:
        logger.error("scripts.sync_cache exited with a non-zero code")
    logger.info("[startup] Cache ready.")


scheduler = BackgroundScheduler(timezone="Asia/Taipei")
scheduler.add_job(
    daily_update,
    CronTrigger(hour=19, minute=0, timezone="Asia/Taipei"),
    id="daily_update",
    replace_existing=True,
)
