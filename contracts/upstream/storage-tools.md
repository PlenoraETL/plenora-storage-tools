# Public profile: storage-tools

Profile identifier: `plenora-storage-tools-profile-v1`

Status: normative

Normative operation catalog:
[`storage-tools-v1.json`](../catalogs/storage-tools-v1.json)

## Applicable contracts

- [Public Surfaces 1.0](../specs/surfaces/PUBLIC-SURFACES-1.0.md)
- [Capability Discovery 2.0](../specs/capabilities/CAPABILITY-DISCOVERY-2.0.md)
- [Typed Errors 1.0](../specs/errors/ERRORS-1.0.md)
- [Public Security 1.0](../specs/security/PUBLIC-SECURITY-1.0.md)
- [CLI 2.0](../specs/cli/CLI-2.0.md)
- [Python SDK 1.0](../specs/sdk/PYTHON-SDK-1.0.md)
- [Runtime Binding 1.0](../specs/runtime/RUNTIME-BINDING-1.0.md)
- [Surface Bindings 1.0](../specs/surfaces/SURFACE-BINDINGS-1.0.md)

## Current contract boundary

The v1 profile selects seven operations: `storage.test`, `storage.list`,
`storage.stat`, `storage.get`, `storage.put`, `storage.copy` and
`storage.delete`. Every operation uses immutable component-owned JSON input
and output identifiers, operation version 1, cancellation and deadline. No v1
operation accepts an idempotency key.

This normative selection is not an artifact conformance claim. The component
owns the immutable operation and capability-attribute schemas referenced by
the catalog. Its adoption manifest identifies the tested contracts revision,
artifact versions and digests, verification commands and deviations according
to [ADOPTION.md](../ADOPTION.md). Provider qualification is separate evidence;
this profile does not certify any provider or service deployment.

## Operation semantics

`test`, `list` and `stat` declare `side_effect: none`. `put`, `copy` and
`delete` declare `remote`. `get` also uses the conservative `remote` class:
the storage read is non-mutating, but its authorized artifact sink may be
externally visible.

`get` and `put` carry no bytes inside the JSON envelope. Runtime requests use
opaque `artifact://` references; a consumer-owned adapter resolves them into
a sink or source under the consumer's authority. Source, sink and
transfer result carry bounded metadata for content type, size and optional
SHA-256. Persisted runtime envelopes contain neither local paths nor inline
credentials. Every destination requires an explicit `overwrite` value, put
and copy require an explicit `publication_policy`, and delete requires an
explicit missing-object policy.

Conformance note (informative): this repository's gate checks the storage
runtime vectors positively for artifact references (`artifact://` with a
bounded opaque handle) and for `credential_ref`, and heuristically for private
paths and inline credentials elsewhere: a string that is a valid opaque
reference (the `reference` grammar of `data-execution-input-v3.schema.json`) is
never judged as a path; any other string is a path when it has a drive, root,
UNC, home, `.`/`..`, `file:`, environment-variable or backslash spelling; a
member name is a credential when, after NFKC and case folding and without `_`,
`-`, `.` and spaces, it contains a secret word; authorization and PEM values
are credentials under any name. A relative path with forward slashes or none
(`dir/report.csv`, `report.csv`) in a free field, or a secret spelled
otherwise, can pass that heuristic; the property itself is shown by the
adopter's boundary tests, not by the gate.

`overwrite=false` is permitted only when the provider guarantees atomic
create-if-absent for that specific operation; put and copy support are
advertised separately. Otherwise it is rejected before mutation. S3 uses a
qualified native conditional primitive for put and rejects conditional copy
when that primitive is unavailable. SFTP can publish through temporary-name plus
rename when the connection qualifies that primitive. FTP does not advertise
atomic create-if-absent, rejects `overwrite=false`, documents non-atomic
publication and rejects `atomic_required`. There is no check-then-write
fallback.

Timeout and cancellation are cooperative and do not prove rollback. If a
write, copy, delete or artifact publication may have started, an unproven
outcome reports `remote_effect: unknown` with conservative retry or recovery.
A definitely partial sink may report `partial` and still forbids automatic
retry.

## Public surfaces

Rust, CLI, Python SDK and runtime are required for all seven operations. The
component owns the Rust operation-to-public-export mapping; the common
[binding maps](../bindings/) define CLI, Python and runtime entrypoints.
Capability Discovery reports
only the surface of the answering artifact and only providers and operations
that are actually present. Experimental operations require explicit opt-in.

The final consumer owns runtime transport and authorization. The runtime
boundary validates routing, resolves authorized artifacts and secret
references, honors deadline/cancellation, and returns a complete versioned
result or `plenora-error-v1` while preserving correlation and contract identity.

### Python SDK

The distribution is `plenora-storage`, imported as `plenora_storage`. Each
operation binds to both `Engine.<action>` and `AsyncEngine.<action>`.
`plenora_storage.version`, `Engine.capabilities` and `AsyncEngine.capabilities`
provide version and discovery. Both modes preserve operation policies, result
meaning and typed error axes as required by Python SDK 1.0.

Python `get` and `put` accept process-local output and input paths respectively;
they are idiomatic local bindings, not serialized runtime artifact references.
Paths must not be copied into persisted runtime envelopes. `overwrite`,
`publication_policy` and `ignore_missing` remain explicit where applicable.
Closing a client follows the common SDK lifecycle contract. Cancellation is
not evidence of rollback; any exposed settled outcome preserves the operation's
result or typed error semantics.

## Pagination and integrity

The `storage.list` cursor is opaque, bounded to 512 bytes and scoped to
provider, connection, prefix and request parameters. Reuse under another scope
fails closed. Cursors may expire or be invalidated by resource eviction, close
or restart; consumers must handle their rejection. Cache capacity and retention
are component-owned policies. Pagination does not provide snapshot isolation
during concurrent mutations.

ETag, provider version ID and SHA-256 remain distinct optional fields. Neither
ETag nor version is defined as a digest, missing values are not synthesized,
and end-to-end integrity is asserted only through the separate SHA-256 field
when it was actually calculated.

## Interchange and composition

The JSON contracts describe control and result metadata, not the transferred
object's semantic content. Therefore no direct composition edge is declared
from an artifact reference alone. An edge may be added only after producer and
consumer share a reviewed contract for the transferred content. No storage
composition edge is selected by this profile.

## Provider-independent decisions

- Rust and CLI are required.
- Runtime Binding 1.0 is required for all seven operations.
- Python SDK 1.0 is required, with sync and async bindings for all seven operations.
- Idempotency keys are unsupported in v1.
- Timeout or cancellation after mutation begins does not imply rollback and
  reports `remote_effect: unknown` with `retry: requires_recovery` when the
  provider cannot prove the result.

The profile does not select providers, clients, multipart strategies, caches
or credential implementations. The compatible Python surface addition and its
adoption impact are recorded in
[decision 0006](../decisions/0006-storage-python-surface.md). Previously pinned
adoption manifests remain evidence for their original revision and artifacts.
