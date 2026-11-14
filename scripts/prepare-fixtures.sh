#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p .fixtures/minio
trap 'status=$?; if [ -f .fixtures/certificate-generation.log ]; then tail -n 30 .fixtures/certificate-generation.log >&2; fi; exit "$status"' ERR
# Reject invalid identities before building or starting any fixture container.
python3 scripts/fixture_tls.py minio --output .fixtures > .fixtures/certificate-generation.log 2>&1
docker compose build minio

docker compose up -d --force-recreate minio-tls
# PLENORA_FIXTURE_RECREATE=1 recreates every fixture container, so a campaign
# can start its measurements on fresh servers; data volumes are kept.
recreate=()
if [ "${PLENORA_FIXTURE_RECREATE:-0}" = 1 ]; then recreate=(--force-recreate); fi
docker compose up -d --wait "${recreate[@]}" minio minio-tls sftp ftp
docker compose run --rm minio-init
docker compose exec -T sftp ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub \
  | awk '{print $2}' > .fixtures/sftp-fingerprint
test -s .fixtures/sftp-fingerprint

bash scripts/prepare-sftp-keys.sh
