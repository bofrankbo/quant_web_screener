"""
Background scheduler: runs daily ingest + sync at 19:00 Taipei time.
Started automatically when FastAPI launches.
"""
import logging
import subprocess
import sys
from pathlib import Path

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).parent.parent


def _run(module: str, *args: str) -> None:
    cmd = [sys.executable, "-m", module, *args]
    result = subprocess.run(cmd, cwd=REPO_ROOT, capture_output=False)
    if result.returncode != 0:
        logger.error(f"{module} exited with code {result.returncode}")


def daily_update() -> None:
    logger.info("=== Daily update start ===")
    _run("scripts.fetch_prices")
    _run("scripts.fetch_concentration")
    _run("scripts.sync_cache", "--all")
    logger.info("=== Daily update done ===")


def startup_sync() -> None:
    """Sync cache from R2 on server startup (populates persistent volume)."""
    logger.info("[startup] Syncing cache from R2 ...")
    _run("scripts.sync_cache", "--all")
    logger.info("[startup] Cache ready.")


scheduler = BackgroundScheduler(timezone="Asia/Taipei")
scheduler.add_job(
    daily_update,
    CronTrigger(hour=19, minute=0, timezone="Asia/Taipei"),
    id="daily_update",
    replace_existing=True,
)
