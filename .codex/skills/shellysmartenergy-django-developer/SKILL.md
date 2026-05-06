---
name: shellysmartenergy-django-developer
description: ShellySmartEnergy Django development guidance for this repository. Use when working in the ShellySmartEnergy codebase or changing/reviewing its Django app, models, views, templates, scheduler, Shelly Cloud integration, ENTSO-E price fetching, thermostat logic, migrations, tests, Docker runtime, or local project conventions.
---

# ShellySmartEnergy Django Developer

## Workflow

Start by inspecting `git status --short` and the files touched by the request. This repository may contain generated/static assets and local data; preserve unrelated user changes and avoid touching `venv/`, `data/`, SQLite files, and collected/generated static output unless the task explicitly requires it.

Read [references/repo-structure.md](references/repo-structure.md) when the task crosses module boundaries, changes scheduler/device behavior, updates price assignment logic, or needs a full map of routes, models, and runtime flow.

Prefer the existing single-app Django structure:

- Keep project-level settings and root routes in `project/`.
- Keep domain models, admin, user-facing views, AJAX endpoints, scheduler tasks, services, utilities, templates, and static files under `app/`.
- Add migrations under `app/migrations/` for model changes.
- Reuse existing utility classes instead of adding parallel helpers.

## Core Rules

Store datetimes in UTC and display them through `TimeUtils` in the user's timezone. The default user timezone is `Europe/Helsinki`; do not hardcode Helsinki for user-facing display unless existing local behavior specifically requires it.

Treat `ElectricityPrice.price_kwh` as cents per kWh, despite the field name. ENTSO-E raw prices are converted from EUR/MWh to c/kWh with `Decimal("0.1")`. Current scheduling and assignment logic works in 15-minute periods, so `run_hours_per_day` maps to `hours * 4` slots.

Preserve user isolation. Normal users should only see/manage their own `ShellyDevice`, `ShellyTemperature`, and `DeviceAssignment` rows. Superusers can select/manage other users in the UI; staff/admin behavior is intentionally broader.

Route external Shelly Cloud calls through `ShellyService` or `ShellyTemperatureService`. Respect `shelly_rate_limiter`, `SHELLY_STOP_REST_DEBUG`, and `SecurityUtils` sanitization. Never log raw API keys, auth tokens, ENTSO-E keys, or full sensitive URLs.

Keep startup code defensive. `app/apps.py` runs database initialization and starts APScheduler from `ready()` only after checking database tables. Avoid import-time network calls, migrations-dependent assumptions, or fragile DB work that can break builds, tests, or `collectstatic`.

Use `log_device_event()` for device/system logs so sensitive content is sanitized and stored consistently in `DeviceLog`.

## Common Changes

For model or admin changes, update `app/models.py`, `app/admin.py`, forms/templates if needed, then create a migration. Check whether user ownership, admin query filtering, and timezone display need matching updates.

For price-fetching or assignment changes, work in `app/price_views.py`, `app/device_assignment_manager.py`, `app/tasks.py`, and `app/thermostat_manager.py` as appropriate. Maintain 15-minute period matching, UTC storage, local day/night transfer-price decisions, and duplicate-assignment prevention.

For Shelly control changes, keep HTTP details inside `app/services/shelly_service.py` and orchestration inside `DeviceController`. Patch/mock network calls in tests; do not rely on real Shelly Cloud or ENTSO-E calls for validation.

For user-facing UI changes, use templates in `app/templates/app/` and Bootstrap 3/jQuery conventions already present in `layout.html` and `index.html`. Keep page context names compatible with existing JavaScript data attributes unless you also update the dependent script.

For scheduler changes, update `app/scheduler.py`, `app/scheduler_config.py`, or `app/tasks.py`. Keep APScheduler jobs idempotent with `replace_existing=True`, `max_instances=1`, and SQLite-safe behavior.

## Verification

Use the project's virtualenv when available:

```bash
venv/bin/python manage.py check
venv/bin/python manage.py test app
```

After model changes, also run:

```bash
venv/bin/python manage.py makemigrations --check --dry-run app
```

If a command would require real network access, live device control, or an existing production database, mock or isolate it and state what was not exercised.
