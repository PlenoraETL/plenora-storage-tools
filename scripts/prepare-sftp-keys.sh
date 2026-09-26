#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p .fixtures
if [ ! -s .fixtures/sftp-client ]; then
  ssh-keygen -q -t ed25519 -N '' -f .fixtures/sftp-client
fi
if [ ! -s .fixtures/sftp-client-encrypted ]; then
  ssh-keygen -q -t ed25519 -N 'plenora-key-fixture-secret' -f .fixtures/sftp-client-encrypted
fi
cat .fixtures/sftp-client.pub .fixtures/sftp-client-encrypted.pub | \
  docker compose exec -T sftp sh -c '
    mkdir -p /home/plenora/.ssh
    cat >> /home/plenora/.ssh/authorized_keys
    chown -R plenora:users /home/plenora/.ssh
    chmod 700 /home/plenora/.ssh
    chmod 600 /home/plenora/.ssh/authorized_keys
  '
