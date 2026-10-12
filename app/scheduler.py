import logging
import os
import sys
import tempfile
from pathlib import Path

from apscheduler.triggers.cron import CronTrigger
from django.conf import settings
from django.db import connection

from app.scheduler_config import get_scheduler
from app.tasks import DeviceController
from app.thermostat_manager import ThermostatAssignmentManager

try:
    import fcntl
except ImportError:  # pragma: no cover - Docker/Linux production uses fcntl.
    fcntl = None

logger = logging.getLogger(__name__)

_scheduler = None
_scheduler_lock_file = None

_SKIP_SCHEDULER_COMMANDS = {
    "check",
    "collectstatic",
    "createcachetable",
    "flush",
    "makemigrations",
    "migrate",
    "shell",
    "showmigrations",
    "test",
}


def _is_one_off_management_command() -> bool:
    command = Path(sys.argv[0]).name if sys.argv else ""
    if command == "manage.py" and len(sys.argv) > 1:
        return sys.argv[1] in _SKIP_SCHEDULER_COMMANDS
    return command == "create_test_user.py"


def _scheduler_lock_path() -> Path:
    db_name = settings.DATABASES["default"]["NAME"]
    if db_name and db_name != ":memory:":
        return Path(db_name).resolve().parent / "apscheduler.lock"
    return Path(tempfile.gettempdir()) / "shellysmartenergy-apscheduler.lock"


def _acquire_scheduler_lock() -> bool:
    global _scheduler_lock_file

    if fcntl is None:
        logger.warning("fcntl is unavailable; starting APScheduler without a process lock.")
        return True

    lock_path = _scheduler_lock_path()
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.touch(exist_ok=True)
    lock_file = lock_path.open("r+")
    try:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock_file.close()
        return False

    lock_file.seek(0)
    lock_file.write(f"pid={os.getpid()} path={lock_path}")
    lock_file.truncate()
    lock_file.flush()
    _scheduler_lock_file = lock_file
    return True


def start_scheduler():
    global _scheduler

    if _is_one_off_management_command():
        logger.info("Skipping APScheduler startup for one-off management command.")
        return

    if _scheduler and _scheduler.running:
        logger.info("APScheduler already running in this process.")
        return

    # Check if the APScheduler tables exist before starting the scheduler
    with connection.cursor() as cursor:
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='django_apscheduler_djangojob';")
        if cursor.fetchone() is None:
            logger.warning("APScheduler tables not found! Skipping scheduler startup until migrations are applied.")
            return

    if not _acquire_scheduler_lock():
        logger.info("APScheduler already running in another process; skipping startup.")
        return

    # Get the optimized scheduler configuration
    scheduler = get_scheduler()

    # Schedule the fetch electricity prices task (Every hour at HH:00 and HH:03)
    # HH:00 for regular updates, HH:03 as a backup in case the first attempt fails
    scheduler.add_job(
        DeviceController.fetch_electricity_prices,
        trigger=CronTrigger(hour="*", minute="0,5"),
        id="fetch_prices",
        max_instances=1,
        replace_existing=True,
    )

    # Schedule device control for 15-minute periods with slight offset to avoid conflicts
    # Adding 1-minute delay after the period starts to ensure price data is available
    scheduler.add_job(
        DeviceController.control_shelly_devices,
        trigger=CronTrigger(minute="15,30,45"),  # Skip minute 0 as price fetch handles it
        id="control_shelly",
        max_instances=1,
        replace_existing=True,
    )

    # EV charger control runs at the same 15-minute marks as Shelly devices
    scheduler.add_job(
        DeviceController.control_ev_chargers,
        trigger=CronTrigger(minute="15,30,45"),
        id="control_ev_chargers",
        max_instances=1,
        replace_existing=True,
    )

    # Run thermostat assignments at the top of the hour to cover the 15-past slot.
    scheduler.add_job(
        ThermostatAssignmentManager.apply_next_period_assignments,
        trigger=CronTrigger(minute="0"),
        id="thermostat_assignments",
        max_instances=1,
        replace_existing=True,
    )

    # Hourly log retention cleanup, off the busy :00/:05/:15 marks.
    scheduler.add_job(
        DeviceController.prune_old_logs,
        trigger=CronTrigger(minute="10"),
        id="prune_old_logs",
        max_instances=1,
        replace_existing=True,
    )

    logger.info("APScheduler started successfully.")
    scheduler.start()
    _scheduler = scheduler
