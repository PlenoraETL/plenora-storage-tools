# Package contents policy (3.0.4 candidate)

The owner decision of 2026-11-09 excludes private cryptographic material and
saved test credentials from distributions. Tests generate those inputs when run.
The earlier caches were synthetic test data, not production secrets; previous
release archives and their historical evidence remain unchanged.

`scripts/package_files.json` is the reviewed, exact inventory. The gate never
updates it. It covers all nine Cargo listings (eight distributed .crate files),
the source bundle, contracts archive, both CLI archives, both ABI3 wheels and
the qualification bundle. Version placeholders substitute only the release
version; no file glob is accepted. Python is distributed only as a wheel: there
is no sdist release path. A new target, ABI or distribution format requires an
explicit inventory and gate change.

The source bundle contains the buildable workspace sources selected from the
crate inventories, root manifests/lock/toolchain, contracts, licenses and product
documentation. It no longer distributes the entire development checkout (Docker
rigs, development scripts and fuzz corpora). Git supplies committed bytes with the build platform checkout conversion;
`package_source.py` checks the resulting archive before extracting it. Dirty
working-tree archive checks are local evidence only, not release qualification.

The 37 consumer files are required by the public `plenora-smb2/testing` feature:
`include_str!` embeds them and `write_compose_files` writes the rig. They remain
in Cargo/source distributions to preserve this public feature. No binary keys
were present. Authenticated consumer fixtures retain username `testuser` and
fixed password `testpass`, preserving their existing public behavior. This
password is a public test value, not a secret, cryptographic material or a
credential cache; it is outside the exclusion above. Docker integration still
requires a live fixture run.

Kerberos fuzz cache seeds are also generated. Qualification regenerates initial
seeds and compares their exact hashes with the campaign report. Raw fuzz corpus
is kept in the private campaign evidence but is no longer selected for public
qualification archives. Reports and logs remain required; historical receipts
are not evidence of the changed candidate.

The guard compares inventories in both directions and rejects duplicate/unsafe
names, links and unexpected empty directories. Every member at every nesting
level must have an exact inventory entry. `member_rules` supplements those
entries; rules outside a distribution inventory are rejected.

Text means valid UTF-8 with no Unicode control characters except TAB, CR and LF.
Other members are classified by provenance, never exempted by extension:

- Tracked source members have `source` and exact `sha256` rules. The package
  gate retains these recorded binary digests and also compares every source
  member (text or binary) with Git checkout bytes at the release revision. This includes the 21 historical gzip evidence files; their
  bytes and historical claims remain unchanged.
- Native exemptions come exclusively from `NATIVE_DISTRIBUTIONS` in
  `scripts/check_packages.py`: the exact distribution-relative member, target
  format and library type are fixed in code, separately for each wheel tag.
  Policy `build` roles are rejected, including inside nested containers.
  CLI executables and extension libraries must be x86-64 ELF64 on Linux or
  PE32+ on Windows; DLL versus EXE and ELF executable versus shared library
  types are checked. Crates admit no native output. Qualification bundles
  admit the two CLI executables at their exact target-relative paths.
- Generated archives nested in qualification have container declarations and
  must match the 24 exact `GENERATED_CONTAINERS` entries in code. A policy
  declaration alone cannot exempt a source from Git identity.
  Their contents recurse into their exact distribution inventory; they are
  never opaque exemptions. A filename alone never authorizes a nested archive.

All path comparisons use one canonical form: `/`, Unicode NFC and casefold.
Backslashes, roots, drive prefixes, empty components, `.` and `..` are rejected,
as are collisions in this form. All keyed inventory, rule and native-table
lookups use `path_lookup`, which rejects missing keys and canonical collisions.
The complete rule table is constructed from the inventory before applying
supplementary rules; verification never substitutes an empty rule on lookup. Archive roots are stripped and crate members
map to `crates/<crate>/`; wheel Python members map to the Python source tree.
Source archives map directly to repository paths. The Python sdist remains
undeclared and is rejected. Mapped paths are checked against Git and the source
inventory; a native exemption cannot cover a protected source path.

