#!/bin/bash
# Update the Cloudflare Tunnel connector (cloudflared) to the newest image.
#
# The connector runs with --no-autoupdate (an immutable container shouldn't
# rewrite its own binary), so updating means: pull the new image, then recreate
# the container. A plain restart would keep running the old image.
#
# Reuses the token saved by rotate-tunnel-token.sh (data/.cloudflared_token,
# 0600), so no token is typed or shown. If the new connector fails to register,
# the previous image is put back automatically.
set -euo pipefail

CONTAINER_ENGINE="${CONTAINER_ENGINE:-docker}"   # set to 'podman' if you use Podman
CONTAINER_NAME="${CONTAINER_NAME:-cloudflared}"
DATA_DIR="${DATA_DIR:-$HOME/ShellySmartEnergy/data}"
TOKEN_FILE="$DATA_DIR/.cloudflared_token"
IMAGE="${CLOUDFLARED_IMAGE:-cloudflare/cloudflared:latest}"

if [ ! -s "$TOKEN_FILE" ]; then
  echo "No token at $TOKEN_FILE."
  echo "Run ./rotate-tunnel-token.sh once first — it saves the token there."
  exit 1
fi

# Start the connector from the given image. TUNNEL_TOKEN (env) keeps the token
# out of the container's command-line ARGS, same as rotate-tunnel-token.sh.
run_connector() {
  "$CONTAINER_ENGINE" rm -f "$CONTAINER_NAME" >/dev/null 2>&1 || true
  TUNNEL_TOKEN="$(cat "$TOKEN_FILE")" \
    "$CONTAINER_ENGINE" run -d --name "$CONTAINER_NAME" --restart unless-stopped \
    --network host \
    -e TUNNEL_TOKEN \
    "$1" tunnel --no-autoupdate run >/dev/null
}

wait_registered() {
  echo "Waiting for the connector to register..."
  for _ in $(seq 1 10); do
    sleep 3
    if "$CONTAINER_ENGINE" logs "$CONTAINER_NAME" 2>&1 | grep -q "Registered tunnel connection"; then
      return 0
    fi
  done
  return 1
}

# Remember what is running now, so we can roll back to exactly that image.
OLD_IMAGE_ID="$("$CONTAINER_ENGINE" inspect "$CONTAINER_NAME" --format '{{.Image}}' 2>/dev/null || true)"
OLD_VERSION="$("$CONTAINER_ENGINE" exec "$CONTAINER_NAME" cloudflared --version 2>/dev/null || echo 'not running')"
echo "Current: $OLD_VERSION"

echo "Pulling $IMAGE ..."
"$CONTAINER_ENGINE" pull "$IMAGE" >/dev/null
NEW_IMAGE_ID="$("$CONTAINER_ENGINE" image inspect "$IMAGE" --format '{{.Id}}')"

if [ -n "$OLD_IMAGE_ID" ] && [ "$OLD_IMAGE_ID" = "$NEW_IMAGE_ID" ]; then
  echo "Already on the newest image — nothing to do."
  exit 0
fi

echo "Recreating the connector (tunnel is down for a few seconds)..."
run_connector "$IMAGE"

if wait_registered; then
  echo "OK — now: $("$CONTAINER_ENGINE" exec "$CONTAINER_NAME" cloudflared --version)"
  # Remove the old image now that the new one works. Only that one image —
  # other projects' images are left alone.
  if [ -n "$OLD_IMAGE_ID" ]; then
    "$CONTAINER_ENGINE" rmi "$OLD_IMAGE_ID" >/dev/null 2>&1 || true
  fi
  exit 0
fi

echo "WARNING: new connector did not register."
if [ -n "$OLD_IMAGE_ID" ]; then
  echo "Rolling back to the previous image..."
  run_connector "$OLD_IMAGE_ID"
  if wait_registered; then
    echo "Rolled back — still on: $OLD_VERSION"
  else
    echo "Rollback did not register either — likely a network issue, not the update."
  fi
fi
echo "  Check: $CONTAINER_ENGINE logs $CONTAINER_NAME"
echo "  If it keeps retrying (UDP blocked), add '--protocol http2' after 'run'."
exit 1
