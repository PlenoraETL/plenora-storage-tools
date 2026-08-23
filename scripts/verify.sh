#!/usr/bin/env bash
set -euo pipefail

cargo fmt --all -- --check
cargo clippy --workspace --all-targets --all-features --locked -- -D warnings
cargo test --workspace --all-targets --locked
cargo build --quiet --locked -p plenora-storage-cli

cli_tmp="$(mktemp -d)"
cli_key="conformance/cli-${RANDOM}-${RANDOM}.txt"
cli_bin="target/debug/plenora-storage"
printf '%s' 'plenora-storage-cli conformance' > "${cli_tmp}/input.txt"

"${cli_bin}" --format json --version
"${cli_bin}" --format json capabilities

set +e
"${cli_bin}" --format json \
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
  --format json --allow-insecure-http --allow-private-network \
  test --connection docker/minio-connection.json
"${cli_bin}" \
  --format json --allow-insecure-http --allow-private-network \
  put --connection docker/minio-connection.json --key "${cli_key}" \
  --input "${cli_tmp}/input.txt" --overwrite
"${cli_bin}" \
  --format json --allow-insecure-http --allow-private-network \
  get --connection docker/minio-connection.json --key "${cli_key}" \
  --output "${cli_tmp}/output.txt"
cmp "${cli_tmp}/input.txt" "${cli_tmp}/output.txt"
"${cli_bin}" \
  --format json --allow-insecure-http --allow-private-network \
  delete --connection docker/minio-connection.json --key "${cli_key}"

run_cli_roundtrip() {
  provider="$1"
  connection="$2"
  shift 2
  key="conformance/cli-${provider}-${RANDOM}-${RANDOM}.txt"

  "${cli_bin}" --format json "$@" \
    test --connection "${connection}"
  "${cli_bin}" --format json "$@" \
    put --connection "${connection}" --key "${key}" \
    --input "${cli_tmp}/input.txt" --overwrite
  "${cli_bin}" --format json "$@" \
    get --connection "${connection}" --key "${key}" \
    --output "${cli_tmp}/output-${provider}.txt"
  cmp "${cli_tmp}/input.txt" "${cli_tmp}/output-${provider}.txt"
  "${cli_bin}" --format json "$@" \
    delete --connection "${connection}" --key "${key}"
}

run_cli_roundtrip \
  sftp docker/sftp-connection.json \
  --allow-private-network --allow-unverified-ssh
run_cli_roundtrip \
  ftp docker/ftp-connection.json \
  --allow-private-network --allow-insecure-ftp
