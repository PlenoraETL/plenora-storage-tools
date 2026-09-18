# Vendored SMB transport

Source: https://crates.io/crates/smb2/0.22.1 (MIT OR Apache-2.0).
Upstream: https://github.com/vdavid/smb2
Archive SHA-256: `c04c8f7cb2f27fbbd4d8d3839f5f1e197423613a94921bf83ec059147d6e6f16`.

This package preserves upstream source and licenses. Plenora changes: package name/version, excluded examples, and advertising SMB2_GLOBAL_CAP_ENCRYPTION during negotiation. The latter is required by Samba servers enforcing encryption, including with SMB 3.1.1. Encryption is implemented upstream but the capability flag was absent. Protocol behavior is tested against encryption-required Samba.

Keep this narrowly scoped fork until an upstream release incorporates the fix. Review upstream security advisories when updating dependencies; the renamed package is not matched automatically by RustSec under the upstream name.

Mechanical adjustments: rustfmt formatting and three scoped lint annotations for upstream test/platform code; no additional behavior changes.

In Plenora 0.2.2, dependency requirements were refreshed to current stable releases,
including CCM 0.6.1 (replacing the release candidate) and lz4_flex 0.14.0.
The declared Rust minimum follows the workspace at 1.92, with equivalent
`is_multiple_of` substitutions required by Clippy at that minimum. The upstream archive
hash above identifies the original source, not this modified package.

Tests: removed an upstream manual test hardcoded to an unrelated private NAS; added a wire-capability regression to the existing negotiation test. Plenora tests SMB against its dedicated Samba fixture.
