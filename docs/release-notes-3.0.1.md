# Plenora Storage Tools 3.0.1

Status: prepared on 2026-10-16; published only after the release campaign
qualifies the final artifacts.

3.0.1 is a patch release with no incompatible changes from 3.0.0 and no
product code changes beyond the dependency update. Inspection of
`git diff v3.0.0..fdcf14f` (the preparation base) shows only the rustls pin in
`Cargo.toml` and its version/checksum in the main and fuzz lockfiles.
The campaign tooling and deterministic SMB test entries carried forward from
Unreleased in the changelog were already included in `v3.0.0`.

- Update rustls 0.23.45 → 0.23.46 in both lockfiles and the workspace pin,
  through commits `0d72fd6` and `d5f56a7`.
- RUSTSEC-2026-0346: client session value `Debug` implementations expose TLS
  secrets. RUSTSEC-2026-0347: buffered application data can be sent to the
  outer server after ECH rejection.
- Exposure review: the product uses rustls for TLS but its FTPS and shared
  HTTP/S3 client construction does not enable ECH and its code does not format
  rustls client session values with `Debug`, so neither advisory's triggering
  path was found in the product configuration reviewed.

Public contracts, Rust/CLI/Python APIs, MSRV 1.98 (toolchain 1.98.1), licensing
and the [3.0.0 scope](release-notes-3.0.0.md) remain unchanged. Distribution
remains GitHub Releases only, for Linux/Windows x86_64 and Python 3.10–3.14.
The scope is the nine documented fixtures. Real AWS, Azure and GCS accounts
remain unqualified; WebDAV retains the serialized WsgiDAV fixture restriction.
The [operational limits](release.md#ambito-e-limiti-operativi) still apply.

Qualification requires new evidence on the final 3.0.1 artifacts, including
the full two-hour soak in both transfer modes. 3.0.0 receipts do not qualify
3.0.1. The [campaign](release-campaign.md#campagna-301) compares the verified
3.0.0 Linux CLI baseline with the final 3.0.1 candidate in a new paired
32-round ABBA measurement, under the existing performance policy.
