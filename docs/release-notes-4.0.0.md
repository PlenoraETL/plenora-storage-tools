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
