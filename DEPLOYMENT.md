# Secure deployment

This app controls physical relays (heating, water heater, EV charger) and stores
Shelly and Tuya API credentials in its database. A compromise is not a defaced
web page — it is control of your home electrics, plus credentials that grant
that control independently of this app. Deploy it accordingly.

The intended shape:

```
browser ──HTTPS──> Cloudflare (Access: identity check)
                        │
                        │  Cloudflare Tunnel (outbound-only)
                        ▼
                   cloudflared  ──HTTP──> 127.0.0.1:8000  (rootless container)
```

No inbound port is opened. The app is never reachable from the LAN or the
internet directly — only through the tunnel, and only after Cloudflare Access
has authenticated the visitor. Django's own login is then a second layer.

---

## 1. Configure

```bash
cp deploy.env.example deploy.env
$EDITOR deploy.env          # set APP_DOMAIN=energypilot.domain.com
```

Setting `APP_DOMAIN` is what switches the app into production mode: secure
cookies, HSTS, HTTPS redirect, and the right `ALLOWED_HOSTS` and
`CSRF_TRUSTED_ORIGINS`. Leaving it empty keeps a plain local HTTP setup.

`deploy.env` is gitignored. Never commit it.

Both run scripts **refuse to start** if `deploy.env` is missing or empty —
rather than silently falling back to `ALLOWED_HOSTS=localhost,127.0.0.1`,
which looks fine until the public domain starts getting a 400 on every
request. If you deliberately want a local-only run, `deploy.env` still has to
exist; just leave `APP_DOMAIN` empty in it.

The run scripts **refuse to start** if `APP_DOMAIN` is set together with
`DJANGO_DEBUG=true`.

On a fresh database the container creates the initial `admin` superuser with a
**random one-time password**, printed once in the startup logs (the run scripts
surface it at the end). Capture it, log in, and change it. To reset later:
`docker exec -it django-shelly-prod python manage.py changepassword admin`.

## 2. Run the container

Prefer rootless Podman:

```bash
sudo apt install podman uidmap
loginctl enable-linger "$USER"      # keeps it running after you log out
./podman-prod.sh
```

Docker still works (`./docker-prod.sh`) and gets the same hardening flags, but
the Docker daemon runs as root, so a daemon-level compromise is a host-level
compromise. Rootless Podman has no daemon, and container root maps to an
unprivileged host UID.

After the new container is up, both scripts prune old images so repeated
deploys can't fill the disk. Only this project's images are touched: the newest
5 `django-shelly-prod` images are kept (for rollback), older ones are removed,
as are untagged leftovers carrying the project's image label
(`org.opencontainers.image.title=shellysmartenergy`). Other images on the host
(e.g. cloudflared, other projects) are never removed. Change `KEEP_IMAGES` in
`deploy-common.sh` to keep more or fewer.

Docker's build cache is shared by every project on the host and can't be
filtered per project, so clearing it is opt-in: set `PRUNE_BUILD_CACHE=true` in
`deploy.env` only on a host dedicated to this app. Migrations run automatically on
container start, so take a copy of `data/db.sqlite3` first when a release
changes the schema.

Both scripts apply:

| flag | effect |
|---|---|
| `-p 127.0.0.1:8000:8000` | loopback only — nothing on the LAN can reach it |
| `--cap-drop=ALL` | no Linux capabilities; nothing here needs any |
| `--security-opt=no-new-privileges` | blocks privilege escalation via setuid |
| `--read-only` | root filesystem immutable; only `/data` is writable |
| `--tmpfs /tmp` | the one writable scratch path, `noexec,nosuid` |

The loopback binding is load-bearing. Django trusts `X-Forwarded-Proto` to
decide a request arrived over HTTPS; anything able to reach the port could forge
that header. Keep `BIND_ADDRESS=127.0.0.1`.

## 3. Cloudflare Tunnel (remotely-managed — recommended)

Cloudflare now recommends **remotely-managed** tunnels: you create and configure
the tunnel in the dashboard, and the connector on your box runs from a token.
The routing (public hostname → local service) lives in the dashboard, so there
is no local `config.yml` to maintain. This is the simpler path for a single box.

1. Cloudflare **Zero Trust** dashboard (one.dash.cloudflare.com) →
   **Networks → Tunnels → Create a tunnel** → connector type **Cloudflared** →
   name it `shelly` → **Save**. (Menu labels shift occasionally; it is the
   "Tunnels" section under Networks.)
