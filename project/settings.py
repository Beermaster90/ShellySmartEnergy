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
DEBUG = True

ALLOWED_HOSTS = os.getenv("ALLOWED_HOSTS", "*").split(",")

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
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

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
# Keep users logged in for extended periods
SESSION_COOKIE_AGE = 60 * 60 * 24 * 90  # 90 days in seconds (default, can be overridden)
SESSION_SAVE_EVERY_REQUEST = True  # Extend session on every request
SESSION_EXPIRE_AT_BROWSER_CLOSE = False  # Don't expire when browser closes (can be overridden)
SESSION_COOKIE_HTTPONLY = True  # Security: prevent JS access to session cookie
SESSION_COOKIE_SECURE = False  # Set to True in production with HTTPS
SESSION_COOKIE_SAMESITE = "Lax"  # CSRF protection
SESSION_ENGINE = "django.contrib.sessions.backends.db"  # Store sessions in database

# Security settings for sessions
CSRF_COOKIE_SECURE = False  # Set to True in production with HTTPS
CSRF_COOKIE_HTTPONLY = True  # Prevent JS access to CSRF cookie

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
