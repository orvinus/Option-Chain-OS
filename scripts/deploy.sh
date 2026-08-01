#!/usr/bin/env bash
# Wrapper so every compose command gets the right flags.
#
# WHY THIS EXISTS: with `-f docker/docker-compose.yml`, Compose treats docker/ as the
# project directory and looks for docker/.env — NOT the repo-root .env where the real
# settings live. Forget `--env-file .env` and compose either fails ("required variable
# POSTGRES_PASSWORD is missing a value") or, worse, silently starts with different
# values than the last deploy.
#
# Usage:
#   scripts/deploy.sh                 # build + start (default)
#   scripts/deploy.sh ps
#   scripts/deploy.sh logs -f backend
#   scripts/deploy.sh restart backend
set -euo pipefail

cd "$(dirname "$0")/.."

[[ -f .env ]] || { echo "ERROR: .env not found in $(pwd)"; exit 1; }

# Fail early with a clear message instead of a compose interpolation error.
for required in POSTGRES_PASSWORD; do
  grep -qE "^${required}=.+" .env || {
    echo "ERROR: $required is not set in .env"
    echo "  Generate one:  openssl rand -base64 32 | tr -d '/+=' | head -c 32"
    echo "  NOTE: on an EXISTING database you must ALTER USER postgres PASSWORD '<new>' FIRST,"
    echo "        otherwise the backend cannot authenticate."
    exit 1
  }
done

[[ $# -eq 0 ]] && set -- up -d --build

exec docker compose --env-file .env -f docker/docker-compose.yml "$@"
