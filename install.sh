#!/bin/sh
# claude-cost installer: installs uv if missing, then the latest claude-cost Release.
# Safe to re-run: it leaves you on the latest Release. Needs curl; does not need git.
# Supports macOS and Linux.
set -eu

REPO="Ilya1vanov/claude-cost"
API="https://api.github.com/repos/$REPO/releases/latest"

say() { printf '%s\n' "$*"; }
fail() { printf 'claude-cost install: %s\n' "$*" >&2; exit 1; }

case "$(uname -s)" in
  Darwin | Linux) ;;
  *) fail "only macOS and Linux are supported (Windows is not supported yet)." ;;
esac

command -v curl >/dev/null 2>&1 || fail "curl is required but was not found."

ORIG_PATH=$PATH  # what the user's own terminal sees, before this script adds anything

tmp=$(mktemp -d) || fail "could not create a temporary folder."
trap 'rm -rf "$tmp"' EXIT

# Step 1: uv, with its official installer
if ! command -v uv >/dev/null 2>&1; then
  say "Installing uv (the tool that installs claude-cost)..."
  curl -fsSL https://astral.sh/uv/install.sh -o "$tmp/uv-install.sh" || fail "could not download the uv installer."
  sh "$tmp/uv-install.sh" || fail "could not install uv."
  # The uv installer puts uv in one of these; make it visible to this script.
  PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
  export PATH
  command -v uv >/dev/null 2>&1 || fail "uv was installed but was not found. Open a new terminal and run this again."
fi

# Step 2: the latest Release's wheel
say "Looking for the latest claude-cost release..."
curl -fsSL -H "Accept: application/vnd.github+json" "$API" -o "$tmp/release.json" \
  || fail "could not reach GitHub to find the latest release. Check your connection and try again."
wheel=$(grep -o '"browser_download_url"[[:space:]]*:[[:space:]]*"[^"]*\.whl"' "$tmp/release.json" \
  | head -n 1 | sed 's/.*:[[:space:]]*"\(.*\)"/\1/') || true
[ -n "$wheel" ] || fail "the latest release has no installable file."

# Step 3: install (--force: re-running always lands on the latest Release)
say "Installing claude-cost..."
uv tool install --force "$wheel" || fail "installation failed."

# Step 4: report
bin_dir=$(uv tool dir --bin 2>/dev/null || printf '%s' "$HOME/.local/bin")
if [ -x "$bin_dir/claude-cost" ]; then
  command_path="$bin_dir/claude-cost"
elif command -v claude-cost >/dev/null 2>&1; then
  command_path=claude-cost
else
  fail "installed, but the claude-cost command was not found. Run 'uv tool update-shell', open a new terminal and try again."
fi

say ""
say "Installed: $("$command_path" --version)"
if PATH=$ORIG_PATH command -v claude-cost >/dev/null 2>&1; then
  say "Open the dashboard with:  claude-cost serve"
else
  say ""
  say "Open a new Terminal window first (it adds claude-cost to your PATH), then start the dashboard with:"
  say "  claude-cost serve"
  say "If that says 'command not found', run 'uv tool update-shell' and open another new Terminal window."
fi
