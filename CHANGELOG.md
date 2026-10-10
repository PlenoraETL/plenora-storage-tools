# Changelog

## Unreleased

- Release campaign tooling: the VM runner measures baseline and candidate in one `performance-ab` phase of 32 rounds, alternated per provider in an ABBA order on its own round index and recorded in both reports, which reduces the effect of a slow environment drift; a stability guard marks the measurement `UNRELIABLE` (exit code 3, never PASS) when either binary moves against itself between the halves of the run by more than 5 % with a 10 ms floor (`stability_allowance` in the policy, motivated by the real 2.1.0 and 3.0.0 runs). The regression comparison, budget and criteria are unchanged; malformed pairing metadata is rejected, and from 3.0.0 the evidence bundle requires a paired run. Every `qualify-vm` attempt starts on recreated fixtures under the VM runner lock, with a unique nonce per preparation in its signal files: it refuses while a runner or preparation is active, archives fixture state, logs and certificates (failing if it cannot), records the reset only when every fixture completes an authenticated application exchange (`scripts/check_fixtures.py`; healthchecks for FTPS, WebDAV and SMB do the same), and the runner measures only if the fixtures were last prepared by that reset. A retry of a passed, unknown or not executed phase, local or VM, is refused with an error, and VM retries require a new `qualify-vm` attempt. One campaign at a time works on a VM root, whatever revision it qualifies (`scripts/campaign_fence.py`): a controller is admitted only with the exclusive `flock` of `<vm_root>/.campaign/lock`, which every activity that touches inputs, fixtures or measurements holds shared for its whole life, and while no labelled runner container exists. Every admission replaces an epoch token (counter with overflow check and a random part) that activities check before starting, before every write and at the end of every measured phase; a missing, unreadable or malformed epoch is an error, never a fresh start. Each VM attempt uploads its inputs into a directory of its epoch and the runner checks the digest of every input before and after every phase; the runner measures only private copies of its inputs, verified against those digests, in the container's own filesystem. Selected evidence must match the digests recorded when its phases passed; the controller requires every downloaded file to be listed with its digest in a report bound to the attempt's epoch, fixture reset and runner ledger, re-verifies the distributions before sealing, and seals only evidence of its own binaries; the VM root must be private to the campaign user (checked before every admission), the runner starts only from its own checkout of the verified source bundle with verified fixture files, keeps its ledger and evidence in the container and hands them to the controller through `docker cp`, and every VM attempt measures every phase (`--vm-retry-phase` is removed); the threat model, and what it leaves out, is in `docs/release-campaign.md`. The Windows qualification runs in a job object (a process group on POSIX) whose whole tree is killed, and verified gone within a bounded time, when the admission channel or its lease ends; an expired lease is never renewed while that tree runs; an admission after a lost channel waits for the previous lease, and every preparation recreates the fixture containers. A refusal for contention exits with code 75 before touching anything; other lock and Docker failures keep their own error. Every preparation marks the fixtures `in-progress` before changing them, so a failed preparation invalidates the previous reset. The fake GCS fixture runs on its in-memory backend: the filesystem backend kept the empty prefix directories of deleted objects and listing slowed down with every round. Its peak, about 2 GiB in the spooled 1 GiB transfers, is checked against available memory before the reset and before `spooled-large` (`scripts/check_memory.py`). No product code changes.
- Make the plenora-smb2 KDC UDP-to-TCP fallback test deterministic: the exchange takes the UDP and TCP endpoints separately internally (`send_to_kdc` still uses one address for both), so the test binds its two mock servers on independent ephemeral ports instead of retrying to find one port free for both protocols, which failed intermittently on Windows (WSAEACCES).

## 3.0.0 — 2026-10-06

Major release: rejects inputs accepted by 2.1 and changes public Rust signatures; see `docs/migration-3.0.md`.

