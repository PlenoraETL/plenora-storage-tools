# Plenora Storage Tools 2.1.0

This release adds explicit disk preparation for large uploads and copies on
local, Azure, GCS, SMB and WebDAV. Existing calls retain their buffered limits.

- Rust: `build_engine_with_upload_strategy` and `UploadStrategy::PrivateFile`.
- CLI: `--spool-uploads`.
- Python: `Engine(spool_uploads=True)` and `AsyncEngine(spool_uploads=True)`.

Preparation checks length, transfer bounds and checksum before publication.
It requires temporary disk space for the complete source; it does not provide
resumable transfers or an aggregate disk quota. Azure uses a conditional single
Put Blob, bounded to 5,000 MiB. Provider-specific overwrite and atomicity limits
remain documented in the [large-transfer guide](large-transfers.md).

Qualification requires separate CLI and installed SDK reports, 1 GiB transfers,
concurrency measurements, fault injection and both upload modes within the same
two-hour soak against the final artifacts. The published receipt identifies the
qualified bytes; development reports do not replace this receipt.

Distribution remains GitHub Releases only, for Linux/Windows x86_64 and Python
3.10–3.14. The scope is the nine documented fixtures. Real AWS, Azure and GCS
accounts remain unqualified; WebDAV retains the serialized WsgiDAV fixture
restriction. See the release receipt and checksums for publication evidence.
