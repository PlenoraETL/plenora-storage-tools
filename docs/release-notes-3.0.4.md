# Plenora Storage Tools 3.0.4

Status: dependency alignment candidate, unreleased and not qualified. The source
manifests still identify 3.0.3. The 3.0.3 preparation commit `ce49505` bumped
manifests, locks and generated state together; that is the release preparation
step, not a prerequisite for this dependency PR. A later release preparation
must update them consistently before qualifying 3.0.4. No evidence from 3.0.3
is reused as qualification of this candidate.

## Dependency scope

| Component | Before | Candidate |
| --- | --- | --- |
| serde_json | 1.0.151 | 1.0.154 |
| maturin | 1.15.0 | 1.16.0 |
| tokio / tokio-util | 1.53.3 / 0.7.19 | unchanged |
| tokio-rustls / hyper / h2 | 0.26.5 / 1.11.1 / 0.4.19 | unchanged |
| time / thiserror | 0.3.55 / 2.0.21 | unchanged |
| rustls / ring | 0.23.46 / 0.17.14 | unchanged |
| aws-lc-rs / aws-lc-sys | 1.18.1 / 0.45.0 | unchanged |
| Rust / PyO3 | 1.98.1 / 0.29.2 | unchanged |

All three tracked lockfiles are aligned: root, `fuzz/` and
`tools/api-inventory/`. There is no vendor lockfile. The workspace serde_json
requirement remains compatible under `DEP-RANGE-1`; the API inventory tool
retains an exact pin. No feature is removed: the distributed default/full CLI and SDK resolve only
`default` and `std` for serde_json through normal/build dependencies on both
release targets. `float_roundtrip` comes from jsonschema/jsonschema-value in
the development dependency graph; the original A/B probes also explicitly
enabled it. It is not a distributed product feature. The product does not
enable `raw_value`, `arbitrary_precision` or `preserve_order`. No new dependency
was introduced.
Maturin pins include pyproject, quality requirements, both CI installation
steps, the Dockerfile, release guide and README build instructions.

## Observable changes and checks