- Replace the yanked `yoke-derive` 0.8.3 with 0.8.4 in the workspace and fuzz lockfiles; `cargo deny` now rejects yanked crates like `cargo audit --deny warnings`.
- Reject `null` for optional runtime metadata (`plenora.execution.deadline`, `plenora.message.causation_id`; the idempotency key is covered by the runtime binding entry below) instead of reading it as absent; a null deadline previously started the operation without a deadline. Absent values are no longer serialized as `null`.
- Require the `content_type`, `size` and `sha256` keys of artifact metadata (nullable but present, as in `plenora-storage-common-v1`) and the nullable keys of object, list and transfer results when deserializing.
- Reject `tls_ca_pem: null` in FTP/FTPS connections; the schema types the key as a string.
- Execute the storage fixtures of RUNTIME-VECTORS-1.0, copied byte for byte from the adopted contracts revision and pinned by SHA-256, through the runtime binding, including fail-closed routing mutations.
- Reject non-canonical runtime version selectors such as `01` and `+1`, which were parsed as `1` and dispatched.
- Pin the SDK build backend to `maturin==1.15.0` and list the transitive dependencies of the quality and campaign requirement files with exact versions; `check_dependencies.py` now rejects unpinned Python requirements.
- Distribute the workspace crates, CLI archives, source bundle and Python SDK under the proprietary Plenora ETL license (`LICENSE`), replacing MIT OR Apache-2.0; `cargo deny` binds each workspace crate to the hash of that text. The `plenora-smb2` fork keeps its upstream MIT OR Apache-2.0 license and texts.
- Reject panic primitives in the library code of every crate: `scripts/check_anti_panic.py` (`cargo clippy --workspace --lib` with `unwrap_used`, `expect_used`, `panic`, `unreachable`, `todo`, `unimplemented` and `unsafe-code` denied) runs in CI on Linux and Windows and in `verify.sh`.
- Replace the 225 panic primitives of the vendored `plenora-smb2` with typed errors. A connection lock poisoned by an earlier panic now fails callers with `Error::Internal` instead of panicking every later operation, while teardown still fails parked waiters; whole-value settings use a lock that cannot be left half-updated. A failing OS random source, an exhausted encryption nonce counter, an SMB 3.1.1 key derivation without preauth hash and a Kerberos AES key of the wrong length from a KDC reply are errors instead of panics. The SMB provider maps `Error::Internal` to the `internal` category and no longer ignores a failure to activate encryption.
- Breaking (Rust, `plenora-smb2`): `Connection` accessors that read connection state (`session_id`, `set_session_id`, `activate_signing`, `activate_encryption`, `should_encrypt`, `preauth_hasher`, `with_preauth_hasher_mut`, `register_dfs_tree`, `deregister_dfs_tree`, `outstanding_requests`, `diagnostics`, `from_transport`), `SmbClient::diagnostics`, `NonceGenerator::next`, `sp800_108_kdf`, `derive_session_keys` and the public Kerberos crypto helpers return `Result`; `Error::Internal` and `ErrorKind::Internal` are new; `transport::MockTransport` requires the `testing` feature.
- Bring `plenora-smb2` under the workspace rules: workspace lints and edition 2024, with the upstream style lints it still violates listed and justified in `src/lib.rs`; exact dependency pins with motivations, or workspace inheritance on the public API boundary; `check_dependencies.py` no longer exempts its manifest.
- Choose the Kerberos cipher from the declared etype, never from the key length: a session key, server subkey or credential-cache key whose length differs from its etype (for example 16 bytes declared AES-256, previously run as AES-128) is rejected as `InvalidData`. The AS session key's own `keytype` now governs the TGS exchange, and a TGS-REP or AP-REP enc-part declared with an etype other than the key protecting it is rejected.
- Run the dependency audit (`cargo audit` on the three lockfiles, `cargo deny`, SMB upstream name audit) and a 300-second-per-target parser fuzz campaign every Monday through the `scheduled` workflow; the per-push jobs are unchanged and the audit job now lives in the reusable `dependencies` workflow.
- Fuzz the SMB2 framing and message decoders, DER/SPNEGO, the NTLM CHALLENGE_MESSAGE and Kerberos KDC/AP replies and credential caches with four new cargo-fuzz targets and versioned seeds.
- Attest SLSA build provenance and the CycloneDX SBOM of CLI archives, wheels and sources from the release-candidate jobs that build them; the release workflow refuses to publish files without both attestations and publishes each target's SBOM and contract adoption manifest as release assets.
- Refuse a Kerberos credential cache whose principal claims more components than its bytes can hold, and address or authorization-data lengths past the end of the file, instead of reserving memory for the claimed count (the process aborted) or skipping beyond the data; found by the new `kerberos_messages` fuzz target.
- **Breaking (plenora-smb2 Rust API, 3.0.0):** whole-file reads whose buffer is sized by the server-declared file size take an explicit `max_bytes` limit: `Tree::read_file_pipelined`, `Tree::read_file_pipelined_with_progress`, `SmbClient::read_file_pipelined`, `SmbClient::read_file_with_progress`, `FileDownload::collect` and `FileDownload::collect_with_progress`. A larger declared size, or a server sending more than the limit, fails with the new `Error::DeclaredSizeOverLimit` (`ErrorKind::TooLarge`) before memory is reserved; the reservation itself is fallible. Previously a forged size aborted the process on allocation. The product reads SMB through `FileReader` and was not affected.
- plenora-smb2 decoders check element counts received from the peer against the remaining bytes before reserving: SMB2 NEGOTIATE dialects and contexts, LOCK elements, server-side copychunk descriptors and srvsvc share entries (a forged `u32` count previously reserved up to about 100 GB).
- Keep compatible requirements for the six dependencies that cross the public Rust API (`async-trait`, `bytes`, `serde`, `serde_json`, `tokio`, `tokio-util`) as the declared deviation `DEP-RANGE-1` (rule, scope, hazard, re-entry in `scripts/dependency-policy.json`), with each motivation next to its line in `Cargo.toml`; `check_dependencies.py` rejects a range without it.
- **Breaking (Rust API, 3.0.0):** `StorageError` gains the public field `execution_id: Option<ExecutionId>`, so struct literals and exhaustive patterns must name it. A `plenora-error-v1` document carrying `execution_id` was rejected by `deny_unknown_fields`; it is now accepted when it is a string of 1 to 128 characters or `null`, and other lengths are refused. The key is serialized only when present, so errors produced by this component are unchanged on the wire. Python `PlenoraError` exposes the same optional `execution_id` attribute.
- Adopt `plenora-contracts` `1e902dfaab5819c1d9ce785878d5b26dbeae48b3`: the storage profile now requires the Python SDK for the seven operations. The common Python binding map is copied and every storage entrypoint is checked from the installed wheel; the twelve storage runtime fixtures (requests for all seven operations, put success, get partial error) are executed through the runtime binding.
- A runtime get that fails after part of the transfer reached the host sink reports `partial`/`never`, as the `storage-get-partial-error` vector requires; with no byte delivered it stays `unknown`/`requires_recovery`. The adoption manifest declares no deviation.
- `Engine::get` reports a sink that accepts part of the transfer and then fails (write or flush) as `STORAGE_GET_SINK_PARTIAL` with `partial`/`never`, for every provider, keeping the category and phase of the sink failure (`io`/`write` for an I/O error, `resource_limit` for a full disk); providers previously reported their own write-failure code with `unknown`/`requires_recovery`. CLI and Python downloads keep staging: the staged file is removed, so their error reports the same code with `rolled_back`.
- **Breaking (runtime wire and Rust API, 3.0.0)**, aligning the runtime binding with the matrix shared by the Plenora libraries (cells marked P follow proposals pending ratification in `plenora-contracts`):
  - every result carries a new `plenora.message.id`, a version 4 UUID from the operating system's random source (never derived from the request, so requests without a usable identity no longer share one), and `plenora.message.causation_id` set to the request's message identity; previously the result reused the request identity and copied its causation. `RuntimeBinding::invoke` and `invoke_json` return `StorageResult<RuntimeResultEnvelope>`: they fail only with `RUNTIME_RESULT_IDENTITY_UNAVAILABLE` (`internal`/`validate`/`none`) when the random source is unavailable, before anything is invoked;
  - `RuntimeResultMetadata::operation`, `operation_version` and `correlation_id` become `Option<String>`: a rejection copies these routing values only when canonical and otherwise omits them, instead of writing `storage.unknown`, `"0"` or the nil UUID (P);
  - well-formed but unannounced capability, version, operation, input contract or content type are `unsupported` (`RUNTIME_ROUTE_UNSUPPORTED`), malformed or non-canonical ones `protocol` (`RUNTIME_ROUTE_INVALID`); every rejection before invocation is `validate`/`none`/`never` (P);
  - the idempotency control is read only from `plenora.execution.idempotency_key` (Runtime Binding 1.0 §4); `plenora.idempotency.key` is no longer accepted. A present key is `unsupported` (`RUNTIME_CONTROL_UNSUPPORTED`, RT-006), an empty one `protocol`;
  - the runtime deadline must be UTC: a non-zero offset such as `+02:00` (previously accepted) and `-00:00` are `protocol` rejections, while every RFC 3339 spelling of UTC (`Z`/`z`, `+00:00`, `T`/`t`, fractions) is accepted (P); a malformed deadline is `protocol` instead of `invalid_configuration`, one beyond the supported range `unsupported`;
  - `protocol` rejections of any malformed reserved key come before `unsupported` ones, then `timeout` (P, RT-018); `RuntimeBinding::invoke_json` accepts the serialized request as received and answers a missing reserved key or a non-string value with a `protocol` envelope instead of a deserialization error;
  - metadata keys Runtime Binding 1.0 does not reserve are ignored instead of failing deserialization (§9);
  - the binding executes the 21 rejection probes and the two storage cleanup vectors proposed by `plenora-contracts` pull request 21, copied and pinned as not yet normative;
  - a deadline already expired at admission is `timeout`/`validate`/`none` with retry `never` instead of `safe`, on every surface (P).
