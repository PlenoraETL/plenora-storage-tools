#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p .fixtures/minio
trap 'status=$?; if [ -f .fixtures/certificate-generation.log ]; then tail -n 30 .fixtures/certificate-generation.log >&2; fi; exit "$status"' ERR
docker compose build minio
# Ephemeral test identity: never copy this key into a deployment.
openssl req -x509 -newkey rsa:2048 -nodes -days 2 \
  -keyout .fixtures/ca.key -out .fixtures/ca.crt \
  -subj '/CN=Plenora storage test CA' -addext 'basicConstraints=critical,CA:TRUE' \
  > .fixtures/certificate-generation.log 2>&1
openssl req -new -newkey rsa:2048 -nodes -subj '/CN=minio-tls' \
  -keyout .fixtures/minio/private.key -out .fixtures/minio.csr \
  >> .fixtures/certificate-generation.log 2>&1
printf '%s\n' 'basicConstraints=critical,CA:FALSE' 'keyUsage=critical,digitalSignature,keyEncipherment' \
  'extendedKeyUsage=serverAuth' 'subjectAltName=DNS:minio-tls' > .fixtures/minio.ext
openssl x509 -req -days 2 -in .fixtures/minio.csr -CA .fixtures/ca.crt \
  -CAkey .fixtures/ca.key -CAcreateserial -extfile .fixtures/minio.ext \
  -out .fixtures/minio/public.crt >> .fixtures/certificate-generation.log 2>&1
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
