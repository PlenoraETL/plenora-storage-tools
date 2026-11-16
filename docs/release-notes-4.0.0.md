# Plenora Storage Tools 4.0.0

4.0.0 is a major release.

## Breaking changes

- Python 3.14 is the minimum supported runtime. The native SDK wheels use the
  cp314 stable ABI; earlier Python runtimes are not supported.
- No compatibility with earlier Storage releases is provided.

## Changes

- Adopt the approved Plenora dependency snapshot and its Linux CI checker,
  including public dependency pins, base64 0.23.1 and proptest 1.12.0.
- Align compatible transitive dependencies with the family references,
  retaining AWS-LC and the exceptions declared in the common manifest.
- Fixture certificates now pass strict TLS verification; fixture startup checks
  application readiness and reports sanitized failures without retrying
  permanent TLS errors.
- Campaign and CI orchestration rebuilt around a single campaign model
  (state, lease and epoch transitions, file permissions, time budgets, durable
  writes) and an executable CI plan with execution receipts.

## Distribution

Rust crates, source archive, CLI for Linux x86_64 and Windows x86_64, and
native Python wheels for both platforms. Nine providers and seven operations.
Tests run against local fixtures; they do not certify real cloud accounts or
arbitrary remote servers.

## Release evidence policy

`storage-release-no-soak-v1` removes soak and its calibration from the release
campaign and required evidence. The bundle cannot claim a passed soak.
Owner decision, 16 November 2026: «il soak non fa parte del cancello di questa release; una campagna soak della famiglia è prevista più avanti».
The manual tools `stress_python.py`, `soak_policy.py`, `calibrate_cycles.py`
and their tests remain available for that later work. Earlier campaign
failures retain their original results; this change is not qualification evidence.
Parser release evidence replays all seeds and uses a 10,000-execution budget
per ASan target; timed exploration remains scheduled separately. Previous
release binaries and performance A/B are no longer release dependencies.
No absolute performance SLO is claimed. Large transfers, spool boundaries,
concurrency, platform ABIs and real timeout/keepalive tests remain required.
Local oracle calibration is not a qualification of the final 4.0.0 artifacts.
