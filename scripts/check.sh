#!/bin/sh
# Local quality gate (this repo has no CI by design): lint + tests. Run before every commit.
#   sh scripts/check.sh
set -e
cd "$(dirname "$0")/.."
PY="${PYTHON:-python3}"
"$PY" -m ruff check .
"$PY" -m pytest
echo "✅ check passed"
