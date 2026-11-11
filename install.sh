#!/usr/bin/env sh
# Bridge One-Command Machine Installer
# Usage:
#   curl -fsSL https://bridge.xray.tech/install.sh | sh
#   BRIDGE_API_KEY="xrkey_v1..." curl -fsSL https://bridge.xray.tech/install.sh | sh

set -eu

printf "=== Bridge Machine Installer ===\n"

export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"

# 1. Ensure uv or pipx is available (automatically bootstrap uv if neither is installed)
if ! command -v uv >/dev/null 2>&1 && ! command -v pipx >/dev/null 2>&1; then
  printf "-> 'uv' not found on PATH; bootstrapping uv (https://astral.sh/uv)...\n"
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
fi

if command -v uv >/dev/null 2>&1; then
  printf "-> Installing/upgrading bridge-plugin via uv tool...\n"
  uv tool install --force --reinstall "git+https://github.com/XRAY-INC/bridge-plugin.git"
elif command -v pipx >/dev/null 2>&1; then
  printf "-> Installing/upgrading bridge-plugin via pipx...\n"
  pipx install --force "git+https://github.com/XRAY-INC/bridge-plugin.git"
else
  printf "Error: Could not locate 'uv' or 'pipx' after bootstrap.\n" >&2
  exit 1
fi

# 2. If BRIDGE_API_KEY is provided, authenticate first so MCP configs receive the key
if [ -n "${BRIDGE_API_KEY:-}" ]; then
  printf "-> Authenticating with Bridge...\n"
  brg auth login --api-key "$BRIDGE_API_KEY"
fi

# 3. Run global machine setup across Claude, Antigravity, Cursor, and Codex
printf "-> Configuring global agent harnesses & status-bar hooks...\n"
brg setup all --global

# 4. If inside a Git repository and authenticated, initialize .bridge.json
if [ -d ".git" ] && [ -n "${BRIDGE_API_KEY:-}" ]; then
  printf "-> Binding current repository (.bridge.json)...\n"
  brg init
fi

printf "=== Bridge Machine Setup Complete ===\n"
printf "Verify anytime with: brg status-bar && brg auth whoami --verify\n"
