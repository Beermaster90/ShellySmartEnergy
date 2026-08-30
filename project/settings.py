"""
Django settings for ShellySmartEnergy project.

Based on 'django-admin startproject' using Django 2.1.2.

For more information on this file, see
https://docs.djangoproject.com/en/2.1/topics/settings/

For the full list of settings and their values, see
https://docs.djangoproject.com/en/2.1/ref/settings/
"""

import os
import posixpath
import secrets
from pathlib import Path

# Build paths inside the project like this: os.path.join(BASE_DIR, ...)
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _env_bool(name: str, default: bool = False) -> bool:
    """Read a boolean from the environment, defaulting when unset or blank."""
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_list(name: str, default: str = "") -> list[str]:
    """Read a comma-separated list from the environment, dropping blanks."""
    return [item.strip() for item in os.getenv(name, default).split(",") if item.strip()]

# Quick-start development settings - unsuitable for production
# See https://docs.djangoproject.com/en/2.1/howto/deployment/checklist/

def _get_secret_key(base_dir: str) -> str:
    env_key = os.environ.get("DJANGO_SECRET_KEY")
    if env_key:
        return env_key

    configured_key_file = os.environ.get("DJANGO_SECRET_KEY_FILE")
    if configured_key_file:
        key_path = Path(configured_key_file).expanduser()
    elif Path("/data").exists():
        # docker-prod.sh mounts its persistent host data directory here. Keeping
        # the signing key beside the session database prevents all sessions from
        # becoming invalid whenever the application image is rebuilt.
        key_path = Path("/data/.django_secret_key")
    else:
        data_dir = Path(base_dir) / "data"
        key_dir = data_dir if data_dir.exists() else Path(base_dir)
        key_path = key_dir / ".django_secret_key"

    if key_path.exists():
        key = key_path.read_text().strip()
        if not key:
            raise RuntimeError(f"Django secret key file is empty: {key_path}")
        os.environ["DJANGO_SECRET_KEY"] = key
        return key

    # Generate once and persist it. In production this path is on /data.
    key_path.parent.mkdir(parents=True, exist_ok=True)
    new_key = secrets.token_urlsafe(50)
    key_path.write_text(new_key)
    key_path.chmod(0o600)
    os.environ["DJANGO_SECRET_KEY"] = new_key
    return new_key


# SECURITY WARNING: keep the secret key used in production secret!
SECRET_KEY = _get_secret_key(BASE_DIR)

# SECURITY WARNING: don't run with debug turned on in production!
# Off unless explicitly switched on, so a missing variable fails safe. With DEBUG
# on, any unhandled error renders a traceback page exposing settings, the secret
# key and stored device API credentials.
DEBUG = _env_bool("DJANGO_DEBUG", False)

# Requests whose Host header is not listed get a 400. Set ALLOWED_HOSTS to the
# tunnel hostname in production; the default only permits local access.
ALLOWED_HOSTS = _env_list("ALLOWED_HOSTS", "localhost,127.0.0.1")

# ---------------------------------------------------------------------------
# TLS / proxy
#
# In production the app sits behind a Cloudflare Tunnel: cloudflared terminates
# TLS and forwards plain HTTP to 127.0.0.1, setting X-Forwarded-Proto. Trusting
# that header is only safe because the container publishes to loopback alone, so
# nothing on the LAN can reach the app to forge it. Keep it that way — see
# docker-prod.sh / podman-prod.sh.
# ---------------------------------------------------------------------------
_LOCAL_ONLY_HOSTS = {"localhost", "127.0.0.1", "[::1]", "testserver"}

# Default on only once a real hostname is configured. Enabling secure cookies and
# HTTPS redirects for a plain-HTTP localhost run would lock you out of your own
# dev server, so "no domain configured" means "not behind TLS". The deploy
# scripts set this explicitly either way.
BEHIND_TLS_PROXY = _env_bool(
    "DJANGO_BEHIND_TLS_PROXY",
    not DEBUG and bool(set(ALLOWED_HOSTS) - _LOCAL_ONLY_HOSTS),
)

