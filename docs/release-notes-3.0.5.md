# Plenora Storage Tools 3.0.5

Status: prepared on 2026-11-11; unreleased and not yet qualified. Workspace
manifests, lockfiles and generated state identify 3.0.5. This candidate requires
its own evidence tied to the merged commit and final artifact digests.

3.0.5 is a patch release with no incompatible changes from 3.0.3 and no change
to the public operations. It includes the changes prepared for 3.0.4 and the
release workflow correction (#44).

3.0.4 was qualified but not published: tag `v3.0.4` and its GitHub draft remain
unpublished. Publication was blocked because the jobs that recheck Cargo
archives lacked the pinned Rust toolchain and populated registry cache needed
to regenerate metadata offline. The 3.0.4 qualification does not qualify 3.0.5;
its artifacts, tag, draft and historical notes are not rewritten or republished.

- Align serde_json to 1.0.154 and the SDK build backend to maturin 1.16.0 (#40).
- Generate synthetic Kerberos test credentials at runtime instead of shipping
  saved caches; enforce exact recursive package inventories (#41).
- Require every packaged source member to match the release Git checkout,
  and reproduce Cargo and wheel generated metadata for comparison (#42).
- Install pinned Rust 1.98.1 and run `cargo fetch --locked` on the tagged checkout
  in both `qualify` and `publish` before recursive package verification (#44).
- Build Cargo archives in CI and recheck them on a separate Ubuntu 24.04 runner;
  workflow tests keep its prerequisites aligned with the publication jobs (#44).

## Dependency scope

| Component | Previous published release (3.0.3) | Candidate |
| --- | --- | --- |
| serde_json | 1.0.151 | 1.0.154 |
| maturin | 1.15.0 | 1.16.0 |
| tokio / tokio-util | 1.53.3 / 0.7.19 | unchanged |
| tokio-rustls / hyper / h2 | 0.26.5 / 1.11.1 / 0.4.19 | unchanged |
| time / thiserror | 0.3.55 / 2.0.21 | unchanged |
| rustls / ring | 0.23.46 / 0.17.14 | unchanged |
| aws-lc-rs / aws-lc-sys | 1.18.1 / 0.45.0 | unchanged |
| Rust / PyO3 | 1.98.1 / 0.29.2 | unchanged |

The root and fuzz lockfiles identify the product crates as 3.0.5. The independent
`tools/api-inventory/` lockfile has no product version to bump; its serde_json
pin remains 1.0.154. No new dependency or product feature is introduced.

## Observable changes and checks

Finite JSON numbers outside the f32 range no longer compare equal to f32
infinity in Rust consumers of public `Value` payloads, for example `1e100`.
The regression `runtime_payload_large_finite_number_is_not_f32_infinity`
exercises that behavior. `invalid_utf8_remains_a_redacted_json_error` preserves
the fixed public JSON diagnostic. Public operations and error contracts remain
unchanged. Historical dependency probes are recorded in the
[3.0.4 preparation notes](release-notes-3.0.4.md); they are not 3.0.5 evidence.

## TLS and optional lockfile entries

The intentional AWS-LC scope and Windows SFTP `prebuilt-nasm` pin remain as
specified in [release choices](release.md#scelte-tls-e-crittografiche).
Historical defmt/bitflags reachability checks remain in the 3.0.4 preparation
notes; optional lockfile inventory entries are not claims about linked binaries.

## Package contents

Kerberos credential caches are no longer distributed: parser tests generate
synthetic inputs at runtime. Exact inventories cover Cargo packages, source,
CLI archives, wheels and nested qualification containers. Every source member
must match the release Git checkout; generated Cargo/wheel metadata is
reproduced and compared. The policy declares the Ubuntu manylinux 2.39,
Bookworm manylinux 2.34 and Windows ABI3 wheels. Coverage uses the same guard
for its instrumented extension without changing thresholds. License texts and
historical evidence remain unchanged. See [package policy](package-policy.md).

## Local evidence

Preparation logs belong under `target/release-3.0.5/`, with
`CARGO_BUILD_JOBS=3` and Cargo offline mode. Local checks do not establish Linux,
remote fixture, cloud or full interpreter compatibility. A dirty checkout
cannot satisfy the release guard's committed-source identity requirement.
No skipped or unexecuted test counts as a passed test. Final qualification
requires the workflows and campaign below on the merged, clean commit.

### Windows offline preparation (2026-11-11)

Rust 1.98.1 and Python 3.11.15 were used. The quality requirements, including
maturin 1.16.0, were made available from local copies under `target/`.

| Command / gate | Result |
| --- | --- |
| Workspace fmt and all-target/all-feature Clippy with `-D warnings` | PASS |
| Workspace all-target test suite | 1,343 passed; 13 fixture integrations ignored |
| Isolated provider feature matrix | PASS: none, nine individual providers, full |
| Docs, test layout, comments, dependencies, anti-panic, code size, rustdoc/doctests | PASS |
| Contract adoption with pinned Python requirements | PASS |
| Python script suite with pinned requirements | 305 run: 255 passed, 3 package-fixture errors, 47 explicit skips |
| Product and qualification SBOM generation and checks | PASS |
| CLI build, smoke, readiness and normal/spooled local fault regressions | PASS |
| `python scripts/check_packages.py --build` | BLOCKED: working-tree sources differ from Git HEAD |
| Source bundle | BLOCKED: requires a clean committed checkout |
| Debug wheel with maturin 1.16.0 | Built, but guard rejects 3.0.5 metadata against Git HEAD's 3.0.4 metadata; installed SDK tests not reached |

The new release notes are also absent from Git HEAD, so package-fixture tests
that require committed sources cannot pass in this preparation checkout.
`gates.json`, `gates-pinned.json` and the individual logs preserve attempts and
exit codes; these results do not constitute release qualification.

## Qualification and publication

After merge, require successful `ci.yml` and dispatch `release-candidate.yml`
for the same commit. Preserve both run IDs. Rebuild the VM runner image from
the current project Dockerfile: its maturin pin changed to 1.16.0. The campaign
uses the canonical Actions artifacts, not binaries rebuilt by the VM runner.

Follow [the release campaign](release-campaign.md) with a fresh 3.0.5 configuration
and output directory. Use the verified Linux CLI from the published **3.0.3**
release as `baseline_binary`, recording its digest; 3.0.4 is an unpublished draft
and is not the published baseline. Complete Windows and Linux fixture checks,
paired performance, transfers and the mandatory 7200-second soak, then seal the
validated qualification bundle. Real cloud accounts remain outside this scope.

Only after qualification, use `scripts/publish_qualified.py` with the sealed
bundle and these notes. It coordinates the new tag/draft and `release.yml`,
including downloaded-asset tests on both targets before publication. Preparation
of this working tree does not create a tag, draft or release.
