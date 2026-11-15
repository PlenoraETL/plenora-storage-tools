#!/usr/bin/env bash
# Complete offline collection. No build, installation or remote service.
# Test repositories and evidence live under target; skip is never a proof PASS.
set -uo pipefail
cd "$(dirname "$0")/.." || exit 1
repo=$(pwd -P)
python=${PYTHON:-python3.14}
run=$(date -u +%Y%m%dT%H%M%SZ)-$$
collection="$repo/target/sempl-3-vm-$run"
mkdir -p "$collection" "$repo/target/sempl-3-tmp" || exit 1
source "$repo/scripts/proof_suites.sh" || exit 1
suite_summary="$collection/summary.tsv"
printf 'suite\texit\tlog\n' >"$suite_summary"
export TMPDIR="$repo/target/sempl-3-tmp"
export PLENORA_REQUIRE_REAL_ACL=1 PLENORA_REQUIRE_UNPRIVILEGED_RUN=1
run_suite prerequisite-python "$collection/python.log" "$python" -c 'import sys; print(sys.version); assert sys.version_info[:2] == (3, 14)'
run_suite prerequisite-python3 "$collection/python3.log" python3 -c 'import sys; print(sys.version); assert sys.version_info[:2] == (3, 14)'
for tool in bash sh flock getfacl setfacl openssl ssh-keygen git; do
  run_suite "prerequisite-$tool" "$collection/$tool.log" command -v "$tool"
done
run_suite prerequisite-acl "$collection/acl-package.log" bash -c 'dpkg-query -S "$(readlink -f "$(command -v getfacl)")"'
if [ "$(id -u)" != 0 ]; then
  run_suite prerequisite-sudo "$collection/sudo.log" sudo -n true
fi
for mask in 002 022; do
  umask "$mask"
  destination="$collection/$mask"
  mkdir -p "$destination" || exit 1
  printf '\nSEMPL-3 current run: umask=%s\n' "$mask"
  run_suite "$mask/test_ci_execution" "$destination/test_ci_execution.log" \
    "$python" -m unittest discover -s scripts/tests -p test_ci_execution.py -v
  run_suite "$mask/test_proof_suites" "$destination/test_proof_suites.log" \
    "$python" -m unittest discover -s scripts/tests -p test_proof_suites.py -v
  run_suite "$mask/preparation-sources" "$destination/preparation-sources.log" \
    "$python" -m unittest discover -s scripts/tests -p test_preparation_sources.py -v
  run_suite "$mask/preparation-reset" "$destination/preparation-reset.log" \
    env PYTHONPATH="$repo/scripts:$repo/scripts/tests" "$python" -m unittest \
    test_campaign_fence.PreparationProtocolTests.test_everything_a_preparation_creates_is_unshared_under_a_group_umask -v
  # frozen_proofs requires a fresh output directory; logs live in its parent.
  run_suite "$mask/frozen-catalog" "$destination/frozen-catalog.log" \
    "$python" scripts/tests/frozen_proofs.py --posix --output "$destination/frozen"
  run_suite "$mask/frozen-discovery" "$destination/frozen-discovery.log" \
    "$python" -m unittest discover -s scripts/tests -p 'test_frozen*.py' -v
  run_suite "$mask/proof-inventory" "$destination/proof-inventory.log" \
    "$python" scripts/check_proof_inventory.py --output "$destination/proof-inventory.json"
  run_suite "$mask/guard-coverage" "$destination/guard-coverage.log" \
    "$python" scripts/check_release_guard_coverage.py --output "$destination/ci-executor-receipts.json"
  run_suite "$mask/packages-offline" "$destination/packages-offline.log" \
    env PYTHONPATH="$repo/scripts:$repo/scripts/tests" "$python" - <<'PY'
import unittest
from unittest.mock import patch
import package_generated
suite = unittest.defaultTestLoader.loadTestsFromNames([
    'test_packages', 'test_packages_canonical', 'test_packages_identity', 'test_packages_metadata'])
with patch.object(package_generated, 'cargo_reference', side_effect=unittest.SkipTest('real Cargo packaging forbidden in SEMPL-3')):
    result = unittest.TextTestRunner(verbosity=2).run(suite)
raise SystemExit(not result.wasSuccessful())
PY
  for suite in test_campaign_fence test_campaign_state test_campaign_model test_campaign_transport \
               test_campaign_performance test_publication_campaign test_qualification_watchdog \
               test_fixture_startup test_proof_collection test_proof_inventory test_unique_json \
               test_release_publication test_release_guard_coverage test_release_workflow; do
    run_suite "$mask/$suite" "$destination/$suite.log" \
      "$python" -m unittest discover -s scripts/tests -p "$suite.py" -v
  done
done
finish_suites
exit $?
