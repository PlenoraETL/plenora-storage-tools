# Vendored SMB transport

Source: https://crates.io/crates/smb2/0.22.1 (MIT OR Apache-2.0).
Upstream: https://github.com/vdavid/smb2
Archive SHA-256: `c04c8f7cb2f27fbbd4d8d3839f5f1e197423613a94921bf83ec059147d6e6f16`.

This package preserves upstream source and licenses. Plenora changes: package name/version, excluded examples, and advertising SMB2_GLOBAL_CAP_ENCRYPTION during negotiation. The latter is required by Samba servers enforcing encryption, including with SMB 3.1.1. Encryption is implemented upstream but the capability flag was absent. Protocol behavior is tested against encryption-required Samba.

Keep this narrowly scoped fork until an upstream release incorporates the fix. Review upstream security advisories when updating dependencies; the renamed package is not matched automatically by RustSec under the upstream name.

Mechanical adjustments: rustfmt formatting and three scoped lint annotations for upstream test/platform code.

In Plenora 0.2.2, dependency requirements were refreshed to current stable releases,
including CCM 0.6.1 (replacing the release candidate) and lz4_flex 0.14.0.
The declared Rust minimum follows the workspace (1.92 at the time; 1.98 from Plenora 3.0.0), with equivalent
`is_multiple_of` substitutions required by Clippy at that minimum. The upstream archive
hash above identifies the original source, not this modified package.

Tests: removed an upstream manual test hardcoded to an unrelated private NAS; added a wire-capability regression to the existing negotiation test. Plenora tests SMB against its dedicated Samba fixture.

In Plenora 1.0.0-alpha.1, the receiver task retains a weak connection reference
while waiting for network input. The upstream strong reference prevented the
last connection owner from releasing the idle socket. A persistent SDK campaign
exposed one retained socket per SMB operation. The regression checks that TCP
stays open while another clone exists and that the peer observes EOF after the
final clone drops, without requiring the server to initiate teardown. The
shared transport fix applies to every SMB operation and to Rust, CLI and Python.

In Plenora 3.0.0 the fork follows the workspace rules instead of an exemption.
It inherits the workspace lints and edition 2024; the pedantic and nursery lints
upstream code still violates are listed, with their reason, at the top of
`src/lib.rs`, and every other lint applies. Its dependencies are exact pins with
a motivation, or inherited from the workspace where they cross the public API
boundary, and `scripts/check_dependencies.py` no longer skips this manifest.
Library code contains no `unwrap`, `expect`, `panic!` or `unreachable!`
(`scripts/check_anti_panic.py`): a poisoned connection lock, a failing random
source, an exhausted nonce counter, an AES key of the wrong length from a KDC
reply and the other former panics are typed errors (`Error::Internal`,
`Error::InvalidData`). The fallible accessors of `Connection` and the Kerberos
and KDF helpers now return `Result`, and `MockTransport` is compiled only for
tests or with the `testing` feature. These are API changes of the fork, part of
the Plenora 3.0.0 major release.
