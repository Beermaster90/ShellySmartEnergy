#!/bin/bash
# Build and run Django test Docker container

# Create data-test directory in user's home directory
DATA_TEST_DIR="$HOME/ShellySmartEnergy/data-test"
if [ ! -d "$DATA_TEST_DIR" ]; then
  mkdir -p "$DATA_TEST_DIR"
fi
sudo chown "$(id -u)":"$(id -g)" "$DATA_TEST_DIR"
VERSION=$(cat VERSION)
BUILD_DATE=$(date +%Y%m%d-%H%M%S)
TAG="$VERSION-$BUILD_DATE"
CONTAINER_NAME="django-shelly-test"
if [ $(docker ps -a -q -f name="^/${CONTAINER_NAME}$") ]; then
  echo "Force removing existing container: $CONTAINER_NAME"
  docker rm -f $CONTAINER_NAME
fi
docker build -t django-shelly-test:$TAG .
# Run the container
docker run -d \
  -p 9000:8000 \
  --name $CONTAINER_NAME \
  --restart unless-stopped \
  --user "$(id -u):$(id -g)" \
  -v "$DATA_TEST_DIR":/data \
  django-shelly-test:$TAG
# Only this project's untagged leftovers, never other images on the host.
docker image prune -f --filter "label=org.opencontainers.image.title=shellysmartenergy"
echo "\nDatabase file location: $DATA_TEST_DIR/db.sqlite3"
echo "Django test server is running at: http://127.0.0.1:9000"