# Django 4+ requires the full origin for cross-origin POSTs (the admin included).
CSRF_TRUSTED_ORIGINS = _env_list("CSRF_TRUSTED_ORIGINS")

if BEHIND_TLS_PROXY:
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
    SECURE_SSL_REDIRECT = True
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    # Start low (e.g. 3600) while verifying the deployment; a long max-age is
    # cached by browsers and cannot be taken back quickly.
    SECURE_HSTS_SECONDS = int(os.getenv("DJANGO_HSTS_SECONDS", str(60 * 60 * 24 * 365)))
    SECURE_HSTS_INCLUDE_SUBDOMAINS = _env_bool("DJANGO_HSTS_INCLUDE_SUBDOMAINS", False)
    SECURE_HSTS_PRELOAD = _env_bool("DJANGO_HSTS_PRELOAD", False)
else:
    SECURE_SSL_REDIRECT = False
    SESSION_COOKIE_SECURE = False
    CSRF_COOKIE_SECURE = False
    SECURE_HSTS_SECONDS = 0

SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = "same-origin"
SECURE_CROSS_ORIGIN_OPENER_POLICY = "same-origin"
X_FRAME_OPTIONS = "DENY"

# Application references
# https://docs.djangoproject.com/en/2.1/ref/settings/#std:setting-INSTALLED_APPS
INSTALLED_APPS = [
    "app",
    # Add your apps here to enable them
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django_apscheduler",
    "axes",  # login rate limiting / brute-force lockout
]
# Middleware framework
# https://docs.djangoproject.com/en/2.1/topics/http/middleware/
MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    # Turns a verified Cloudflare Access token into a Django session. Self-disables
    # (MiddlewareNotUsed) unless CF_ACCESS_TEAM_DOMAIN and CF_ACCESS_AUD are set.
    "app.middleware.CloudflareAccessMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    # Must be last so it wraps the fully-resolved request/response.
    "axes.middleware.AxesMiddleware",
]

# ---------------------------------------------------------------------------
# Cloudflare Access single sign-on
#
# When set, a request carrying a valid Access JWT is logged in automatically,
# matched to an existing user by email (see app/middleware.py). Leave both blank
# to keep the normal username/password login (local dev, or no Access in front).
#   CF_ACCESS_TEAM_DOMAIN  e.g. energypilot-team.cloudflareaccess.com
#   CF_ACCESS_AUD          the Access application's Audience (AUD) tag
# ---------------------------------------------------------------------------
CF_ACCESS_TEAM_DOMAIN = os.getenv("CF_ACCESS_TEAM_DOMAIN", "")
CF_ACCESS_AUD = os.getenv("CF_ACCESS_AUD", "")

# Authentication backends — AxesStandaloneBackend must come first so failed
# logins are counted and locked-out users are rejected before the real check.
AUTHENTICATION_BACKENDS = [
    "axes.backends.AxesStandaloneBackend",
    "django.contrib.auth.backends.ModelBackend",
]

# ---------------------------------------------------------------------------
# Brute-force protection (django-axes)
# ---------------------------------------------------------------------------
# Lock after N failed attempts, keyed on username + client IP so one attacker
# cannot lock out a legitimate user from a different address.
AXES_FAILURE_LIMIT = int(os.getenv("AXES_FAILURE_LIMIT", "8"))
AXES_COOLOFF_TIME = float(os.getenv("AXES_COOLOFF_HOURS", "1"))  # hours
AXES_LOCKOUT_PARAMETERS = [["username", "ip_address"]]
AXES_RESET_ON_SUCCESS = True
AXES_ENABLE_ADMIN = True
# Behind Cloudflare Tunnel the real client IP is the last hop in
# X-Forwarded-For. django-ipware reads it; the tunnel is the only thing that can
# set this header because the app binds to loopback only.
AXES_IPWARE_PROXY_COUNT = int(os.getenv("AXES_PROXY_COUNT", "1"))
AXES_IPWARE_META_PRECEDENCE_ORDER = ["HTTP_X_FORWARDED_FOR", "REMOTE_ADDR"]

