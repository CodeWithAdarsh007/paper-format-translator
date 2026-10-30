#!/usr/bin/env bash
# Deploy app/ to a HuggingFace Streamlit Space.
#   1) hf auth login          (paste a write token from hf.co/settings/tokens)
#   2) ./deploy_hf.sh <hf-username> [space-name]
set -euo pipefail

USER_NAME="${1:?usage: deploy_hf.sh <hf-username> [space-name]}"
SPACE="${2:-paper-format-translator}"
REPO="$USER_NAME/$SPACE"
APP_DIR="$(cd "$(dirname "$0")" && pwd)"

if ! hf auth whoami >/dev/null 2>&1; then
  echo "Not logged in. Run: hf auth login" >&2
  exit 1
fi

STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT
tar --exclude='__pycache__' --exclude='.pytest_cache' -C "$APP_DIR" -cf - . | tar -C "$STAGE" -xf -

hf repo create "$REPO" --repo-type space --space-sdk streamlit --exist-ok
# third positional "." puts the staging contents at the repo root
hf upload "$REPO" "$STAGE" . --repo-type space \
  --commit-message "Paper Format Translator: Streamlit app + cached demo papers"

echo "Space: https://huggingface.co/spaces/$REPO"
echo "URL:   https://$(echo "$USER_NAME" | tr '[:upper:]' '[:lower:]')-$SPACE.hf.space"
echo "First build installs TeX Live via packages.txt; expect 5-10 minutes."
