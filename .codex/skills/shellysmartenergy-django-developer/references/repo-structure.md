# ShellySmartEnergy Repo Structure

## Project Map

- `manage.py`: Django entrypoint.
- `project/settings.py`: settings, SQLite path selection, static files, session settings, login redirects, APScheduler settings.
- `project/urls.py`: root routes for home/contact/about/graphs/login/logout/admin and `include("app.urls")` at `/shellyapp/`.
- `project/wsgi.py`: WSGI entrypoint for Gunicorn.
- `app/models.py`: domain models and the `User` post-save signal.
- `app/admin.py`: admin UI, user-scoped querysets, timezone-aware display, extended `UserAdmin`.
- `app/views.py`: page views, shared dashboard context, login behavior, admin test page, AJAX assignment/status toggles.
- `app/price_views.py`: ENTSO-E price fetching, parsing, 15-minute resampling, cheapest-period assignment.
- `app/shelly_views.py`: AJAX status/toggle endpoints for Shelly devices.
- `app/graph_views.py`: cost and temperature graphs.
- `app/tasks.py`: scheduled orchestration through `DeviceController`.
- `app/scheduler.py` and `app/scheduler_config.py`: APScheduler setup with Django job store.
- `app/services/shelly_service.py`: Shelly Cloud HTTP wrappers and temperature extraction.
- `app/thermostat_manager.py`: thermostat-driven next-period assignment/unassignment.
- `app/device_assignment_manager.py`: assignment creation and next-24-hour lookup helpers.
- `app/utils/`: shared time, database retry, security, and rate-limiting utilities.
- `app/templates/app/`: Bootstrap 3 templates and inline page JavaScript.
- `app/static/app/`: committed Bootstrap/jQuery/static assets used by templates.
- `Dockerfile`, `docker-compose.yml`, `docker-prod.sh`, `docker-test.sh`: container runtime and setup.
- `requirements.txt`: unpinned Django/runtime dependencies except selected packages such as pandas/numpy.

## Domain Model Summary

- `AppSetting`: key/value settings such as `ENTSOE_API_KEY`, `SHELLY_STOP_REST_DEBUG`, and `CLEAR_LOGS_ON_STARTUP`.
- `ShellyDevice`: user-owned controllable relay device. Includes API credentials, server URL, relay channel, automation status, run hours, transfer prices, optional thermostat, and auto-assign threshold.
- `ShellyTemperature`: user-owned temperature sensor/thermostat source with thresholds, current temperature, and update timestamp.
- `ElectricityPrice`: UTC price period with `price_kwh` stored in c/kWh. Current fetch logic creates 15-minute periods.
- `TemperatureReading`: historical thermostat readings.
- `DeviceLog`: sanitized system/device logs.
- `DeviceAssignment`: user/device/price-period assignment.
- `UserProfile`: timezone preference. A `post_save` signal creates profiles, assigns the `commoneers` group, marks new users staff, and creates a demo device.

## Runtime Flow

1. Django starts and `app.apps.AppConfig.ready()` ensures required `AppSetting` rows, optionally clears `DeviceLog`, then starts APScheduler if scheduler tables exist.
2. `scheduler.start_scheduler()` schedules price fetches at every hour minute `0,5`, device control at `15,30,45`, and thermostat assignment jobs at minute `0`.
3. `DeviceController.fetch_electricity_prices()` calls `call_fetch_prices(None)`.
4. `call_fetch_prices()` reads `ENTSOE_API_KEY`, fetches Finland day-ahead prices, parses the preferred resolution, resamples to 15 minutes, stores UTC periods, calls `set_cheapest_hours()` when new rows are added, and then runs device control.
5. `set_cheapest_hours()` finds future prices, computes cheapest 15-minute slots with transfer costs and optional threshold, and records `DeviceAssignment` rows through `DeviceAssignmentManager`.
6. `DeviceController.control_shelly_devices()` finds the active 15-minute price period, groups enabled devices by server/token, checks current Shelly state once per device, and toggles only when current state differs from assignment.
7. Temperature devices are fetched after control. `ThermostatAssignmentManager.apply_next_period_assignments()` assigns or unassigns the next period using min/max thresholds with 0.5 C hysteresis.

## URLs And Views

- `/` and `/shellyapp/`: dashboard from `views.index`, requiring login.
- `/graphs/` and `/shellyapp/graphs/`: graph page from `graph_views.graphs`, requiring login.
- `/shellyapp/get-graph-data/`: AJAX graph data.
- `/shellyapp/fetch-device-status/`: AJAX Shelly status.
- `/shellyapp/toggle-device-output/`: AJAX Shelly relay control.
- `/shellyapp/fetch-prices/`: ENTSO-E fetch trigger.
- `/shellyapp/toggle-assignment/`: POST JSON assignment toggle.
- `/shellyapp/toggle-device-status/`: POST JSON automation status toggle.
- `/admin-test/` and `/shellyapp/admin-test/`: staff-only manual backend triggers.

## Time And Price Rules

- Use `TimeUtils.now_utc()` for current time and `TimeUtils.to_utc()` for normalization.
- Use `TimeUtils.to_user_timezone()` or `format_datetime_with_tz()` before displaying times.
- Match price periods at minute precision when comparing datetimes.
- Day transfer price applies from 07:00 through 21:59 local time; night applies from 22:00 through 06:59.
- Apply VAT only where the existing UI/graph/admin display expects it. Stored electricity prices are base c/kWh.

## Safety And Testing Notes

- Network calls to Shelly Cloud and ENTSO-E must be mocked in tests.
- The global scheduler can start from `AppConfig.ready()` when DB tables exist; tests touching startup behavior may need to patch scheduler startup or isolate the database.
- Existing `app/tests.py` is legacy and minimal. Add focused tests near the behavior you change, especially for assignment math, timezone conversion, permission filtering, and JSON endpoint validation.
- `create_test_user.py` is used by Docker startup to ensure a default admin exists. Avoid changing default credentials or group behavior unless requested.
- `compare_users.py` appears diagnostic and references an old `price_per_kwh` attribute; do not use it as authoritative without fixing it first.
