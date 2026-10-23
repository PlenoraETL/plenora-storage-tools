# Plenora Storage Tools 3.0.2

Status: qualified on 2026-10-23 by the release campaign on the dedicated VM
(attempt 3: performance PASS with 162/162 comparisons and 108/108 stability
checks, soak and transfer phases complete); published on 2026-10-23 from the
sealed bundle, assets re-downloaded and verified by `publish_qualified.py`.

3.0.2 is a patch release with no incompatible changes from 3.0.1 and no change
to the public operations.

- Adopt plenora-contracts `3aec9f7` (Public Catalogs 2.0) with the storage
  catalog and profile unchanged upstream at v1: refreshed copies and hashes,
  provider annotations, API baseline, `check_contract_adoption.py` green
  (#26).
- Close the non-blocking campaign review items #17, #20 and #22: test and
  diagnostics hardening from the reviews of #16, #19 and #21; no contract
  change. Real POSIX/ACL checks remain with the Linux CI (#27).
- webpki advisories: `rustls-webpki` 0.103.16 (RUSTSEC-2026-0382) and
  `webpki-roots` 1.0.10 (RUSTSEC-2026-0381), fuzz lockfile included (#28).
- Family dependency pin alignment: `thiserror` 2.0.21 (#29).

Qualification notes: attempt 1 passed on the VM but its result could not be
collected, because the local controller was terminated while it waited for
the runner; by design a VM attempt never resumes, so its evidence was archived
and not used. Attempt 2 was UNRELIABLE: all 162 comparisons passed, but two
stability checks of the 3.0.1 baseline on SFTP moved against themselves by
just over the 10 ms floor, in opposite directions, during host I/O; the
campaign refused a verdict, as its policy requires. Attempt 3 ran on a quiet
host and passed every phase.