Upstream [serde_json 1.0.152](https://github.com/serde-rs/json/releases/tag/v1.0.152)
corrects comparison of large finite JSON numbers with f32 infinity. A Rust
consumer inspecting a public runtime `Value` payload containing `1e100` now
gets false from `value == f32::INFINITY` (previously true). The regression
`runtime_payload_large_finite_number_is_not_f32_infinity` exercises the public
runtime envelope and its serialization round-trip in the normal workspace
suite, run by both native Windows CI and Docker conformance.

Local A/B probes with 1.0.151 and 1.0.154 were repeated separately with the
product's `default`/`std` configuration (without `float_roundtrip`) and with
explicit `float_roundtrip`. All four probes passed their round-trip assertions.
For these inputs there is no observed difference between feature configurations:

| Configuration | Version | `1e100 == f32::INFINITY` | Invalid UTF-8 line:column |
| --- | --- | --- | --- |
| default/std | 1.0.151 | true | 1:10 |
| default/std | 1.0.154 | false | 1:3 |
| default/std + float_roundtrip | 1.0.151 | true | 1:10 |
| default/std + float_roundtrip | 1.0.154 | false | 1:3 |

All four serialize `-0`, `0.1`, `1e100`, u64::MAX and i64::MIN respectively as
`-0.0`, `0.1`, `1e+100`, `18446744073709551615` and `-9223372036854775808`,
and reparse to equal Values. This bounded probe does not establish equivalence
for all floating-point inputs. The upstream
[1.0.153](https://github.com/serde-rs/json/releases/tag/v1.0.153) UTF-8 position
correction is visible in the raw parser (column 10 becomes 3 in the probe),
but the public preflight discards that diagnostic. The regression
`invalid_utf8_remains_a_redacted_json_error` checks `JSON_INVALID` and its fixed
message. This is not a new public diagnostic. The arbitrary-precision negative
zero fix and [1.0.154 no_std preserve_order support](https://github.com/serde-rs/json/releases/tag/v1.0.154)
do not apply to the resolved product features.

The [maturin 1.16.0 changes](https://www.maturin.rs/changelog) include interpreter
discovery, ABI tags and packaging changes. A local Windows CPython wheel check
cannot establish equivalence on Linux or on every supported interpreter.
Those remain separate CI/release gates.

The two local debug wheels built from this candidate with maturin 1.15.0 and
1.16.0 have the same `cp310-abi3-win_amd64` tag, member names, native extension
bytes and Python source bytes. Only the WHEEL generator, the generated SBOM's
timestamp/UUID and RECORD differ (`wheel-comparison.json`, `wheel-metadata.diff`
and `wheel-sbom.diff` in the local evidence directory). No new packaging
behaviour is claimed for that tested configuration.

## TLS and optional lockfile entries

The intentional AWS-LC scope and Windows SFTP `prebuilt-nasm` pin are documented
in [release choices](release.md#scelte-tls-e-crittografiche).

`bitflags 1.3.2` is required by `defmt 1.1.1`; the root lock reaches defmt through
the optional dependencies of `jiff 0.2.37` and `jiff-core 0.1.1`. It is not a
reason to force a bitflags major upgrade. The following command was run for
both `plenora-storage-cli` and `plenora-storage-py`, for
`x86_64-pc-windows-msvc` and `x86_64-unknown-linux-gnu`:

```sh
cargo tree --locked --offline --target TARGET -p PACKAGE -e normal,build --prefix none
```

All four trees completed successfully with neither defmt nor bitflags 1.3.2.
The SDK trees were also checked with the wheel's `pyo3/extension-module` feature.
The shipped default/full CLI and SDK therefore do not compile them through
their normal/build dependency graphs. This is target-specific dependency
reachability evidence, not a binary scan or a Linux build. Inverse queries
for either absent package fail with package-not-found; that failure alone is
not treated as proof. Optional inventory entries remain in Cargo.lock/SBOM.

## Package contents

`cargo package --list --allow-dirty --offline --locked -p plenora-smb2` includes
`src/testing/fixtures/consumer/` and the Kerberos test credential caches
`src/auth/kerberos/fixtures/test.ccache` and `test_v3.ccache`. These are existing
test fixtures in the distributed Rust source package, not production
credentials; they are also in the committed source bundle. They have not been
removed by this alignment. All nine workspace crate file lists were checked.
The CLI archive uses the explicit file list in `scripts/package_cli.py`.
The built Python wheel has no fixture/key filenames in its member list.
Repository license texts are preserved byte for byte.

## Local evidence

Commands, exit codes and logs for this working-tree check are kept under
`target/dependency-alignment/`; `gates.json` records the ordinary offline gates.
The original A/B probes are `probe-1.0.151.log` and `probe-1.0.154.log`
(explicit float_roundtrip only); the corrected four-way comparison, manifests,
locks, feature trees and gate results are under `correction/`. The four complete
release-target graphs are `tree-<target>-<package>.log`. These local files are
not a release receipt. Remote fixtures, real cloud services, Linux execution,
coverage/fuzz campaigns and the full interpreter matrix require their own
workflow runs before release qualification.

### Windows offline results (2026-11-09)

All Cargo builds used `CARGO_BUILD_JOBS=3`; the gate runners set
`CARGO_NET_OFFLINE=true`. Build tools were installed separately into `target/`
before their tests. Rust is 1.98.1; Python is 3.11.15. No release build or
publication was performed.

| Command / gate | Result |
| --- | --- |
| `cargo fmt --all -- --check` | PASS, workspace only |
| `cargo clippy --workspace --all-targets --all-features --locked --offline -- -D warnings` | PASS, including the new regressions |
| `python scripts/check_anti_panic.py` | PASS |
| `cargo test --workspace --all-targets --locked --offline` | PASS on retry: 1,343 passed, 13 fixture integrations ignored |
| `cargo test --offline --locked -p plenora-storage-core unique_json` | PASS, including both new regressions |
| `cargo deny --config target/dependency-alignment/deny-local.toml --locked --offline check` | PASS: advisories, bans, licenses, sources |
| `python -m unittest discover -s scripts/tests -v` | PASS after installing quality requirements: 228 run, 47 explicit skips |
| `python scripts/check_contract_adoption.py` | PASS with quality requirements |
| `python scripts/check_features.py` | PASS on sequential retry: none, all nine isolated providers and full |
| `python scripts/check_test_layout.py`, `check_comments.py`, `check_dependencies.py`, `check_docs.py`, `check_rust_docs.py` | PASS, including rustdoc with denied warnings and doctests |
| `python scripts/code_size.py --check --output target/dependency-alignment/code-size.json` | PASS |
| `python scripts/render_sbom.py <output>` and `--check`, repeated with `--qualification` | PASS for both inventories |
| `cargo build --offline --locked -p plenora-storage-cli`; `python scripts/release_publication.py smoke-cli --binary target/debug/plenora-storage.exe` | PASS |
| `python scripts/audit_release_readiness.py` | PASS, local servers |
| `python scripts/qualify_extended_faults.py`, repeated with `--spool-uploads --output target/dependency-alignment/spooled-regressions.json` | PASS, local fault checks |
| `cargo clippy --offline --locked --manifest-path tools/api-inventory/Cargo.toml -- -D warnings` | PASS |
| `python scripts/build_python.py --debug` with maturin 1.16.0 and dedicated `--output` / `--environment` under target | PASS, 31 installed SDK tests |
| SDK environment Python: `scripts/check_installed_sdk.py target/dependency-alignment/wheel-1.16.0 --coverage --typing --output target/dependency-alignment/sdk-compatibility` | PASS, 31 tests, 100% measured line/branch coverage, mypy 2.3.1 valid/invalid consumers |

The 47 script skips concern POSIX/Linux, real ACL/unprivileged checks and the
unavailable smbclient; they are not passed tests. The initial Rust suite was
interrupted after a stalled strsim compilation and then rerun successfully.
MSVC emitted linker debug-information warnings during the test build; they
are retained in the logs. The first feature-check attempt failed while other
builds shared the target directory; its log is retained separately from the
sequential retry. Initial missing-Python-module errors were resolved by
installing the pinned requirements into the test environment.

The default cargo-deny attempt could not lock the read-only global cache.
The successful retry used an unchanged policy with only `advisories.db-path`
redirected to a copy under target. Database revision:
`263a4d951a0867d4bddc29dddd6d59427c78f9a7`. Additional cargo-audit 0.22.2 runs
with `--db <cached-db> --no-fetch --no-yanked --file <lock> --deny warnings`
passed on all three locks and on the reconstructed original SMB identity
used by `audit_smb_upstream.py`. These audit runs did not refresh advisories
or query online yanks; cargo-deny retained the project's `yanked = "deny"`
policy against the cached registry. The online CI audit workflow was not run.

`python scripts/package_source.py` was attempted and refused the dirty
checkout as designed. No commit was made to bypass it. The source archive
consumer, final release packaging, Linux execution, remote fixture matrix,
parser fuzz, Rust coverage, performance/soak campaign and other supported
Python interpreters were not run. This candidate still requires release
preparation and artifact-bound qualification.

### Attribution and pin correction (2026-11-09)

The follow-up runner is `target/dependency-alignment/correction.py`; commands,
exit codes and logs are in `target/dependency-alignment/correction/results.json`
and adjacent logs. It uses `CARGO_BUILD_JOBS=3` and offline Cargo resolution.
The standalone probes use exact serde_json versions and no dev dependencies:

```sh
cargo generate-lockfile --offline --manifest-path PROBE/Cargo.toml
cargo run --locked --offline --manifest-path PROBE/Cargo.toml
cargo tree --locked --offline --manifest-path PROBE/Cargo.toml -e normal,build,features -i serde_json
cargo tree --locked --offline --target TARGET -p PACKAGE -e normal,build,features -i serde_json
```

All four probes and their feature trees passed. All four product feature trees
passed with only default/std (SDK adds `--features pyo3/extension-module`).
`cargo tree --locked --offline -e features -i serde_json` separately confirms
the development-only jsonschema/jsonschema-value path to float_roundtrip.
Execution of the probes is native Windows; Linux evidence here is dependency
resolution only.

The residual-version search included hidden tracked configuration files and
excluded target and .git. The release guide, Dockerfile build install and
Python pin-test example now use maturin 1.16.0. Remaining old-version mentions
are historical changelog/release notes or explicit A/B baselines in this report;
Cargo.lock's unrelated `portable-atomic 1.15.0` is unchanged. No current serde_json pin
remains at 1.0.151. Historical release evidence was not edited.

| Follow-up gate | Result |
| --- | --- |
| `cargo test --offline --locked -p plenora-storage-core unique_json` | PASS, 4 tests; remaining tests filtered out |
| `python scripts/check_docs.py` | PASS, repeated after the final report edit |
| `python scripts/check_dependencies.py` | PASS |
| `python -m unittest discover -s scripts/tests -v` | PASS, 228 run, 47 explicit skips (same platform/tool limits described above) |
| `git diff --check` | PASS |

These are the existing CI product-quality/native test gates, not a new release
qualification. The Dockerfile pin was corrected but the Docker image was not
rebuilt in this Windows follow-up.
