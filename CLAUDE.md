# Agent instructions for ShellySmartEnergy

## Standing rules

1. **Keep documentation in step with every change.** Whenever you change
   behaviour, configuration, deployment, or project structure, update the docs in
   the same piece of work — don't leave them for later:
   - `README.md` — features, project structure, how to run, configuration.
   - `DEPLOYMENT.md` — deployment, Cloudflare Tunnel/Access/SSO, hardening.
   - `deploy.env.example` — any new/changed env var, with a short comment.
   - Code comments — explain *why*, where it isn't obvious.
   A change isn't done until the docs describe it accurately.

2. **Security always in mind, but kept simple.** Every solution should consider
   security (auth, secrets, input, exposure, least privilege) — but favour the
   simplest design that is safe. Prefer a solution a human can read and understand
   over a clever one. If a security measure adds a lot of complexity, call out the
   trade-off and pick the simplest option that still holds. Never trade away a real
   protection for convenience without saying so explicitly.

## What that means in practice here

- This app controls physical relays (heating, water, EV charger) and stores
  Shelly/Tuya/ENTSO-E credentials. Treat a compromise as control of someone's home
  electrics, not just a web defacement.
- Secrets live in files or deploy config, never in the database or git:
  `SECRET_KEY`, the field encryption key (`/data/.field_encryption_key`), the
  Cloudflare tunnel token, `deploy.env`. These are gitignored — keep it that way.
- Security/TLS/SSO settings stay env-driven (`deploy.env`), not web-editable.
  Application keys (ENTSO-E key, device credentials) are admin/UI-editable and
  encrypted at rest.
- The app must keep working **without** Cloudflare/SSO (local mode: `APP_DOMAIN`
  and `CF_ACCESS_*` unset → normal Django login, plain HTTP). Don't make Cloudflare
  a hard dependency.
- Run `python manage.py test app` after changes; add tests for new behaviour.
