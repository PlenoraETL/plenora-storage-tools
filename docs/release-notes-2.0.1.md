# Plenora Storage Tools 2.0.1

This patch consolidates release qualification without changing the public Rust,
CLI or Python contracts.

- Reproduce backend disk pressure and recovery on an isolated, bounded MinIO
  filesystem; require the result against the final Linux CLI bytes.
- Reserve filesystem headroom before large transfer and concurrency campaigns.
- Version the Windows/VM campaign coordinator and gated publication commands.
  Resume verified phases, preserve failed attempts, and bind checkpoints to
  source, configuration and artifact digests.
- Keep the nine-fixture scope, Linux/Windows x86_64 and CPython 3.10–3.14.
  Real AWS/Azure/GCS accounts remain unqualified; WebDAV retains its documented
  serialized WsgiDAV fixture restriction. The provisional soak is two hours.

The published qualification receipt and checksums identify the actual released
artifacts. See the packaged campaign guide for repeatable qualification.