`wheel-tags` declares `cp310-abi3-win_amd64`,
`cp310-abi3-manylinux_2_34_x86_64` (Bookworm release/coverage) and
`cp310-abi3-manylinux_2_39_x86_64` (Ubuntu quality CI). Both Linux tags use the
same exact member inventory. The release qualification inventory requires the
Bookworm wheel, not both Linux CI variants. Undeclared tags are rejected rather
than inferred from the build host. CLI distributions are exactly Windows MSVC
ZIP and Linux GNU tar.gz. No standalone static library is distributed.

A recorded source hash is checked even if replacement bytes are text. Text
and non-exempt binary members are checked for private PEM, bare or multiline base64 private
DER blocks, credential documents and fixed Samba passwords other than the exact
public fixture value described above; structural checks for
DER and Kerberos material remain additional checks. Traditional six-INTEGER DSA
is included in the base64 key recognition.

The owner decision of 2026-11-10 requires **every source member**, textual or
binary, to equal its tracked release source byte for byte. The reference comes
from the pinned Git revision and an isolated shared clone under `target/`,
checked out with the repository's `core.autocrlf` and `core.eol` settings.
Git >= 2.39 is required and checked explicitly. Each guard Git command trusts
only the absolute source path and, when needed, the isolated clone through
command-scoped `safe.directory`. Shallow sources are accepted explicitly;
clone/checkout failures retain Git's diagnostic. Working-tree bytes and dirty
attributes cannot supply the reference. `.gitattributes` and the build platform's Git EOL settings
apply; the guard never strips CR or otherwise normalizes package bytes. The
source builder uses the same checkout conversion. Changes to inventory or a
recorded digest cannot authorize an untracked or substituted source.

