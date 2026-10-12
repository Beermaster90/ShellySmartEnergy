from django.apps import AppConfig


class AppConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "app"

    def ready(self):
        # Ensure required settings exist at startup
        from .models import AppSetting, DeviceLog
        from django.db import connection
        from django.db.utils import OperationalError
        
        try:
            # Check if tables exist before attempting database operations
            with connection.cursor() as cursor:
                cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='app_appsetting'")
                if not cursor.fetchone():
                    # Tables don't exist yet (e.g., during Docker build), skip initialization
                    print("Database tables not found - skipping app initialization (likely during build)")
                    return
        except OperationalError:
            # Database not available, skip initialization
            print("Database not available - skipping app initialization")
            return

        try:
            # Ensure ENTSOE_API_KEY exists
            if not AppSetting.objects.filter(key="ENTSOE_API_KEY").exists():
                AppSetting.objects.create(key="ENTSOE_API_KEY", value="ABC123")

            # Ensure SHELLY_STOP_REST_DEBUG exists (0 = allow REST calls, 1 = block REST calls)
            if not AppSetting.objects.filter(key="SHELLY_STOP_REST_DEBUG").exists():
                AppSetting.objects.create(key="SHELLY_STOP_REST_DEBUG", value="0")

            # Ensure CLEAR_LOGS_ON_STARTUP exists (0 = keep logs, 1 = clear logs on startup)
            if not AppSetting.objects.filter(key="CLEAR_LOGS_ON_STARTUP").exists():
                AppSetting.objects.create(key="CLEAR_LOGS_ON_STARTUP", value="1")

            # Days of device logs kept by the hourly cleanup job (tasks.prune_old_logs)
            if not AppSetting.objects.filter(key="LOG_RETENTION_DAYS").exists():
                AppSetting.objects.create(key="LOG_RETENTION_DAYS", value="14")

            # Clear all existing logs at startup (configurable)
            try:
                clear_logs_setting = AppSetting.objects.filter(
                    key="CLEAR_LOGS_ON_STARTUP"
                ).first()
                if clear_logs_setting and clear_logs_setting.value == "1":
                    log_count = DeviceLog.objects.count()
                    DeviceLog.objects.all().delete()
                    print(f"Startup: Cleared {log_count} device logs from database")
                else:
                    print(
                        "Startup: Log clearing disabled via CLEAR_LOGS_ON_STARTUP setting"
                    )
            except Exception as e:
                print(f"Warning: Could not clear logs at startup: {e}")
        except OperationalError as e:
            print(f"Warning: Could not initialize app settings (database not ready): {e}")

        # Start the APScheduler when Django starts
        try:
            from app.scheduler import start_scheduler

            start_scheduler()
        except Exception as e:
            import logging

            logger = logging.getLogger(__name__)
            logger.error(f"Failed to start scheduler: {e}")
            # Continue without scheduler rather than crashing
