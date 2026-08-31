#!/bin/bash
# Rotate the Cloudflare Tunnel connector token and restart the connector.
#
# A tunnel token is a bearer credential. If it leaks, the only real fix is to
# rotate it in Cloudflare (which invalidates the old one everywhere), then
# restart the connector with the new token. This script does the local half.
#
# The token is read from a HIDDEN prompt, so it is never echoed to the screen
# nor saved in your shell history — that's the point of running it this way
# instead of typing a `docker run ... --token eyJ...` command by hand.
set -euo pipefail

CONTAINER_ENGINE="${CONTAINER_ENGINE:-docker}"   # set to 'podman' if you use Podman
CONTAINER_NAME="${CONTAINER_NAME:-cloudflared}"
DATA_DIR="${DATA_DIR:-$HOME/ShellySmartEnergy/data}"
TOKEN_FILE="$DATA_DIR/.cloudflared_token"
IMAGE="${CLOUDFLARED_IMAGE:-cloudflare/cloudflared:latest}"

echo "=== Cloudflare Tunnel token rotation ==="
echo
echo "STEP 1 — refresh the token in Cloudflare FIRST (this kills the old one):"
echo "  Zero Trust -> Networks -> Tunnels -> your tunnel -> Configure -> Refresh token"
echo "  Then copy the NEW token (the long 'eyJ...' string) from the install command."
echo
read -rp "Have you refreshed the token in Cloudflare? [y/N] " ack
case "$ack" in
  y|Y) ;;
  *) echo "Aborted — refresh the token in the dashboard first."; exit 1 ;;
esac

# -s: hidden input, so the token is not shown and not stored in shell history.
read -rsp "STEP 2 — paste the NEW tunnel token: " NEW_TOKEN
echo
if [ -z "${NEW_TOKEN:-}" ]; then
  echo "No token entered. Aborted."
  exit 1
fi

# Persist it 0600 alongside the other secrets on the data volume (gitignored),
# so a later redeploy can reuse it without re-entry.
mkdir -p "$DATA_DIR"
( umask 077; printf '%s' "$NEW_TOKEN" > "$TOKEN_FILE" )
chmod 600 "$TOKEN_FILE"
echo "Saved to $TOKEN_FILE (0600)."

# Recreate the connector. TUNNEL_TOKEN (env) keeps the token out of the
# container's command-line ARGS; it is still visible via '$CONTAINER_ENGINE
# inspect' to anyone with host/root access, which is expected for any container.
echo "STEP 3 — restarting the connector..."
"$CONTAINER_ENGINE" rm -f "$CONTAINER_NAME" >/dev/null 2>&1 || true
"$CONTAINER_ENGINE" run -d --name "$CONTAINER_NAME" --restart unless-stopped \
  --network host \
  -e TUNNEL_TOKEN="$NEW_TOKEN" \
  "$IMAGE" tunnel --no-autoupdate run >/dev/null
unset NEW_TOKEN

echo "Waiting for the connector to register..."
sleep 12
if "$CONTAINER_ENGINE" logs "$CONTAINER_NAME" 2>&1 | grep -q "Registered tunnel connection"; then
  n=$("$CONTAINER_ENGINE" logs "$CONTAINER_NAME" 2>&1 | grep -c "Registered tunnel connection")
  echo "OK — $n tunnel connection(s) registered."
  echo "The old token is now invalid everywhere it may have leaked."
else
  echo "WARNING: no 'Registered tunnel connection' yet."
  echo "  Check: $CONTAINER_ENGINE logs $CONTAINER_NAME"
  echo "  If it keeps retrying (UDP blocked), recreate adding '--protocol http2' after 'run'."
  exit 1
fi
