#!/usr/bin/env bash
#
# keryx-stream installer — links this plugin into your Hermes plugins dir.
#
# Unlike the old in-tree patch this replaces, it touches NO hermes-agent core
# files: it only symlinks the plugin package into ~/.hermes/plugins/ so Hermes
# discovers it, then prints the config + token you still need to set.
#
# Usage:
#   ./install.sh            # symlink (recommended — `git pull` updates the live plugin)
#   ./install.sh --copy     # copy instead of symlink
#   HERMES_HOME=/path ./install.sh
#
set -euo pipefail

HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
PLUGINS_DIR="$HERMES_HOME/plugins"
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/keryx_stream"
DEST="$PLUGINS_DIR/keryx_stream"

if [[ ! -f "$SRC/plugin.yaml" || ! -f "$SRC/__init__.py" ]]; then
  echo "✗ can't find the plugin package at $SRC — run this from the repo root." >&2
  exit 1
fi

mkdir -p "$PLUGINS_DIR"
rm -rf "$DEST"

if [[ "${1:-}" == "--copy" ]]; then
  cp -r "$SRC" "$DEST"
  echo "✓ copied plugin to $DEST"
else
  ln -sfn "$SRC" "$DEST"
  echo "✓ linked $DEST -> $SRC"
fi

CONFIG="$HERMES_HOME/config.yaml"
echo
if [[ -f "$CONFIG" ]] && grep -q '^keryx_stream:' "$CONFIG"; then
  echo "✓ config.yaml already has a keryx_stream: block"
else
  echo "→ Add this to $CONFIG (non-secret settings):"
  cat <<'YAML'

    keryx_stream:
      enabled: true
      host: "0.0.0.0"
      port: 8646
      default_platform: matrix
      toolsets:
        locked: []
        forbidden: []
YAML
fi

echo
ENV_FILE="$HERMES_HOME/.env"
if [[ -n "${KERYX_STREAM_TOKEN:-}" || -n "${API_SERVER_KEY:-}" ]] ||
   { [[ -f "$ENV_FILE" ]] && grep -Eq '^(KERYX_STREAM_TOKEN|API_SERVER_KEY)=.+' "$ENV_FILE"; }; then
  echo "✓ bearer token found (KERYX_STREAM_TOKEN or API_SERVER_KEY)"
else
  echo "→ Set a bearer token (secret) in $ENV_FILE: KERYX_STREAM_TOKEN=... (or reuse API_SERVER_KEY)"
  echo "  Without one the plugin refuses every request."
fi

echo
echo "Next:"
echo "  1. hermes plugins enable keryx-stream     # user plugins load only once enabled"
echo "  2. hermes gateway restart"
echo "  3. curl -s localhost:8646/keryx/health     # version + feature list"
echo "  4. Keryx → Settings → Gateways → Hermes Link: http://<this-host>:8646 + your key → Test link"
echo
echo "Needs a hermes-agent with the shipped stream observer hooks (on_stream_start/"
echo "delta/end). After a Hermes update, \`hermes plugins compat\` reports retired imports."
