#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
profile=$(python3 scripts/campaign_shell.py fixture_file)
eval "$profile"
mkdir -p .fixtures/extended
trap 'status=$?; tail -n 30 .fixtures/extended/certificate-generation.log >&2; exit "$status"' ERR
python3 scripts/fixture_tls.py ftps --output .fixtures/extended > .fixtures/extended/certificate-generation.log 2>&1

docker compose -f docker-compose.yml -f compose.extended.yml build ftps
# PLENORA_FIXTURE_RECREATE=1 recreates the containers: the in-memory servers
# start empty and every server serves the certificate generated above.
recreate=()
if [ "${PLENORA_FIXTURE_RECREATE:-0}" = 1 ]; then recreate=(--force-recreate); fi
docker compose -f docker-compose.yml -f compose.extended.yml up -d --wait "${recreate[@]}" azure gcs ftps webdav smb
docker compose -f docker-compose.yml -f compose.extended.yml run --rm --no-deps azure-init
docker compose -f docker-compose.yml -f compose.extended.yml run --rm --no-deps azure-init gcs
