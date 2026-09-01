# ShellySmartEnergy

Automatically runs Shelly smart-plug devices (heaters, water heaters), thermostats
and an EV charger during the cheapest electricity hours, using ENTSO-E day-ahead
spot prices at 15-minute resolution. Django app, deployed as a Docker/Podman
container, optionally published to the internet behind a Cloudflare Tunnel with
Cloudflare Access single sign-on.

---

## What it does

- Fetches ENTSO-E day-ahead prices (Finland) and, per device, books the cheapest
  15-minute periods to reach a daily run-hours target.
- **Price source fallback** — if ENTSO-E fails (e.g. its own maintenance), falls
  back to [Elering](https://dashboard.elering.ee/en/nps/price) (Estonia's TSO,
  republishes Nord Pool prices for Finland on separate infrastructure, no API
  key needed) before giving up for that cycle.
- **Block scheduling** — instead of scattering runtime across the day, books a few
  long continuous runs in the cheapest stretches, trading a fraction of a percent
  of cost for far fewer on/off cycles (configurable per device).
- **Night fallback** — if price data is unavailable (e.g. an ENTSO-E outage), still
  runs the daily target hours at night, added 15 minutes at a time.
- **Thermostat control** — min-temperature failsafe (force heat when cold),
  max-temperature cutoff, and a headroom band to stop early when warm enough.
- **EV charger** (Tuya) — schedules charging on cheap hours, with a reduced-current
  mode to protect fuses when other high-power devices run.
- Stored credentials (Shelly / Tuya / ENTSO-E keys) are **encrypted at rest**.

---

## Project structure

```
app/                     Django app
  models.py              devices, thermostats, EV charger, prices, assignments, settings
  price_views.py         ENTSO-E fetch (+ Elering fallback) + cheapest-hours / block-scheduling algorithm
  tasks.py               scheduler jobs: device control, price fetch, night fallback
  thermostat_manager.py  thermostat-driven assignment logic
  device_assignment_manager.py, device_override_manager.py
  views.py               dashboard, in-app device Settings pages, override endpoints
  forms.py               login + owner-editable device/thermostat/charger settings
  fields.py              EncryptedCharField / EncryptedTextField (at-rest encryption)
  cf_access.py           Cloudflare Access JWT verification
  middleware.py          Cloudflare Access SSO auto-login
  admin.py               Django admin (superuser-only)
  services/              Shelly + Tuya API clients
  management/commands/   backtest_scheduling, backfill_ev_session_energy
  templates/app/         dashboard, settings, login, graphs
project/settings.py      env-driven settings (DEBUG, TLS, SSO, security)
Dockerfile               multi-stage build; entrypoint runs migrations + admin bootstrap
docker-prod.sh           build & run with Docker (hardened)
podman-prod.sh           build & run rootless with Podman (hardened)
deploy-common.sh         shared deploy config, reads deploy.env
deploy.env.example       template for deploy config (copy to deploy.env)
cloudflared/             Cloudflare Tunnel config example (locally-managed tunnels)
DEPLOYMENT.md            full secure deployment: Tunnel, Access, Google SSO, hardening
```

---

## Running the app

There are two ways to run it. **You do not need Cloudflare or SSO** — that's only
for publishing it to the internet.

### A. Local / LAN, no Cloudflare, normal login (simplest)

Leave `APP_DOMAIN` and the `CF_ACCESS_*` values unset. The app then runs in local
mode: plain HTTP, the normal Django username/password login, no SSO, no forced
HTTPS redirect.

```bash
cp deploy.env.example deploy.env      # leave APP_DOMAIN and CF_ACCESS_* blank
./docker-prod.sh                      # or ./podman-prod.sh for rootless
```

- App listens on `127.0.0.1:8000` by default. To reach it from the LAN during
  local use, set `BIND_ADDRESS=0.0.0.0` in `deploy.env` (only do this on a trusted
  network — see the note in DEPLOYMENT.md about why loopback is the safe default).