The closed generated-text table is `GENERATED_MEMBERS` in
`scripts/package_generated.py`, matched by distribution and exact member (only
version placeholders expand). Every such exception must remain UTF-8 text,
contain no container, and pass the private-material and credential scanner;
it cannot carry a source/digest rule. Cargo metadata is reproduced offline by the pinned Cargo from an isolated Git
checkout (`cargo package --workspace --no-verify --locked --offline`): normalized
`Cargo.toml` and generated `Cargo.lock` must match byte for byte. The VCS JSON
is serialized canonically from the release revision and expected `path_in_vcs`.
Wheel `WHEEL` and `METADATA` are reconstructed in maturin's field order from
`pyproject.toml`, the pinned maturin version/tag and the declared README body
(LF serialization plus maturin's final newline). `RECORD` is reconstructed as
LF CSV in archive member order, with SHA-256 and sizes, and its own empty
hash/size. All these members must match the reconstructed bytes exactly;
parser-equivalent spellings, ignored lines, BOMs and whitespace are rejected.
These checks also apply inside qualification containers. Regression tests in
`test_packages_canonical.py` run through the existing script-test discovery in
CI `product-quality` and `native-windows`, including an untrusted shallow clone.
The wheel SBOM, campaign evidence and publication documents remain scanned
text exceptions, not content-derived provenance guarantees:

| Distribution | Generated-text exceptions |
| --- | --- |
| Source tar.gz, contracts, CLI documents/licenses | None |
| Each Cargo package | `Cargo.toml`, `Cargo.lock`, `.cargo_vcs_info.json`; `Cargo.toml.orig` must equal the tracked manifest |
| Each supported wheel | Exact versioned dist-info `METADATA`, `RECORD`, `WHEEL`, `sboms/plenora-storage-py.cyclonedx.json`; the copied `licenses/LICENSE` must equal Git |
| Qualification | 144 exact report, log, checksum and manifest paths enumerated in the code table; 24 declared nested distributions recurse and natives use the existing native table |
| Standalone publication documents | The five exact versioned manifest/SBOM/qualification paths enumerated in the code table |
| Python sdist | Unsupported and rejected; no exceptions |

No aggregated license is currently distributed as a generated-text exception.
A new generated member requires a code-table change as well as an inventory
change. Qualification source members not in that table map to repository paths
and must equal Git, including sources inside all nested packages.

Text scanning recognizes private PEM and bare base64 private DER; it does not
guarantee recognition of hex, prefixed/mixed base64 or other encodings. Under
[STOR-LIMIT-003](contract-limits.md#stor-limit-003), the residual risk includes repository content subject to PR review **and**
generated campaign evidence/publication documents and the wheel SBOM.
For source members, encoded substitutions made only during packaging are
rejected by byte identity.
The 144 generated qualification entries and five publication documents remain
within STOR-LIMIT-003: provenance is guaranteed by the receipt and the
publication HEAD/receipt/tag binding, not by their content. A `deny.log` with
the required success substring is not proof that its remaining text is safe.
The generated wheel SBOM also retains the scanner's encoding limits; an exact
RECORD hash binds its bytes but does not prove their content. This limit is
not reduced to the repository alone.
The hex and prefixed-base64 scanner tests document non-detection, while source
substitution tests demonstrate rejection independently of encoding.

Every supported container is scanned as a raw byte stream after decompression,
covering headers, metadata, padding, central directory and member data. Only
exact data ranges of code-table natives in their specified distribution are
excluded: compressed or stored entry bytes for ZIP, member data bytes for tar
(without block padding). These ranges come from the same layout validation
that rejects unaccounted bytes. Native headers and ZIP data descriptors remain
scanned, as does every decompressed non-native member.

Tar accepts only regular files, empty directories and local PAX headers with
`path`, empty `linkpath`, `size`, `mtime`, `uid`, `gid`, `uname` or `gname`.
All other PAX keys, global PAX headers, GNU longname/longlink/sparse headers and
other entry types are rejected before the parser consumes them. The source
builder recreates local headers from selected Git member fields, without
propagating Git's global commit comment. Cargo and source archives are checked
with this closed dialect; sdist remains undeclared.

Only native members in the code table, in their specified distribution, have name,
role, target, ABI and format checks, with no content scanning. All other
members, including textual sources, tracked binaries, declared containers
and wheel metadata, remain subject to scanning. Appended or embedded payloads, such as a ZIP after an ELF/PE header,
are not recursively inspected or reliably detected. This is
[STOR-LIMIT-004](contract-limits.md#stor-limit-004); its test documents
non-detection as declared behavior. The hazard is a binary produced by a
compromised build. CI produces build outputs from sources covered by exact
inventories, digests and scanning; this is the mitigation, not proof of binary
contents. Closing the limit requires build reproducibility verification or
build attestation.

Container magic is checked before accepting a binary digest: gzip, bzip2, xz,
zstd (including skippable frames), lz4, ZIP, tar, 7z, RAR, ar and cpio. A container
requires an explicit `container` declaration and, for a source member, an exact
digest even when all its bytes are printable text. Generated distribution
containers instead require a recursive distribution declaration. zstd,
lz4, 7z, RAR, ar and cpio are unsupported and always rejected, even when declared.
Supported compressed streams specify `format` and a `payload` rule; ZIP/tar
specify an exact `members` mapping of names to rules. The same rules apply
recursively, with a maximum nesting depth of eight. A declared distribution
member uses `distribution: true` to select its own exact package inventory.
Its filename alone never authorizes it. Top-level distribution wrappers are
selected by the explicitly supported distribution names; all their members
are subject to the rules above. Standalone release/publication files are also
selected from exact qualification/public-document inventories.

Digest recording is a separate policy edit for tracked source members only:

```sh
python scripts/record_package_digests.py --repository
```

Declare the exact `source` path and any container structure before recording.
The recorder compares input bytes with Git HEAD checkout bytes, refuses untracked/substituted
inputs, and never learns member names or container declarations. Archive
arguments validate generated members without recording their digests;
`--member KIND NAME FILE` refuses build outputs. Rebuilding a CLI, wheel or
nested Cargo archive requires no policy edit. Release manifests and receipts
still bind the actual final artifacts to their SHA-256 checksums: this policy
change does not weaken artifact identity checks or claim reproducible builds.
Diagnostics never include inspected payloads.

CI runs `python scripts/check_packages.py --build`; release builders check the
actual archives, `verify_release.py` checks every artifact, and publication
checks the final qualification bundle recursively before generating its index.
`python -m unittest discover -s scripts/tests -v` exercises positive/negative
content, nested archives, extra/missing files, duplicate names and links.
