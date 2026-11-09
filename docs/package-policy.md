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
rigs, development scripts and fuzz corpora). Git supplies committed bytes;
`package_source.py` checks the resulting archive before extracting it. Dirty
working-tree archive checks are local evidence only, not release qualification.

The 37 consumer files are required by the public `plenora-smb2/testing` feature:
`include_str!` embeds them and `write_compose_files` writes the rig. They remain
in Cargo/source distributions to preserve this public feature. No binary keys
were present. Two Dockerfiles previously installed a fixed test password. They
now read a Compose secret at container start, never in an image layer.
`write_compose_files` creates `smb-password` using OS randomness (32 bytes, hex),
preserves it on repeated preparation and uses mode 0600 on Unix. Consumers pass
`TestServers::test_password()` with username `testuser` to the existing explicit
credential methods. Manual consumers read that generated file. Windows uses the
parent directory's ACL. Docker integration still requires a live fixture run.

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
Every other member requires its exact SHA-256 in `member_rules`. A recorded hash
is checked even if replacement bytes are text. File extensions never grant an
exception. This closes unknown binary key formats by construction, independently
of the private-key recognizer. Text is still checked for private PEM, bare or
multiline base64 private DER blocks, credential documents and fixed passwords;
structural checks for DER and Kerberos material remain additional checks.
Traditional six-INTEGER DSA is included in the base64 key recognition.

Under the owner's static-guard decision of 2026-11-06, the guard covers direct
forms by construction and declares variants as limits rather than pursuing
them. Text scanning recognizes private PEM and bare base64 private DER; it
does not guarantee recognition of a key recoded as hex, base64 with a prefix
or mixed with other text, or other encodings. This is declared as
[STOR-LIMIT-003](contract-limits.md#stor-limit-003). A new or changed text file
is still in the exact inventory and subject to PR review; the limit concerns
only encoded content inside admitted text files. The hex and prefixed-base64
tests document non-detection as declared behavior, not guard success.

Container magic is checked before accepting a binary digest: gzip, bzip2, xz,
zstd (including skippable frames), lz4, ZIP, tar, 7z, RAR, ar and cpio. A container
requires both an exact digest and an explicit `container` declaration, even
when all its bytes are printable text. zstd,
lz4, 7z, RAR, ar and cpio are unsupported and always rejected, even when declared.
Supported compressed streams specify `format` and a `payload` rule; ZIP/tar
specify an exact `members` mapping of names to rules. The same rules apply
recursively, with a maximum nesting depth of eight. A declared distribution
member uses `distribution: true` to select its own exact package inventory.
Its filename alone never authorizes it. Top-level distribution wrappers are
selected by the explicitly supported distribution names; all their members
are subject to the rules above. Standalone release/publication files are also
selected from exact qualification/public-document inventories.

The 21 historical gzip evidence files have explicit gzip declarations, text
payload rules and exact hashes in each distribution that includes them. Their
bytes and historical claims are unchanged. Native Windows CLI/wheel hashes
currently identify the local debug artifacts, not optimized release artifacts.
No Linux native digest or completed qualification-bundle digest is invented.
Such artifacts fail closed until their actual bytes have been reviewed and
recorded. Rebuilding a native artifact may change its digest.

Digest recording is a separate, deliberate policy edit, never part of a build,
CI verification or publication. From the repository root, reproduce the current
hashes with:

```powershell
python scripts/record_package_digests.py --member qualification x86_64-pc-windows-msvc/plenora-storage.exe target/debug/plenora-storage.exe
python scripts/record_package_digests.py --repository target/fixture-packages/archives/plenora-storage-3.0.3-x86_64-pc-windows-msvc.zip target/fixture-packages/wheel/plenora_storage-3.0.3-cp310-abi3-win_amd64.whl
```

`--repository` records binary documentation from the exact source/CLI inventories;
archive arguments record binary members from the corresponding exact inventory.
The command refuses undeclared/unsupported containers and never adds member
names or container declarations. Review the resulting `scripts/package_files.json`
diff, then run the guard independently. To prepare another candidate, build its
artifacts (the builder may initially reject their unrecorded digests), record
those exact artifacts, review the policy change, then rerun verification/build.
`--member KIND NAME FILE` records a single existing inventory entry (for example,
the standalone CLI in qualification). Nested package policies must also be recorded from their individual artifacts.
This is approval of specific bytes, not an assertion that a format detector can
identify every obfuscated secret. Diagnostics never include inspected payloads.

CI runs `python scripts/check_packages.py --build`; release builders check the
actual archives, `verify_release.py` checks every artifact, and publication
checks the final qualification bundle recursively before generating its index.
`python -m unittest discover -s scripts/tests -v` exercises positive/negative
content, nested archives, extra/missing files, duplicate names and links.
