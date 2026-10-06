# Plenora Storage Tools 3.0.0

Status: prepared on 2026-10-06; published only after the release campaign
qualifies the final artifacts.

3.0.0 is a major release: it rejects inputs that 2.1 accepted, changes the
runtime binding wire, changes public Rust signatures and raises the minimum
Rust version. The [migration guide](migration-3.0.md) lists every change a
consumer may need to make.

- Contracts: `plenora-contracts` `1e902dfaab5819c1d9ce785878d5b26dbeae48b3`,
  with all twelve storage runtime vectors executed and no declared deviation.
- Wire: `null` is no longer read as absent for optional runtime metadata and
  for `tls_ca_pem`; nullable result keys must be present.
- Runtime binding aligned with the common Plenora matrix: a new random
  result `message.id` with the request as causation, `unsupported` for
  well-formed but unannounced values and `protocol` for malformed ones,
  invalid routing values omitted from rejections,
  `plenora.execution.idempotency_key` rejected as unsupported, UTC-only
  deadlines with `never` retry when expired, and `committed` when only the
  post-publication metadata read fails. Cells that follow the draft
  `plenora-contracts#21` are marked as proposals in the
  [adoption matrix](contract-adoption.md).
- Get: a sink that accepts part of the transfer and then fails is
  `STORAGE_GET_SINK_PARTIAL` with `partial`/`never` on every provider and
  surface, keeping the cause of the sink failure.
- Rust: `StorageError::execution_id`; `RuntimeBinding::invoke` and
  `invoke_json` return `StorageResult`; `RuntimeResultMetadata` routing fields
  are optional; MSRV 1.98, toolchain 1.98.1.
- `plenora-smb2`: no panic primitives in library code (poisoned state, random
  source and cryptographic failures are typed errors); Kerberos ciphers
  follow the declared etype; whole-file reads take an explicit `max_bytes`
  and refuse a larger server-declared size before allocating; decoders check
  peer counts against the received bytes; `MockTransport` requires the
  `testing` feature.
- Python: `PlenoraError.execution_id`.
- Quality and supply chain: an anti-panic Clippy gate on every library;
  weekly dependency audit and fuzzing, including new SMB2, SPNEGO, NTLM and
  Kerberos targets; SLSA provenance and SBOM attestations for CLI archives,
  wheels and sources; SBOM and adoption manifest published as separate
  assets; the six public-boundary dependency ranges declared as deviation
  `DEP-RANGE-1`.
- License: proprietary Plenora ETL license instead of MIT OR Apache-2.0; the
  `plenora-smb2` fork keeps its upstream license.

Distribution remains GitHub Releases only, for Linux/Windows x86_64 and Python
3.10–3.14. The scope is the nine documented fixtures. Real AWS, Azure and GCS
accounts remain unqualified; WebDAV retains the serialized WsgiDAV fixture
restriction. Qualification requires new evidence on the final 3.0.0 artifacts,
including the two-hour soak; 2.1.0 receipts do not qualify this release.
