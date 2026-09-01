#!/bin/bash
# Shared deployment configuration for docker-prod.sh and podman-prod.sh.
# Sourced, not executed.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="${ENV_FILE:-$SCRIPT_DIR/deploy.env}"

# Require deploy.env to exist (even just to explicitly leave APP_DOMAIN empty
# for a local run) rather than silently falling back to defaults. A missing
# file previously meant ALLOWED_HOSTS silently reverted to localhost-only,
# which looks fine right up until it 400s every request on the real domain.
if [ ! -f "$ENV_FILE" ]; then
  echo "ERROR: $ENV_FILE not found — refusing to deploy." >&2
  echo "Create it first:" >&2
  echo "  cp deploy.env.example deploy.env" >&2
  echo "  \$EDITOR deploy.env   # set APP_DOMAIN, or leave it empty for local-only" >&2
  exit 1
fi
if [ ! -s "$ENV_FILE" ]; then
  echo "ERROR: $ENV_FILE exists but is empty — refusing to deploy." >&2
  echo "Fill it in (see deploy.env.example) before deploying." >&2
  exit 1
fi

echo "Loading deployment config from $ENV_FILE"
set -a
# shellcheck disable=SC1090
. "$ENV_FILE"
set +a

APP_DOMAIN="${APP_DOMAIN:-}"
BIND_ADDRESS="${BIND_ADDRESS:-127.0.0.1}"
BIND_PORT="${BIND_PORT:-8000}"
DJANGO_DEBUG="${DJANGO_DEBUG:-false}"
DJANGO_SESSION_DAYS="${DJANGO_SESSION_DAYS:-30}"
DJANGO_HSTS_SECONDS="${DJANGO_HSTS_SECONDS:-3600}"
DJANGO_HSTS_INCLUDE_SUBDOMAINS="${DJANGO_HSTS_INCLUDE_SUBDOMAINS:-false}"
DJANGO_HSTS_PRELOAD="${DJANGO_HSTS_PRELOAD:-false}"

# Setting APP_DOMAIN is what turns on production TLS behaviour. Without it the
# app stays in local mode, because secure cookies over plain HTTP would simply
# never be sent and would lock you out.
if [ -n "$APP_DOMAIN" ]; then
  ALLOWED_HOSTS="$APP_DOMAIN,localhost,127.0.0.1"
  CSRF_TRUSTED_ORIGINS="https://$APP_DOMAIN"
  DJANGO_BEHIND_TLS_PROXY="true"
  PUBLIC_URL="https://$APP_DOMAIN"
else
  ALLOWED_HOSTS="localhost,127.0.0.1"
  CSRF_TRUSTED_ORIGINS=""
  DJANGO_BEHIND_TLS_PROXY="false"
  PUBLIC_URL="http://$BIND_ADDRESS:$BIND_PORT"
fi

# Refuse the two combinations that would put a debug console or a default
# superuser on a public hostname.
if [ -n "$APP_DOMAIN" ]; then
  if [ "$DJANGO_DEBUG" = "true" ]; then
    echo "REFUSING TO START: DJANGO_DEBUG=true with a public APP_DOMAIN." >&2
    echo "Debug pages leak the secret key and stored device credentials." >&2
    exit 1
  fi
fi

if [ "$BIND_ADDRESS" != "127.0.0.1" ] && [ "$BIND_ADDRESS" != "localhost" ]; then
  echo "WARNING: binding to $BIND_ADDRESS exposes the app beyond loopback." >&2
  echo "         Django trusts X-Forwarded-Proto, which anything reaching it can forge." >&2
fi

# Environment passed into the container by both run scripts.
APP_ENV_ARGS=(
  -e "DJANGO_SECRET_KEY_FILE=/data/.django_secret_key"
  -e "DJANGO_DEBUG=$DJANGO_DEBUG"
  -e "ALLOWED_HOSTS=$ALLOWED_HOSTS"
  -e "CSRF_TRUSTED_ORIGINS=$CSRF_TRUSTED_ORIGINS"
  -e "DJANGO_BEHIND_TLS_PROXY=$DJANGO_BEHIND_TLS_PROXY"
  -e "DJANGO_SESSION_DAYS=$DJANGO_SESSION_DAYS"
  -e "DJANGO_HSTS_SECONDS=$DJANGO_HSTS_SECONDS"
  -e "DJANGO_HSTS_INCLUDE_SUBDOMAINS=$DJANGO_HSTS_INCLUDE_SUBDOMAINS"
  -e "DJANGO_HSTS_PRELOAD=$DJANGO_HSTS_PRELOAD"
  -e "AXES_FAILURE_LIMIT=${AXES_FAILURE_LIMIT:-8}"
  -e "AXES_COOLOFF_HOURS=${AXES_COOLOFF_HOURS:-1}"
  -e "AXES_PROXY_COUNT=${AXES_PROXY_COUNT:-1}"
  -e "CF_ACCESS_TEAM_DOMAIN=${CF_ACCESS_TEAM_DOMAIN:-}"
  -e "CF_ACCESS_AUD=${CF_ACCESS_AUD:-}"
)

# Runtime hardening applied to both engines:
#   cap-drop=ALL           nothing in this image needs a Linux capability
#   no-new-privileges      a compromised process cannot gain privileges via setuid
#   read-only rootfs       the app only ever writes to /data
#   tmpfs /tmp             the one writable path the runtime still needs
HARDENING_ARGS=(
  --cap-drop=ALL
  --security-opt=no-new-privileges
  --read-only
  --tmpfs /tmp:rw,noexec,nosuid,size=64m
)
