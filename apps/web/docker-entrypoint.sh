#!/usr/bin/env bash
#
# Startup wrapper for the webapp2 container.
#
# Vite + @cloudflare/vite-plugin runs the worker in workerd locally. workerd
# reads `vars` from wrangler.jsonc (NOT from container env), so to make the
# in-compose backend hostname reach the worker we patch wrangler.jsonc at
# container start. The host process (Vite itself) picks WEBAPP1_API_BASE up
# from the environment for its own dev proxy, so the same value drives both
# code paths.

set -euo pipefail

API_BASE="${WEBAPP1_API_BASE:-http://127.0.0.1:8080}"

# Replace just the value of "WEBAPP1_API_BASE": "..." in wrangler.jsonc.
# We deliberately avoid `sed -i` because it writes a temp file and renames
# it — which fails when wrangler.jsonc is a Docker bind mount (the rename
# would change the inode, but bind mounts target a specific one). Instead
# we read the file, transform it, then truncate-and-write the same path,
# preserving the inode.
if grep -q '"WEBAPP1_API_BASE"' wrangler.jsonc; then
  patched=$(sed 's|"WEBAPP1_API_BASE":[[:space:]]*"[^"]*"|"WEBAPP1_API_BASE": "'"$API_BASE"'"|' wrangler.jsonc)
  printf '%s\n' "$patched" > wrangler.jsonc
  echo "wrangler.jsonc → WEBAPP1_API_BASE = $API_BASE"
else
  echo "warn: WEBAPP1_API_BASE not found in wrangler.jsonc; worker will use the default"
fi

exec pnpm exec vite dev --port 3000 --host 0.0.0.0
