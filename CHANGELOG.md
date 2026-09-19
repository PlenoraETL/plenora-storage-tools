# Changelog

## Unreleased

- Prepare complete Cargo caches before offline Python builds on clean CI runners.
- Normalize Rust prerelease versions to Python wheel metadata and preserve qualification gates for release candidates.

## 0.2.2

- Refresh direct stable dependencies and the compatible lockfile, including FTP/SFTP major API migrations.
- Complete the FTPS peer TLS shutdown before releasing upload sockets to prevent truncated Windows transfers.
- Preserve SHA-256 output and SSH host-key pinning with the updated libraries.
- Qualify Rust, CLI and Python on Windows/Linux against all nine storage fixtures.

## 0.2.1

- Share application composition and file transfers through `plenora-storage-engine`.
- Select any provider subset with Cargo features; verify isolated builds and discovery.
- Add a typed Python SDK backed by PyO3, synchronous and asyncio engines,
  host credential callbacks, cancellation, deadlines and staged file downloads.
- Build and test installed Python wheels, generate the current product inventory,
  and include a CycloneDX dependency graph and artifact hashes in release packaging.
- Align quality policy and executable CI gates with Database Tools.

## 0.2.0

- Add local filesystem, explicit FTPS, Azure Blob / ADLS Gen2 via Blob API,
  encrypted SMB3, Google Cloud Storage JSON API and WebDAV on Rust and CLI.
- Add versioned configuration contracts, host-resolved credentials and bounded
  transfers; expose publication and create-if-absent guarantees per provider.
- Preserve FTPS TLS data streams through the server acknowledgement and verify
  transferred sizes. FTP copy checks the source before creating destination directories.
- Reject unsafe raw listing keys, redirects, repeated continuation tokens and
  partial WebDAV mutation responses; guard WebDAV deletion with strong ETags.
- Add six-provider fixtures, concurrent-create and protocol-failure qualification,
  and extend release gates to all nine providers on Linux and Windows.
- Include a documented, narrowly patched SMB transport with upstream RustSec auditing.
  Package all seven crates and validate the extracted artifacts.
- Preserve 0.1.0 artifacts; cloud emulators do not qualify real cloud deployments.

## 0.1.0 — prepared for release

- Add bounded CLI pagination within one process (`list --all`).
- Enforce FTP parent-probe deadlines and cancellation.
- Qualify SFTP atomic replacement with the OpenSSH POSIX rename extension.
- Reject oversized declared uploads before opening destinations; account for
  parent directory effects in failure and recovery reporting.
- Validate provider configurations and transport policies during preflight.
- Bound CLI connection-file reads; handle Unix SIGTERM and conservative panic outcomes.
- Make fixture skips explicit and qualify HTTPS, SSH pins and all three providers.
- Upgrade rustls for RUSTSEC-2026-0285 and refresh yanked dependencies.
- Package all five crates with licenses and versioned dependencies; build Windows
  and Linux candidates, validate extracted archives, and emit SHA-256 manifests.
- Qualify lost multipart commit responses, late commits, deadlines and SIGTERM;
  exercise SFTP commit interruption and staging cleanup without deleting the final object.
- Enforce canonical runtime UUIDs, preserve optional causation identity, and
  report route mismatches as protocol errors before invocation.
- Generate validated adoption manifests v4 with actual artifact digests and
  run the runtime binding suite from the packaged core crate.
- Promote the v1 catalog to available; the old experimental flag remains accepted.
- Require matching committed sources, platform evidence and dependency gates
  before sealing a release as qualified for publication.