- A proved S3, SFTP or FTP publication whose metadata cannot be read back is `committed`/`cleanup` with retry `never` instead of `requires_recovery` (P); an FTP object published or downloaded with a size different from the transferred bytes keeps `requires_recovery` under the new code `FTP_COMMITTED_SIZE_MISMATCH`.
- **Breaking (MSRV):** raise the Rust toolchain to 1.98.1 and the declared minimum (`rust-version`) to 1.98, aligned with the other Plenora libraries; Docker images use `rust:1.98.1-bookworm` pinned by digest. API snapshots are regenerated with 1.98.1: the differences are auto-trait and std path rendering only, not crate signatures.
- Give both CycloneDX inventories a `serialNumber` (a UUID derived from the SHA-256 of the inventory, so it is deterministic and changes with the content). `actions/attest` requires it: without it the first 3.0.0 release candidate was refused as "Unsupported SBOM format".
- Classify login and session-setup failures by cause; this defect was already present in 2.1.0 and earlier. FTP/FTPS: only replies 430, 530 and 532 are `authentication`/`never` (`FTP_AUTHENTICATION_FAILED`); any other 4xx reply such as pure-ftpd's `421 ... users (the maximum) are already logged in` is `transient`/`connect`/`none` with retry `safe` (`FTP_LOGIN_TEMPORARILY_REFUSED`), other replies and any reply that is not well formed under RFC 959 (code of exactly three digits, multiline reply closed by the same code) are `protocol` (`FTP_LOGIN_UNEXPECTED_RESPONSE`) and transport failures follow the general mapping; previously every login failure was `FTP_AUTHENTICATION_FAILED` with retry `never`. SMB negotiate and session setup report phase `connect`: logon-rejection statuses are `authentication`, resource refusals, lost connections and timeouts are retryable (`safe`), and local authentication-exchange failures are `protocol` instead of `authentication`. WebDAV 429 and 502–504 are `transient` (retry `safe` without a mutation) instead of `protocol`/`never`. SFTP already separated rejected credentials from transport failures. The FTP fixture now allows 48 sessions over 100 passive ports: with 64 passive ports pure-ftpd accepted only 32, which failed the 16-worker qualification of the first 3.0.0 campaign.
- SFTP requests wait for the server as long as the operation deadline allows, instead of the SFTP client library's fixed 10-second request timeout, which failed a slow but correct `fsync` of a 1 GiB upload in the first qualification campaign of 3.0.0 (`SFTP_TRANSFER_IO_FAILED`); the defect was present in 2.1.0 and earlier. Without a deadline a single unanswered request is limited to the declared `REQUEST_TIMEOUT_WITHOUT_DEADLINE` (300 s). An unanswered request is now `timeout` (`SFTP_TIMEOUT`) with the effect of the operation (`unknown`/`requires_recovery` after a write) instead of `io`; other stream failures stay `io`.
- HTTP (WebDAV, Azure, GCS), S3 and SMB requests wait for the server as long as the operation deadline allows, instead of fixed per-request limits that ignored it (HTTP 60 s and S3 30 s for the whole request, body included; SMB 30 s of silence); the defect was present in 2.1.0 and earlier. Connection setup stays bounded by 10 s (HTTP) or 5 s (S3, SMB), or by the time remaining when shorter. Without a deadline the declared limits apply. Every HTTP and S3 request, read or write, fails after 300 s of inactivity (`HTTP_READ_TIMEOUT_WITHOUT_DEADLINE`, re-exported by `plenora-storage-providers`, and `READ_TIMEOUT_WITHOUT_DEADLINE`): the clock is re-armed only by real data, every non-empty piece of the request body the transport takes (at most 64 KiB) and every non-empty piece of the answer (empty frames, trailers, the end of a body and errors do not count), and keeps running while the answer is awaited, so a transfer that keeps moving in either direction is never cut and a server that stops reading or answering is. A piece counts once the transport has taken it, not once the server has it. No body is ever sent twice: the internal retries of `object_store` are set to zero and reqwest retries are disabled. reqwest's own read timeout is no longer used: it starts with the request and is not re-armed by the bytes sent, so it cut uploads still moving. All requests, including mutations without a body such as `UploadPartCopy` and the multipart completion, go through the same adapter, so no routing decides which rule applies. SMB allows 30 s of silence (`SMB_RESPONSE_TIMEOUT_WITHOUT_DEADLINE`, re-exported, 180 s on a connection the keepalive proves alive). A request a client gives up on is now `timeout` (none/safe without a mutation, unknown/requires_recovery during one) instead of `io`; for SMB this includes `ServerUnresponsive`, send timeouts and a credit wait that runs out. A request the credits of an SMB connection cannot fund, with no response left that could grant more, fails at once as `resource_limit` (`SMB_CREDITS_EXHAUSTED`); `plenora-smb2` reports it as the new `Error::CreditsExhausted` instead of `Error::CreditStarvation`, which now means only a credit wait that ran out of time. A TCP connection that ran out of time on every address is `timeout` in `connect` (none/safe); a refusal or any other failure on any address is `io`, whatever the order of the addresses. The S3 multipart cleanup keeps its own 10 s budget and may end up to 10 s after the deadline. `ExecutionControl::remaining` exposes the time left; `plenora-storage-core` gains `Inactivity` and `InactivityTimeout`; `plenora-smb2` gains `Connection::response_timeout` and `Error::CreditsExhausted`, and measures response waits on the tokio clock, so paused-time tests can drive them.

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
