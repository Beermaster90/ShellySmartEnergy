#!/bin/bash
# Build and run the Django production container with ROOTLESS Podman.
#
# Why this over Docker: the Docker daemon runs as root, so a container escape or
# a daemon-level flaw lands an attacker as root on the host. Rootless Podman has
# no daemon at all, and the container's "root" is mapped through a user
# namespace to an unprivileged subordinate UID. An escape gets the attacker no
# more than your own unprivileged user account.
#
# One-time setup:
#   sudo apt install podman uidmap
#   loginctl enable-linger "$USER"     # keeps containers running after logout
#
# For start-on-boot, generate a systemd user unit after the first run:
#   podman generate systemd --new --name django-shelly-prod \
#     --files --restart-policy=always
#   mkdir -p ~/.config/systemd/user && mv container-*.service ~/.config/systemd/user/
#   systemctl --user daemon-reload
#   systemctl --user enable --now container-django-shelly-prod
set -euo pipefail

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/deploy-common.sh"

if ! command -v podman >/dev/null 2>&1; then
  echo "podman is not installed. Try: sudo apt install podman uidmap" >&2
  exit 1
fi

if [ "$(id -u)" -eq 0 ]; then
  echo "REFUSING TO RUN AS ROOT: the point of this script is rootless Podman." >&2
  echo "Run it as your normal user." >&2
  exit 1
fi

DATA_DIR="${DATA_DIR:-$HOME/ShellySmartEnergy/data}"
mkdir -p "$DATA_DIR"

VERSION=$(cat VERSION)
BUILD_DATE=$(date +%Y%m%d-%H%M%S)
TAG="$VERSION-$BUILD_DATE"
CONTAINER_NAME="django-shelly-prod"
SECRET_KEY_FILE="$DATA_DIR/.django_secret_key"

echo "Database file location: $DATA_DIR/db.sqlite3"
echo "Listening on:   $BIND_ADDRESS:$BIND_PORT"
echo "Reachable at:   $PUBLIC_URL"

if [ -s "$SECRET_KEY_FILE" ]; then
  chmod 600 "$SECRET_KEY_FILE"
fi

if podman container exists "$CONTAINER_NAME" 2>/dev/null; then
  echo "Removing existing container: $CONTAINER_NAME"
  podman rm -f "$CONTAINER_NAME"
fi

echo "Building with VERSION=$VERSION and BUILD_DATE=$BUILD_DATE"
podman build \
  --build-arg "VERSION=$VERSION" \
  --build-arg "BUILD_DATE=$BUILD_DATE" \
  -t "django-shelly-prod:$TAG" .

# --userns=keep-id maps your host UID to the same UID inside the container, so
# the bind-mounted /data stays readable and writable without any chown dance.
podman run -d \
  -p "$BIND_ADDRESS:$BIND_PORT:8000" \
  --name "$CONTAINER_NAME" \
  --restart unless-stopped \
  --userns=keep-id \
  --user "$(id -u):$(id -g)" \
  "${HARDENING_ARGS[@]}" \
  "${APP_ENV_ARGS[@]}" \
  -v "$DATA_DIR":/data:Z \
  "django-shelly-prod:$TAG"

echo
echo "Running rootless as UID $(id -u). Verify with:"
echo "  podman inspect $CONTAINER_NAME --format '{{.HostConfig.SecurityOpt}} {{.HostConfig.CapDrop}}'"
prune_old_images podman

# Surface the initial admin credentials (printed once on a fresh database).
echo "Waiting for the container to initialise..."
sleep 12
CREDS=$(podman logs "$CONTAINER_NAME" 2>&1 | sed -n '/>>>>> INITIAL ADMIN CREDENTIALS/,/<<<<< END INITIAL ADMIN CREDENTIALS/p')
echo
if [ -n "$CREDS" ]; then
  echo "$CREDS"
else
  echo "Admin account already existed — password unchanged."
  echo "To reset it: podman exec -it $CONTAINER_NAME python manage.py changepassword admin"
fi
