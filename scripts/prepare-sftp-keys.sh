#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
profile=$(python3 scripts/campaign_shell.py fixture_file)
eval "$profile"
mkdir -p .fixtures
if [ ! -s .fixtures/sftp-client ]; then
  ssh-keygen -q -t ed25519 -N '' -f .fixtures/sftp-client
fi
if [ ! -s .fixtures/sftp-client-encrypted ]; then
  ssh-keygen -q -t ed25519 -N 'plenora-key-fixture-secret' -f .fixtures/sftp-client-encrypted
fi
ssh_profile=$(python3 scripts/campaign_shell.py private_directory)
cat .fixtures/sftp-client.pub .fixtures/sftp-client-encrypted.pub | \
  docker compose exec -T sftp sh -c '
    eval "$1"
    mkdir -p /home/plenora/.ssh
    cat >> /home/plenora/.ssh/authorized_keys
    chown -R plenora:users /home/plenora/.ssh
    chmod "$profile_mode" /home/plenora/.ssh
    chmod "$2" /home/plenora/.ssh/authorized_keys
  ' campaign-keys "$ssh_profile" "$(python3 scripts/campaign_shell.py private_file --mode)"