2. The dashboard shows an install command containing a long token. On the
   server, install the connector as a system service from that token:
   ```bash
   # install the cloudflared package first (see below), then:
   sudo cloudflared service install eyJhIjoiXXXX...   # the token from the dashboard
   ```
   To install the package:
   ```bash
   curl -L https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64.deb -o /tmp/cf.deb
   sudo dpkg -i /tmp/cf.deb
   ```
3. Back in the dashboard, open the tunnel → **Public Hostname** → **Add a public
   hostname**:
   - Subdomain `shelly`, Domain `domain.com`
   - Service: **HTTP** → `127.0.0.1:8000`
   The DNS record is created for you. Add **only** this one hostname — with no
   catch-all route, the tunnel can reach nothing else on the host.

That's it: no credentials file, no `config.yml`, and the token lives only in the
root-owned service config on the box.

### Alternative: locally-managed tunnel (config in a file you version-control)

Prefer this only if you want the ingress rules in git rather than the dashboard.
`cloudflared/config.example.yml` is the template for it.

```bash
cloudflared tunnel login
cloudflared tunnel create shelly
cp cloudflared/config.example.yml ~/.cloudflared/config.yml
$EDITOR ~/.cloudflared/config.yml            # tunnel UUID, username, hostname
cloudflared tunnel route dns shelly energypilot.domain.com
sudo cloudflared service install              # runs from the local config
```

Here the `http_status:404` catch-all at the end of the ingress list is what stops
the tunnel being pointed at any other service on the host — keep it.

## 4. Cloudflare Access + SSO — do not skip this

Access puts an identity check *in front of* Django, so unauthenticated traffic
never reaches the app. Once someone passes Access, the app reads the signed
identity token Cloudflare forwards and logs them straight in (SSO) — no second
password. Free for up to 50 users.

> **Optional.** If you only run locally/LAN (README option A), skip sections 3
> and 4 entirely — leave `APP_DOMAIN` and `CF_ACCESS_*` unset and use the normal
> Django login. Everything below is only for the internet-facing setup.

### 4a. Choose how people prove who they are (identity provider)

Two options:

- **One-time PIN** (simplest, no setup): Cloudflare emails a code to any
  allow-listed address. Nothing to configure — it's on by default. Good enough
  for a couple of users.
- **Google / Gmail** (nicer "Sign in with Google" button): needs a Google OAuth
  client, created in the Google Cloud Console — details below.

#### Setting up Google sign-in (Google Cloud Console)

1. Go to <https://console.cloud.google.com> → create or pick a project.
2. **APIs & Services → OAuth consent screen**:
   - User type **External**, fill app name + your support email.
   - Scopes: the defaults (`openid`, `email`, `profile`) are enough.
   - **Test users**: while the consent screen is in "Testing", *only* the Google
     accounts you add here can sign in. Add each person's Gmail address (yours,
     Kirsti's). (Or click **Publish app** to allow any Google account — but then
     your Access policy is the only gate, so keep that allow-list tight.)
3. **APIs & Services → Credentials → Create credentials → OAuth client ID**:
   - Application type **Web application**.
   - **Authorized JavaScript origins**: `https://<your-team>.cloudflareaccess.com`
   - **Authorized redirect URI**:
     `https://<your-team>.cloudflareaccess.com/cdn-cgi/access/callback`
   - Create, then copy the **Client ID** and **Client secret**.
4. In Cloudflare **Zero Trust → Settings → Authentication → Login methods → Add
   new → Google**: paste the Client ID and secret, save, and click **Test**.
   (Use the generic **Google** connector for personal Gmail/Google accounts;
   **Google Workspace** is a separate connector for a Workspace domain.)

Your team domain (`<your-team>.cloudflareaccess.com`) is shown under **Zero Trust
→ Settings** and appears in the redirect URL above.

### 4b. Create the Access application

Zero Trust → **Access → Applications → Add an application → Self-hosted**:

- **Application name**: EnergyPilot
- **Session duration**: e.g. 24h
- **Public hostname**: `energypilot.domain.com` (the tunnel hostname from §3)
- **Identity providers**: enable Google and/or One-time PIN
- Save.

### 4c. Add the policy — who is allowed in

On the application, add a policy: **Action Allow**, **Include → Emails**, and list
each person's email address. Only those addresses can pass Access. (This is
separate from the Google "test users" list in 4a — both must include the person
while Google is in testing mode.)

### 4d. Point the app at this Access application (enables SSO)

The app needs two values so it can cryptographically verify Access tokens:

- **`CF_ACCESS_TEAM_DOMAIN`** — `<your-team>.cloudflareaccess.com`
  (Zero Trust → Settings).
