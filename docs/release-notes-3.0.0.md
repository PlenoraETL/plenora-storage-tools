# Plenora Storage Tools 3.0.0

Status: in preparation, not published. Publication requires the release
campaign and a separate approval.

3.0.0 is a major release: it rejects inputs that 2.1 accepted and changes some
public Rust signatures. The [migration guide](migration-3.0.md) lists every
change a consumer may need to make.

- Wire: `null` is no longer read as absent for optional runtime metadata and
  for `tls_ca_pem`; nullable result keys must be present.
- Runtime binding aligned with the common Plenora matrix: new result
  `message.id` with the request as causation, `unsupported` for well-formed
  but unannounced values and `protocol` for malformed ones, invalid routing
  values omitted from rejections, `plenora.execution.idempotency_key`
  rejected as unsupported, UTC-only deadlines with `never` retry when
  expired, `partial`/`never` for a get that delivered bytes before failing,
  and `committed` when only the post-publication metadata read fails. Cells
  that follow the draft `plenora-contracts#21` are marked as proposals in the
  [adoption matrix](contract-adoption.md).
- Contracts: `plenora-contracts` `1e902dfaab5819c1d9ce785878d5b26dbeae48b3`,
  with all twelve storage runtime vectors executed.
- Rust: `StorageError::execution_id` accepts the optional field of
  `plenora-error-v1`; `RuntimeResultMetadata` routing fields are optional;
  `plenora-smb2` reports poisoned state, random source and cryptographic
  failures as typed errors instead of panicking, bounds Kerberos ccache
  allocations, and `MockTransport` requires the `testing` feature.
- Python: `PlenoraError.execution_id`.
- Supply chain: weekly dependency audit and fuzzing, including new SMB2,
  SPNEGO, NTLM and Kerberos targets; SLSA provenance and SBOM attestations for
  CLI archives, wheels and sources; SBOM and adoption manifest published as
  separate assets.
- License: proprietary Plenora ETL license instead of MIT OR Apache-2.0; the
  `plenora-smb2` fork keeps its upstream license.

Distribution remains GitHub Releases only, for Linux/Windows x86_64 and Python
3.10–3.14. The scope is the nine documented fixtures. Real AWS, Azure and GCS
accounts remain unqualified; WebDAV retains the serialized WsgiDAV fixture
restriction. Qualification requires new evidence on the final 3.0.0 artifacts,
including the two-hour soak; 2.1.0 receipts do not qualify this release.
