
#!/bin/bash
# Build and run Django production Docker container

# Create data directory in user's home directory
DATA_DIR="$HOME/ShellySmartEnergy/data"
if [ ! -d "$DATA_DIR" ]; then
  mkdir -p "$DATA_DIR"
fi
sudo chown "$(id -u)":"$(id -g)" "$DATA_DIR"

docker image prune -f

echo "Database file location: $DATA_DIR/db.sqlite3"
echo "Django production server is running at: http://127.0.0.1:8000"

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
# Run the container
docker run -d \
  -p 8000:8000 \
  --name $CONTAINER_NAME \
  --restart unless-stopped \
  --user "$(id -u):$(id -g)" \
  -e DJANGO_SECRET_KEY_FILE=/data/.django_secret_key \
  -v "$DATA_DIR":/data \
  django-shelly-prod:$TAG
