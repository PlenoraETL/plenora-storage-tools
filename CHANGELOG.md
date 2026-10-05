# Changelog

## Unreleased

- Replace the yanked `yoke-derive` 0.8.3 with 0.8.4 in the workspace and fuzz lockfiles; `cargo deny` now rejects yanked crates like `cargo audit --deny warnings`.
- Reject `null` for optional runtime metadata (`plenora.execution.deadline`, `plenora.idempotency.key`, `plenora.message.causation_id`) instead of reading it as absent; a null deadline previously started the operation without a deadline. Absent values are no longer serialized as `null`.
- Require the `content_type`, `size` and `sha256` keys of artifact metadata (nullable but present, as in `plenora-storage-common-v1`) and the nullable keys of object, list and transfer results when deserializing.
- Reject `tls_ca_pem: null` in FTP/FTPS connections; the schema types the key as a string.
- Execute the storage fixtures of RUNTIME-VECTORS-1.0, copied byte for byte from the adopted contracts revision and pinned by SHA-256, through the runtime binding, including fail-closed routing mutations.
- Reject non-canonical runtime version selectors such as `01` and `+1`, which were parsed as `1` and dispatched.
- Pin the SDK build backend to `maturin==1.15.0` and list the transitive dependencies of the quality and campaign requirement files with exact versions; `check_dependencies.py` now rejects unpinned Python requirements.
- **Breaking (Rust API, 3.0.0):** `StorageError` gains the public field `execution_id: Option<ExecutionId>`, so struct literals and exhaustive patterns must name it. A `plenora-error-v1` document carrying `execution_id` was rejected by `deny_unknown_fields`; it is now accepted when it is a string of 1 to 128 characters or `null`, and other lengths are refused. The key is serialized only when present, so errors produced by this component are unchanged on the wire. Python `PlenoraError` exposes the same optional `execution_id` attribute.
- Adopt `plenora-contracts` `1e902dfaab5819c1d9ce785878d5b26dbeae48b3`: the storage profile now requires the Python SDK for the seven operations. The common Python binding map is copied and every storage entrypoint is checked from the installed wheel; the twelve storage runtime fixtures (requests for all seven operations, put success, get partial error) are executed through the runtime binding.
- A runtime get that fails after part of the transfer reached the host sink reports `partial`/`never`, as the `storage-get-partial-error` vector requires; with no byte delivered it stays `unknown`/`requires_recovery`. The adoption manifest declares no deviation.
- **Breaking (runtime wire and Rust API, 3.0.0)**, aligning the runtime binding with the matrix shared by the Plenora libraries (cells marked P follow proposals pending ratification in `plenora-contracts`):
  - every result carries a new `plenora.message.id` (a UUID v8 derived from the request identity) and `plenora.message.causation_id` set to the request's message identity; previously the result reused the request identity and copied its causation;
  - `RuntimeResultMetadata::operation`, `operation_version` and `correlation_id` become `Option<String>`: a rejection copies these routing values only when canonical and otherwise omits them, instead of writing `storage.unknown`, `"0"` or the nil UUID (P);
  - well-formed but unannounced capability, version, operation, input contract or content type are `unsupported` (`RUNTIME_ROUTE_UNSUPPORTED`), malformed or non-canonical ones `protocol` (`RUNTIME_ROUTE_INVALID`); every rejection before invocation is `validate`/`none`/`never` (P);
  - the idempotency control is read only from `plenora.execution.idempotency_key` (Runtime Binding 1.0 §4); `plenora.idempotency.key` is no longer accepted. A present key is `unsupported` (`RUNTIME_CONTROL_UNSUPPORTED`, RT-006), an empty one `protocol`;
  - the runtime deadline must be UTC: a non-zero offset such as `+02:00` (previously accepted) and `-00:00` are `protocol` rejections, while every RFC 3339 spelling of UTC (`Z`/`z`, `+00:00`, `T`/`t`, fractions) is accepted (P); a malformed deadline is `protocol` instead of `invalid_configuration`, one beyond the supported range `unsupported`;
  - `protocol` rejections of any malformed reserved key come before `unsupported` ones, then `timeout` (P, RT-018); `RuntimeBinding::invoke_json` accepts the serialized request as received and answers a missing reserved key or a non-string value with a `protocol` envelope instead of a deserialization error;
  - metadata keys Runtime Binding 1.0 does not reserve are ignored instead of failing deserialization (§9);
  - the binding executes the 21 rejection probes and the two storage cleanup vectors proposed by `plenora-contracts` pull request 21, copied and pinned as not yet normative;
  - a deadline already expired at admission is `timeout`/`validate`/`none` with retry `never` instead of `safe`, on every surface (P).
- A proved S3, SFTP or FTP publication whose metadata cannot be read back is `committed`/`cleanup` with retry `never` instead of `requires_recovery` (P); an FTP object published or downloaded with a size different from the transferred bytes keeps `requires_recovery` under the new code `FTP_COMMITTED_SIZE_MISMATCH`.
## 2.1.0 — 2026-09-30

- Add explicit private-file upload preparation for local, Azure, GCS, SMB and WebDAV through Rust, CLI and Python, preserving default buffered limits.
- Validate length, transfer bounds and checksum before publication; retain provider-specific conditional writes and error effects.
- Require separate private-file CLI/SDK, large-transfer and concurrency evidence, and exercise both upload strategies during the same two-hour soak.
- Bind fixture TLS certificates to the configured VM host and include the complete tracked documentation tree in CLI archives.
- Publish qualified Linux/Windows distributions after a complete two-hour soak, exact-artifact performance comparison on an isolated runner and downloaded-asset verification. Preserve earlier failed measurements in `docs/release-2.1.0.md`.

## 2.0.1 — 2026-09-29

- Reproduce MinIO disk pressure and recovery in a bounded disposable filesystem, preserving public error axes and requiring evidence for final Linux CLI bytes.
- Reserve filesystem headroom before transfer campaigns and check again on retries.
- Version the resumable Windows/VM qualification coordinator and publication controller; preserve failed attempts and verify source, configuration and artifact identity before resuming.
- Publish through GitHub Releases after final Linux/Windows qualification, a complete two-hour soak following a VM reboot, and downloaded-asset verification. See `docs/release-2.0.1.md` for artifact identities and the preserved interrupted attempt.

## 2.0.0 — 2026-09-28

- Implement the 2.0 maintainability plan while preserving the 1.0 Rust API baseline, CLI protocol and Python calls.
- Separate provider validation, transfer, error mapping and publication responsibilities; extract CLI command execution.
- Require public Rust documentation, scoped Clippy exceptions, product size budgets, source comment rules and explicit dependency pin policy in CI.
- Scan the exact CLI archives and Python wheels with pinned Syft; require artifact-bound native inventories alongside lockfile SBOMs for 2.0 qualification.
- Add migration guidance and current quality documentation. Publish qualified Rust sources, Linux/Windows CLI builds and Python wheels through GitHub Releases, with final artifact evidence and a two-hour soak.

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
