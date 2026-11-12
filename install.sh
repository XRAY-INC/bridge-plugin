#!/usr/bin/env sh
# Bridge One-Command Machine Installer
# Usage:
#   curl -fsSL https://bridge.xray.tech/install.sh | sh
#   curl -fsSL https://bridge.xray.tech/install.sh | BRIDGE_API_KEY="xrkey_v1..." sh
#   curl -fsSL https://bridge.xray.tech/install.sh | sh -s -- --api-key "xrkey_v1..."

set -eu

API_KEY="${BRIDGE_API_KEY:-}"
INIT_ORG=""
INIT_REPO=""

while [ "$#" -gt 0 ]; do
  case "$1" in
    --api-key)
      API_KEY="${2:-}"
      shift 2
      ;;
    --api-key=*)
      API_KEY="${1#*=}"
      shift 1
      ;;
    --org)
      INIT_ORG="${2:-}"
      shift 2
      ;;
    --org=*)
      INIT_ORG="${1#*=}"
      shift 1
      ;;
    --repo)
      INIT_REPO="${2:-}"
      shift 2
      ;;
    --repo=*)
      INIT_REPO="${1#*=}"
      shift 1
      ;;
    *)
      shift 1
      ;;
  esac
done

printf "%s\n" "=== Bridge Machine Installer ==="

export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"

# 1. Ensure uv or pipx is available (automatically bootstrap uv if neither is installed)
if ! command -v uv >/dev/null 2>&1 && ! command -v pipx >/dev/null 2>&1; then
  printf "%s\n" "-> 'uv' not found on PATH; bootstrapping uv (https://astral.sh/uv)..."
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
fi

if command -v uv >/dev/null 2>&1; then
  printf "%s\n" "-> Installing/upgrading bridge-plugin via uv tool..."
  uv tool install --force --reinstall "git+https://github.com/XRAY-INC/bridge-plugin.git"
  UV_BIN_DIR="$(uv tool dir --bin 2>/dev/null || true)"
  if [ -n "$UV_BIN_DIR" ]; then
    export PATH="$UV_BIN_DIR:$PATH"
  fi
elif command -v pipx >/dev/null 2>&1; then
  printf "%s\n" "-> Installing/upgrading bridge-plugin via pipx..."
  pipx install --force "git+https://github.com/XRAY-INC/bridge-plugin.git"
else
  printf "%s\n" "Error: Could not locate 'uv' or 'pipx' after bootstrap." >&2
  exit 1
fi

BRG_BIN="brg"
if ! command -v brg >/dev/null 2>&1; then
  if [ -x "$HOME/.local/bin/brg" ]; then
    BRG_BIN="$HOME/.local/bin/brg"
  else
    printf "%s\n" "Error: 'brg' executable not found after installation. Add ~/.local/bin to your PATH." >&2
    exit 1
  fi
fi

# 2. If an API key is provided, authenticate first so MCP configs receive the key
if [ -n "$API_KEY" ]; then
  printf "%s\n" "-> Authenticating with Bridge..."
  "$BRG_BIN" auth login --api-key "$API_KEY"
fi

# 3. Configure global skills, MCP server, and safety hooks across Claude Code, Antigravity, Cursor, and Codex
printf "%s\n" "-> Configuring global agent harnesses & status-bar hooks..."
"$BRG_BIN" setup all --global

# 4. If inside a Git repository (or --org passed) and authenticated, initialize .bridge.json
if [ -n "$API_KEY" ] && { [ -d ".git" ] || [ -n "$INIT_ORG" ]; }; then
  printf "%s\n" "-> Binding current repository (.bridge.json)..."
  set -- init
  if [ -n "$INIT_ORG" ]; then
    set -- "$@" --org "$INIT_ORG"
  fi
  if [ -n "$INIT_REPO" ]; then
    set -- "$@" --repo "$INIT_REPO"
  fi
  "$BRG_BIN" "$@"
fi

printf "%s\n" "=== Bridge Machine Setup Complete ==="
printf "%s\n" "Verify anytime with: brg status-bar && brg auth whoami --verify"