- **`CF_ACCESS_AUD`** — the application's **Application Audience (AUD) Tag**
  (Access → Applications → your app → **Overview**). A 64-character hex string.

Put both in `deploy.env` and redeploy. With them set, the SSO middleware activates
(it self-disables when they're blank).

### 4e. How Access identities map to app users

The app verifies the token, reads the authenticated **email**, and matches it to
an existing Django user **by email address**. So each allow-listed Access email
must equal the `email` field of a Django user:

- Set a user's email in **Django admin → Users** (superuser), matching their
  Access/Google login address.
- If a verified email has no matching user, Access lets them to the app but the
  app does not log them in (it logs a warning) — create/assign a user first.
- The `admin` superuser has no email by default, so it is reached via the normal
  password login locally, not via SSO. To administer through SSO instead, set a
  real user's email and make that user a superuser (Users → tick **Superuser**).

## 4f. Rotating the tunnel token

The tunnel token is a bearer credential — if it ever leaks (pasted into a chat,
a log, shell history), the fix is to **rotate it**, not to scrub copies. Rotating
invalidates the old token everywhere at once.

1. In the Cloudflare dashboard open **Networking → Tunnels** (also reachable via
   Zero Trust), click your tunnel → **Configure**, and use **Refresh token** on
   the right-hand side of the page (this kills the old token).
2. On the host, run `./rotate-tunnel-token.sh`. It prompts for the new token with
   **hidden input** (so it never enters your shell history) and restarts the
   connector. It stores the token 0600 at `data/.cloudflared_token` (gitignored)
   and passes it via `TUNNEL_TOKEN` so it stays out of the container's arg list.

After rotation, any leaked copy of the old token is worthless.

## 5. Verify

```bash
# Should report no issues
DJANGO_DEBUG=false DJANGO_BEHIND_TLS_PROXY=true \
  ALLOWED_HOSTS=energypilot.domain.com \
  python manage.py check --deploy

# Should refuse (400) — proves ALLOWED_HOSTS is enforced
curl -s -o /dev/null -w '%{http_code}\n' -H 'Host: evil.example' http://127.0.0.1:8000/

# Should NOT be reachable from another machine on your LAN
curl --max-time 5 http://<this-host-lan-ip>:8000/
```

Check the cookies on a real login: both `sessionid` and `csrftoken` must show
`Secure` and `HttpOnly`.

---

## What was hardened, and what is still open

Fixed:

- `DEBUG` now defaults to **off** and is env-driven. It was hardcoded `True`,
  which exposes the secret key and stored device credentials on any error page.
- **No default admin password.** `create_test_user.py` no longer seeds
  `admin`/`admin123`; on a fresh DB it generates a random one-time password and
  prints it once.
- **Credentials encrypted at rest.** Shelly/Tuya API keys and the ENTSO-E key
  are stored as Fernet ciphertext (`app/fields.py`); the key lives in
  `/data/.field_encryption_key`, never in the database. A leaked DB file no
  longer yields working keys.
- **Brute-force lockout** via `django-axes` (locks a username+IP after 8 failed
  logins).
- **Admin is superuser-only** and hidden from other users; owners manage their
  own devices through the in-app Settings page instead.
- **Removed `@csrf_exempt` from the device-control endpoints** and added
  `@login_required` + ownership checks (they were unauthenticated/forgeable).
  The frontend sends `X-CSRFToken` on every state-changing request.
- Session/CSRF cookies `Secure`+`HttpOnly`+`SameSite=Lax`; added
  `SECURE_PROXY_SSL_HEADER`, `CSRF_TRUSTED_ORIGINS`, HSTS, `SECURE_SSL_REDIRECT`,
  `X_FRAME_OPTIONS=DENY`, referrer/COOP policies.
- SSRF guard on user-editable `shelly_server` / `tuya_base_url` (public https
  hosts only).
- Session lifetime 90 → 30 days; `ALLOWED_HOSTS` no longer `*`; dependencies
  pinned and Django/urllib3/etc. updated to patched versions.

**Back up `/data` as a unit — and never separate these two files:**
`db.sqlite3` and `.field_encryption_key`. The key decrypts the credentials and
`.django_secret_key` signs sessions; losing the field key makes the stored
credentials unrecoverable. Keep them together and off any public location.

Still worth doing:

- SQLite with 3 gunicorn workers will contend under load. Not a security issue,
  but it is why `with_db_retries` exists.
- Consider key rotation for `.field_encryption_key` (re-encrypt on a new key)
  if it is ever suspected exposed.
