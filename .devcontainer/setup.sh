#!/usr/bin/env bash
# Runs once, automatically, when a codespace is created.
set -e

echo "==> Installing apimend and its developer tools"
pip install --upgrade pip
pip install -e ".[dev]"

echo "==> Installing Prism (fake API server)"
npm install -g @stoplight/prism-cli

echo "==> Installing oasdiff (API spec comparer)"
curl -fsSL https://raw.githubusercontent.com/oasdiff/oasdiff/main/install.sh | sudo sh

echo "==> Checking everything is ready"
apimend doctor