- On first start a **random admin password is generated and printed once** — the
  script shows it at the end (and it's in `docker logs django-shelly-prod`). Log in,
  then change it.

### B. Internet-facing, Cloudflare Tunnel + Access SSO (recommended for remote use)

Set `APP_DOMAIN` (and the Cloudflare Access values) in `deploy.env`. The app
switches into production mode automatically: secure cookies, HSTS, HTTPS-only,
and — when the `CF_ACCESS_*` values are set — Cloudflare Access SSO so authenticated
visitors are logged straight in without a second password.

Full step-by-step (tunnel, Access application, Google/Gmail identity provider,
allow-listing emails, hardening) is in **[DEPLOYMENT.md](DEPLOYMENT.md)**.

---

## Configuration

Configuration lives in three places, by design:

| Where | What | Who edits it |
|---|---|---|
| `deploy.env` (host file, gitignored) | Deployment/security/infra: `APP_DOMAIN`, TLS, session length, HSTS, brute-force limits, Cloudflare Access `CF_ACCESS_TEAM_DOMAIN` / `CF_ACCESS_AUD` | operator, at deploy time |
| Django admin → **App Settings** (superuser) | `ENTSOE_API_KEY`, `SHELLY_STOP_REST_DEBUG`, `CLEAR_LOGS_ON_STARTUP` | superuser, in the UI |
| In-app **Settings** page / admin | Per-device settings & credentials (run hours, block scheduling, prices, Shelly/Tuya keys, thermostat, night fallback) | device owner (own devices) or superuser (all) |

Security/TLS/SSO settings are deliberately **not** editable from the web UI — see
DEPLOYMENT.md for why (a compromised admin session must not be able to weaken
authentication or transport security). Secrets (`SECRET_KEY`, the field encryption
key, the tunnel token) live in files/deploy config, never in the database.

### deploy.env keys

See `deploy.env.example` for the full annotated list. Key ones:

| Key | Default | Meaning |
|---|---|---|
| `APP_DOMAIN` | *(empty)* | Public hostname. Empty = local mode. Setting it enables production TLS mode. |
| `BIND_ADDRESS` / `BIND_PORT` | `127.0.0.1` / `8000` | Where the container listens on the host. Keep loopback behind a tunnel. |
| `DJANGO_SESSION_DAYS` | `30` | "Remember me" session lifetime. |
| `AXES_FAILURE_LIMIT` | `8` | Failed logins before lockout. |
| `CF_ACCESS_TEAM_DOMAIN` | *(empty)* | e.g. `your-team.cloudflareaccess.com`. Empty = SSO off. |
| `CF_ACCESS_AUD` | *(empty)* | Access application Audience (AUD) tag. Empty = SSO off. |

---

## Devices, thermostats, users

- **Add / edit devices** in the in-app **Settings** page (each user manages their
  own) or in the Django admin (superuser sees all).
- **Thermostat**: add a Shelly Temperature device, then link it to a Shelly device.
  Fields: min temperature (winter/summer failsafe — force heat below min−0.5°C),
  max temperature (stop above max+0.5°C), and headroom (winter/summer — stop this
  many degrees below max when already warm).
- **Users**: the Django admin (superuser) manages users, including the
  **Superuser** / **Staff** checkboxes. Under Cloudflare Access SSO, a verified
  email is matched to an existing Django user by email address.

---

## Scheduling tools

Backtest the block-scheduling algorithm against stored price history:
```bash
docker exec -it django-shelly-prod python manage.py backtest_scheduling --days 90
```

---

## Versioning

The image tag is `‹VERSION›-‹build-timestamp›`, e.g. `1.0.0-20260830-164123`.
`VERSION` is a plain file in the repo root; edit it to change the version, then
rebuild. List images with `docker images django-shelly-prod`.

---

## Local development (venv)

```bash
python3 -m venv venv && source venv/bin/activate
pip install --upgrade pip && pip install -r requirements.txt
python manage.py migrate
python manage.py runserver            # local mode, normal login
python manage.py test app             # run the test suite
```

---

## License

GNU AGPL v3. You may use, run, modify and share the software, including
commercially; you must provide source and license notices when distributing, and
if you run a modified version as a network service you must provide its source to
users of that service. Derivative works remain under AGPL v3.
