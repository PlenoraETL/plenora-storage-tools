#!/usr/bin/env bash
set -euo pipefail
trap 'status=$?; python3 scripts/export_evidence.py || status=$?; exit "$status"' EXIT

# Maturin requests metadata for all targets, including dependencies unused by
# this host. Populate that complete lockfile before any offline wheel build.
cargo fetch --locked

test -s .fixtures/ca.crt
test -s .fixtures/sftp-fingerprint
cp .fixtures/ca.crt /usr/local/share/ca-certificates/plenora-storage-fixture.crt
update-ca-certificates > /tmp/plenora-storage-certificates.log
export PLENORA_SFTP_HOST_KEY_SHA256
PLENORA_SFTP_HOST_KEY_SHA256="$(cat .fixtures/sftp-fingerprint)"
export PLENORA_SFTP_PRIVATE_KEY_FILE="$PWD/.fixtures/sftp-client"
export PLENORA_SFTP_ENCRYPTED_KEY_FILE="$PWD/.fixtures/sftp-client-encrypted"

python3 scripts/check_webdav_fixture.py
cargo fmt --all -- --check
cargo clippy --workspace --all-targets --all-features --locked -- -D warnings
python3 scripts/check_anti_panic.py
cargo test --workspace --all-targets --locked -- --include-ignored
cargo build --quiet --locked -p plenora-storage-cli
python3 scripts/release_publication.py smoke-cli --binary target/debug/plenora-storage
python3 scripts/qualify_local_faults.py
python3 scripts/qualify_local_faults.py --spool-uploads --output target/release-readiness/spooled-local-faults.json

cli_tmp="$(mktemp -d)"
cli_key="conformance/cli-${RANDOM}-${RANDOM}.txt"
cli_bin="target/debug/plenora-storage"
printf '%s' 'plenora-storage-cli conformance' > "${cli_tmp}/input.txt"

"${cli_bin}" --format json --version
"${cli_bin}" --format json capabilities

set +e
"${cli_bin}" --format json \
  --allow-experimental-contracts \
  test --connection docker/minio-connection.json \
  > "${cli_tmp}/error.json" 2> "${cli_tmp}/error.stderr"
error_code=$?
set -e
test "${error_code}" -eq 2
test ! -s "${cli_tmp}/error.stderr"
test "$(wc -l < "${cli_tmp}/error.json")" -eq 1
grep -q '"status":"error"' "${cli_tmp}/error.json"
grep -q '"category":"invalid_configuration"' "${cli_tmp}/error.json"

"${cli_bin}" \
  --format json --allow-experimental-contracts --allow-insecure-http --allow-private-network \
  test --connection docker/minio-connection.json
"${cli_bin}" \
  --format json --allow-experimental-contracts --allow-insecure-http --allow-private-network \
  put --connection docker/minio-connection.json --key "${cli_key}" \
  --input "${cli_tmp}/input.txt" --overwrite true --publication-policy atomic-required
"${cli_bin}" \
  --format json --allow-experimental-contracts --allow-insecure-http --allow-private-network \
  get --connection docker/minio-connection.json --key "${cli_key}" \
  --output "${cli_tmp}/output.txt" --overwrite true
cmp "${cli_tmp}/input.txt" "${cli_tmp}/output.txt"
"${cli_bin}" \
  --format json --allow-experimental-contracts --allow-insecure-http --allow-private-network \
  delete --connection docker/minio-connection.json --key "${cli_key}" --ignore-missing false

run_cli_roundtrip() {
  provider="$1"
  connection="$2"
  shift 2
  key="conformance/cli-${provider}-${RANDOM}-${RANDOM}.txt"

  "${cli_bin}" --format json "$@" \
    test --connection "${connection}"
  "${cli_bin}" --format json "$@" \
    put --connection "${connection}" --key "${key}" \
    --input "${cli_tmp}/input.txt" --overwrite true --publication-policy best-effort
  "${cli_bin}" --format json "$@" \
    get --connection "${connection}" --key "${key}" \
    --output "${cli_tmp}/output-${provider}.txt" --overwrite true
  cmp "${cli_tmp}/input.txt" "${cli_tmp}/output-${provider}.txt"
  "${cli_bin}" --format json "$@" \
    delete --connection "${connection}" --key "${key}" --ignore-missing false
}

run_cli_roundtrip \
  sftp docker/sftp-connection.json \
  --allow-experimental-contracts --allow-private-network --allow-unverified-ssh
run_cli_roundtrip \
  ftp docker/ftp-connection.json \
  --allow-experimental-contracts --allow-private-network --allow-insecure-ftp

python3 scripts/audit_release_readiness.py
python3 scripts/qualify_cli.py
python3 scripts/qualify_commit_faults.py

python3 scripts/qualify_extended_faults.py
python3 scripts/qualify_extended_faults.py --spool-uploads --output target/release-readiness/spooled-regressions.json
if [ "${PLENORA_EXTENDED_TEST:-0}" = "1" ]; then
  python3 scripts/qualify_extended.py
  python3 scripts/qualify_extended.py --spool-uploads --output target/release-readiness/spooled-qualification.json
  python3 scripts/build_python.py --debug
  version="$(python3 scripts/release_version.py --python)"
  target/python-sdk-test/bin/python scripts/qualify_python.py \
    --wheel target/python-wheels/plenora_storage-"${version}"-*.whl \
    --output target/release-readiness/python-qualification.json
  target/python-sdk-test/bin/python scripts/qualify_python.py --spool-uploads \
    --wheel target/python-wheels/plenora_storage-"${version}"-*.whl \
    --output target/release-readiness/spooled-python-qualification.json
fi
