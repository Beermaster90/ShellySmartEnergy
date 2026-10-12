#!/bin/bash
# Build and run the Django production container (Docker).
#
# For a hardened, rootless deployment prefer podman-prod.sh — the Docker daemon
# runs as root, so a daemon-level compromise is a host-level compromise.
set -euo pipefail

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/deploy-common.sh"

# Create data directory in user's home directory
DATA_DIR="${DATA_DIR:-$HOME/ShellySmartEnergy/data}"
if [ ! -d "$DATA_DIR" ]; then
  mkdir -p "$DATA_DIR"
fi
sudo chown "$(id -u)":"$(id -g)" "$DATA_DIR"

docker image prune -f

echo "Database file location: $DATA_DIR/db.sqlite3"
echo "Listening on:   $BIND_ADDRESS:$BIND_PORT"
echo "Reachable at:   $PUBLIC_URL"

VERSION=$(cat VERSION)
BUILD_DATE=$(date +%Y%m%d-%H%M%S)
TAG="$VERSION-$BUILD_DATE"
CONTAINER_NAME="django-shelly-prod"
SECRET_KEY_FILE="$DATA_DIR/.django_secret_key"
EXISTING_CONTAINER_ID=$(docker ps -a -q -f name="^/${CONTAINER_NAME}$")
if [ -n "$EXISTING_CONTAINER_ID" ]; then
  # Preserve the signing key used by older images before removing the container.
  # This makes the first deployment of persistent-key support retain sessions too.
  USES_PERSISTENT_SECRET=$(
    docker inspect --format '{{range .Config.Env}}{{println .}}{{end}}' "$CONTAINER_NAME" \
      | grep -c '^DJANGO_SECRET_KEY_FILE=/data/.django_secret_key$' || true
  )
  if [ "$USES_PERSISTENT_SECRET" -eq 0 ]; then
    echo "Migrating Django session signing key to persistent storage..."
    MIGRATING_SECRET_FILE="$DATA_DIR/.django_secret_key.migrating"
    docker cp "$CONTAINER_NAME:/app/.django_secret_key" "$MIGRATING_SECRET_FILE" 2>/dev/null || true
    if [ -s "$MIGRATING_SECRET_FILE" ]; then
      sudo chown "$(id -u)":"$(id -g)" "$MIGRATING_SECRET_FILE"
      chmod 600 "$MIGRATING_SECRET_FILE"
      mv "$MIGRATING_SECRET_FILE" "$SECRET_KEY_FILE"
    else
      rm -f "$MIGRATING_SECRET_FILE"
    fi
  fi
  echo "Force removing existing container: $CONTAINER_NAME"
  docker rm -f "$CONTAINER_NAME"
fi
if [ -s "$SECRET_KEY_FILE" ]; then
  sudo chown "$(id -u)":"$(id -g)" "$SECRET_KEY_FILE"
  chmod 600 "$SECRET_KEY_FILE"
fi
echo "Building with VERSION=$VERSION and BUILD_DATE=$BUILD_DATE"
docker build --build-arg VERSION=$VERSION --build-arg BUILD_DATE=$BUILD_DATE -t django-shelly-prod:$TAG .
# Run the container. The port is bound to loopback so only cloudflared (running
# on this host) can reach it — see deploy-common.sh.
docker run -d \
  -p "$BIND_ADDRESS:$BIND_PORT:8000" \
  --name "$CONTAINER_NAME" \
  --restart unless-stopped \
  --user "$(id -u):$(id -g)" \
  "${HARDENING_ARGS[@]}" \
  "${APP_ENV_ARGS[@]}" \
  -v "$DATA_DIR":/data \
  "django-shelly-prod:$TAG"

# Surface the initial admin credentials (printed once on a fresh database).
echo "Waiting for the container to initialise..."
sleep 12
CREDS=$(docker logs "$CONTAINER_NAME" 2>&1 | sed -n '/>>>>> INITIAL ADMIN CREDENTIALS/,/<<<<< END INITIAL ADMIN CREDENTIALS/p')
echo
if [ -n "$CREDS" ]; then
  echo "$CREDS"
else
  echo "Admin account already existed — password unchanged."
  echo "To reset it: docker exec -it $CONTAINER_NAME python manage.py changepassword admin"
fi

prune_old_images docker
