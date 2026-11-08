#!/usr/bin/env bash
# Runs once, automatically, when a codespace is created.
set -euo pipefail

OASDIFF_VERSION="1.36.0"

echo "==> Installing apimend and its developer tools"
pip install --upgrade pip
pip install -e ".[dev]"

echo "==> Installing Prism (fake API server)"
npm install -g @stoplight/prism-cli

echo "==> Installing oasdiff ${OASDIFF_VERSION} (API spec comparer)"
case "$(uname -m)" in
  x86_64) ARCH="amd64" ;;
  aarch64 | arm64) ARCH="arm64" ;;
  *) echo "Unsupported CPU architecture: $(uname -m)"; exit 1 ;;
esac
URL="https://github.com/oasdiff/oasdiff/releases/download/v${OASDIFF_VERSION}/oasdiff_${OASDIFF_VERSION}_linux_${ARCH}.tar.gz"
TMP_DIR="$(mktemp -d)"
if ! curl -fsSL --retry 6 --retry-delay 5 --retry-all-errors --connect-timeout 20 \
    "$URL" -o "$TMP_DIR/oasdiff.tar.gz"; then
  echo "!! Could not download oasdiff from GitHub (temporary network problem)."
  echo "!! Wait a few minutes, then run:  bash .devcontainer/setup.sh"
  exit 1
fi
tar -xzf "$TMP_DIR/oasdiff.tar.gz" -C "$TMP_DIR"
sudo install -m 0755 "$TMP_DIR/oasdiff" /usr/local/bin/oasdiff
rm -rf "$TMP_DIR"

echo "==> Checking everything is ready"
apimend doctor