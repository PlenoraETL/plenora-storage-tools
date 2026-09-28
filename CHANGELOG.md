# Changelog

## Unreleased

## 2.0.0 — in qualification

- Implement the 2.0 maintainability plan while preserving the 1.0 Rust API baseline, CLI protocol and Python calls.
- Separate provider validation, transfer, error mapping and publication responsibilities; extract CLI command execution.
- Require public Rust documentation, scoped Clippy exceptions, product size budgets, source comment rules and explicit dependency pin policy in CI.
- Scan the exact CLI archives and Python wheels with pinned Syft; require artifact-bound native inventories alongside lockfile SBOMs for 2.0 qualification.
- Add migration guidance and current quality documentation. Final 2.0 publication still requires its own artifact qualification and two-hour soak.

## 1.0.0 — in qualification

- Prepare the stable Rust library, CLI and Python SDK for Linux x86_64 and Windows x86_64, distributed only through GitHub Releases.
- Retain the nine-provider scope and public contracts exercised by the release candidates, including typed Python results, cancellation outcomes, bounded transfers and redacted errors.
- Require qualification of the final stable artifact bytes, including the provisional two-hour soak, platform and Python matrices, fault injection, API compatibility, coverage, dependency audits and performance budgets.
- Fix cross-platform qualification SBOM reproducibility by preserving LF in dependency declaration files.
- Keep AWS/Azure/GCS account compatibility unqualified and WebDAV compatibility limited to the explicitly configured fixture. Publication remains pending until the final evidence bundle and downloaded-asset checks pass.

## 1.0.0-rc.2 — in preparation

- Flush SFTP download sinks under deadline/cancellation control before reporting success; preserve redacted I/O errors and ambiguous effects. Exercise partial writes, flush failures/cancellation and source failures across all nine fixture providers.
- Add typed Python result dictionaries and strict installed-wheel consumers, separate Rust test sources from product coverage, and split CLI argument/output responsibilities while preserving public contracts.
- Require complete artifact-bound qualification evidence, expand dependency inventories and prepare GitHub draft publication with checks after download on both supported targets.
- Keep real cloud accounts outside the qualified scope; this candidate is not yet qualified or published.
- Restrict the WebDAV fixture claim to WsgiDAV 4.3.5 with serialized application requests after reproducing duplicate conditional writes on its default server. Keep HTTP connection workers separate, require an independent HTTP concurrency probe and record the deployment configuration in qualification scope.
- Calibrate the CLI performance gate with explicit absolute timing margins from preserved same-binary campaigns; retain relative limits, the memory budget and the original failed fixture evidence.

## 1.0.0-rc.1 — in qualification

- Prepare the first release candidate for qualification on Windows and Linux; publication and stable promotion require the remaining release gates.

- Scope the first 1.0 release to the nine declared fixture systems; defer real AWS, Azure and GCS qualification and record those services as not qualified in release manifests and receipts.

- Validate FTP/FTPS UNIX.mode facts before MLSD/MLST parsing to prevent a panic on malformed Unicode permissions, including parent-directory reconciliation.
- Measure installed Python wrapper line/branch coverage against wheel bytes on CPython 3.10–3.14; enforce per-module floors and test repeated cancellation, native error redaction and all async operations.

- Reject unrelated XML documents as invalid S3, Azure and WebDAV listings instead of silently returning an empty result.
- Exercise S3/Azure/WebDAV XML and shared FTP/FTPS parsers with bounded, coverage-guided libFuzzer campaigns, AddressSanitizer, versioned seeds and preserved crash evidence; gate CI and release candidates.

- Guard compiler-resolved Rust APIs on Windows/Linux, the compiled CLI model, installed Python signatures and versioned JSON contracts against a reviewed alpha baseline; add negative compatibility tests and release workflow gates.

- Preserve local artifact error causes and read/prepare/write/commit phases across Rust, CLI and Python; keep existing codes and redacted OS errors.
- Prevent Python input serialization callbacks from leaking raw exceptions; avoid deep-copying unvalidated engine configuration.
- Document the public API behavior baseline and its executable compatibility tests, with remaining API freeze work explicit.

- Disable implicit HTTP retries across S3, Azure, GCS and WebDAV; preserve ambiguous mutation outcomes for host reconciliation and reject invalid TLS without a retry loop.
- Release idle SMB sockets when the final connection owner drops; qualify persistent SDK resource use across all nine fixtures.
- Preserve access to settled async cancellation outcomes through Python 3.10 and `wait_for` wrappers with `cancellation_outcome()`.
- Build the pinned MinIO test server from verified official source when its former container images are unavailable.
- Reconcile concurrent parent-directory creation in FTP/FTPS, SFTP and WebDAV only after proving the parent is a directory.
- Add bounded-memory transfer, concurrency, coverage and seeded CLI mutation gates, plus a CPython compatibility matrix.
- Start the 1.0.0-alpha.1 development series; no stable 1.0 release is published.
- Support pinned SFTP public-key authentication with plain or encrypted OpenSSH keys resolved by the host.
- Align Python discovery, version identity, root errors and async lifecycle with the common SDK contract; verify the installed wheel outside the checkout.
- Add a reusable target qualification command and retain candidate test logs.
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
