#!/usr/bin/env bash
# Installs apimend and the tools it needs. Runs automatically when a codespace is
# created, and in CI (.github/workflows/ci.yml), so both use exactly the same setup.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PRISM_VERSION="5.14.2"
OASDIFF_VERSION="1.36.0"
OASDIFF_CHECKSUMS="${SCRIPT_DIR}/oasdiff.sha256"  # official SHA-256 fingerprints

echo "==> Installing apimend and its developer tools"
pip install --upgrade pip
pip install -e ".[dev]"

echo "==> Installing Prism ${PRISM_VERSION} (fake API server)"
npm install -g "@stoplight/prism-cli@${PRISM_VERSION}"

echo "==> Installing oasdiff ${OASDIFF_VERSION} (API spec comparer)"
case "$(uname -m)" in
  x86_64) ARCH="amd64" ;;
  aarch64 | arm64) ARCH="arm64" ;;
  *) echo "Unsupported CPU architecture: $(uname -m)"; exit 1 ;;
esac
FILE="oasdiff_${OASDIFF_VERSION}_linux_${ARCH}.tar.gz"
URL="https://github.com/oasdiff/oasdiff/releases/download/v${OASDIFF_VERSION}/${FILE}"
TMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TMP_DIR"' EXIT  # clean up even if a step below fails
if ! curl -fsSL --retry 6 --retry-delay 5 --retry-all-errors --connect-timeout 20 \
    "$URL" -o "$TMP_DIR/$FILE"; then
  echo "!! Could not download oasdiff from GitHub (temporary network problem)."
  echo "!! Wait a few minutes, then run:  bash .devcontainer/setup.sh"
  exit 1
fi

# Refuse to install a file that does not match its official fingerprint.
if [ ! -f "$OASDIFF_CHECKSUMS" ]; then
  echo "!! Missing ${OASDIFF_CHECKSUMS}. Refusing to install oasdiff without a checksum."
  exit 1
fi
EXPECTED="$(awk -v file="$FILE" '$2 == file { print $1 }' "$OASDIFF_CHECKSUMS")"
ACTUAL="$(sha256sum "$TMP_DIR/$FILE" | cut -d ' ' -f 1)"
if [ -z "$EXPECTED" ]; then
  echo "!! No checksum for ${FILE} in ${OASDIFF_CHECKSUMS}. Refusing to install it."
  exit 1
fi
if [ "$ACTUAL" != "$EXPECTED" ]; then
  echo "!! Checksum mismatch for ${FILE}: the download may be corrupted or tampered with."
  echo "!!   expected ${EXPECTED}"
  echo "!!   got      ${ACTUAL}"
  echo "!! Nothing was installed."
  exit 1
fi
echo "    checksum verified (sha256 ${ACTUAL})"
tar -xzf "$TMP_DIR/$FILE" -C "$TMP_DIR"
sudo install -m 0755 "$TMP_DIR/oasdiff" /usr/local/bin/oasdiff

echo "==> Checking everything is ready"
apimend doctor
