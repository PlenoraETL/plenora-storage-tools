#!/usr/bin/env bash
# Source in a collector with errexit disabled. Every command keeps its own log
# and real exit status; returning success here only lets collection continue.
suite_failures=0

run_suite() {
  local label=$1 log=$2 status=0
  shift 2
  printf '\nRUN %s\n' "$label"
  if "$@" >"$log" 2>&1; then status=0; else status=$?; fi
  printf '%s\t%s\t%s\n' "$label" "$status" "$log" >>"$suite_summary"
  printf 'RESULT %s exit=%s log=%s\n' "$label" "$status" "$log"
  if [ "$status" -ne 0 ]; then
    suite_failures=1
    cat "$log"
  fi
  return 0
}

finish_suites() {
  printf '\nAll suite results (exit 0 is command success; see logs for skips):\n'
  cat "$suite_summary"
  return "$suite_failures"
}
