#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
rustup component add llvm-tools-preview
cargo install cargo-llvm-cov --version 0.9.1 --locked --root target/coverage-tools
mkdir -p .fixtures/evidence
cp .fixtures/ca.crt /usr/local/share/ca-certificates/plenora-storage-fixture.crt
update-ca-certificates >/tmp/plenora-storage-coverage-certificates.log
export PLENORA_SFTP_HOST_KEY_SHA256
PLENORA_SFTP_HOST_KEY_SHA256="$(cat .fixtures/sftp-fingerprint)"
export PLENORA_SFTP_PRIVATE_KEY_FILE="$PWD/.fixtures/sftp-client"
export PLENORA_SFTP_ENCRYPTED_KEY_FILE="$PWD/.fixtures/sftp-client-encrypted"
export PATH="$PWD/target/coverage-tools/bin:$PATH"
export CARGO_TARGET_DIR="$PWD/target/llvm-cov-target"
# Drive the instrumented product through its public CLI and installed wheel as
# well. Rust unit tests alone do not exercise the application composition root.
source <(cargo llvm-cov show-env --sh)
cargo llvm-cov clean --workspace
python3 scripts/run_logged.py .fixtures/evidence/coverage-tests.log \
  cargo test --workspace --all-targets --all-features --locked -- --include-ignored
cargo build --locked -p plenora-storage-cli
export PLENORA_CLI_BIN="$CARGO_TARGET_DIR/debug/plenora-storage"
python3 scripts/run_logged.py .fixtures/evidence/coverage-cli.log python3 scripts/qualify_cli.py
python3 scripts/run_logged.py .fixtures/evidence/coverage-extended.log python3 scripts/qualify_extended.py
python3 scripts/run_logged.py .fixtures/evidence/coverage-regressions.log python3 scripts/audit_release_readiness.py
python3 scripts/run_logged.py .fixtures/evidence/coverage-extended-faults.log python3 scripts/qualify_extended_faults.py
python3 scripts/run_logged.py .fixtures/evidence/coverage-python-build.log python3 scripts/build_python.py --debug
version="$(python3 scripts/release_version.py --python)"
python3 scripts/run_logged.py .fixtures/evidence/coverage-python-live.log \
  target/python-sdk-test/bin/python scripts/qualify_python.py \
    --wheel target/python-wheels/plenora_storage-"${version}"-*.whl \
    --output .fixtures/evidence/coverage-python-qualification.json
cargo llvm-cov report --json --output-path .fixtures/evidence/rust-coverage.json
python3 scripts/summarize_coverage.py .fixtures/evidence/rust-coverage.json \
  .fixtures/evidence/coverage-summary.json --policy scripts/coverage-policy.json
