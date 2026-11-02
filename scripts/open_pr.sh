#!/usr/bin/env bash
# Open a PR for this repository only. Usage:
#   bash scripts/open_pr.sh <base> <head> <title> <body-file>
set -euo pipefail

REPO="Vvynot/sigma-fp-companion"
BASE="${1:?base branch}"
HEAD="${2:?head branch}"
TITLE="${3:?title}"
BODY="${4:?body file}"

exec "/c/Program Files/GitHub CLI/gh.exe" pr create \
  --repo "$REPO" \
  --base "$BASE" \
  --head "$HEAD" \
  --title "$TITLE" \
  --body-file "$BODY"