ROOT_URLCONF = "project.urls"

# Template configuration
# https://docs.djangoproject.com/en/2.1/topics/templates/
TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "project.wsgi.application"
# Database
# https://docs.djangoproject.com/en/2.1/ref/settings/#databases


def _sqlite_path(base_dir: Path) -> str:
    """
    Resolve a reliable SQLite path across Linux/Docker and Windows dev.

    Order of precedence:
      1) DJANGO_SQLITE_PATH env var (expanded; directory auto-created)
      2) /data/db.sqlite3 if /data exists (container/Linux)
      3) <BASE_DIR>/db.sqlite3 as a safe local default (Windows/dev)
    """
    # 1) Explicit override via env
    env_val = os.environ.get("DJANGO_SQLITE_PATH")
    if env_val:
        p = Path(env_val).expanduser()
        p.parent.mkdir(parents=True, exist_ok=True)
        return str(p)

    # 2) Container/Linux convention if the mount point actually exists
    data_mount = Path("/data")
    if data_mount.exists():
        p = data_mount / "db.sqlite3"
        p.parent.mkdir(parents=True, exist_ok=True)
        return str(p)

    # 3) Windows/dev fallback next to the project
    p = Path(base_dir) / "db.sqlite3"
    p.parent.mkdir(parents=True, exist_ok=True)
    return str(p)


DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": _sqlite_path(BASE_DIR),
    }
}

# Password validation
# https://docs.djangoproject.com/en/2.1/ref/settings/#auth-password-validators
AUTH_PASSWORD_VALIDATORS = [
    {
        "NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.CommonPasswordValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.NumericPasswordValidator",
    },
]

# Internationalization
# https://docs.djangoproject.com/en/2.1/topics/i18n/
LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_TZ = True  # Set to False if you don't want Django to use timezone-aware datetimes
USE_I18N = True
USE_L10N = True

# Static files (CSS, JavaScript, Images)
# https://docs.djangoproject.com/en/2.1/howto/static-files/
STATIC_URL = "/static/"
STATIC_ROOT = os.path.join(BASE_DIR, "staticfiles")

# Use regular static files storage for development
# STATICFILES_STORAGE = "whitenoise.storage.CompressedManifestStaticFilesStorage"

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# Session Configuration
# How long a "remember me" login lasts. A stolen session cookie is valid for
# this long, so it is a direct security/convenience trade-off — shorter is safer.
SESSION_COOKIE_DAYS = int(os.getenv("DJANGO_SESSION_DAYS", "30"))
SESSION_COOKIE_AGE = 60 * 60 * 24 * SESSION_COOKIE_DAYS
SESSION_SAVE_EVERY_REQUEST = True  # Extend session on every request
SESSION_EXPIRE_AT_BROWSER_CLOSE = False  # Don't expire when browser closes (can be overridden)
SESSION_COOKIE_HTTPONLY = True  # Security: prevent JS access to session cookie
SESSION_COOKIE_SAMESITE = "Lax"  # CSRF protection
SESSION_ENGINE = "django.contrib.sessions.backends.db"  # Store sessions in database

# Security settings for sessions
CSRF_COOKIE_HTTPONLY = True  # Prevent JS access to CSRF cookie
CSRF_COOKIE_SAMESITE = "Lax"
# SESSION_COOKIE_SECURE and CSRF_COOKIE_SECURE are set in the TLS block above.

# Login/Logout redirect URLs
LOGIN_URL = "/login/"
LOGIN_REDIRECT_URL = "/shellyapp/"
LOGOUT_REDIRECT_URL = "/login/"

# APScheduler Settings
APSCHEDULER_DATETIME_FORMAT = "N j, Y, f:s a"  # More readable format for dates
DJANGO_APSCHEDULER_CLEANUP_INTERVAL = 60  # Clean up interval in seconds
APSCHEDULER_RUN_NOW_TIMEOUT = 25  # Shorter timeout for immediate jobs
APSCHEDULER_CONNECTION_OPTIONS = {
    'isolation_level': None  # Disable transaction isolation for APScheduler
}
