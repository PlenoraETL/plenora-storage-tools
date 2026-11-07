# Plenora Storage Tools 3.0.3

Status: qualified on 2026-11-07 by the release campaign on the dedicated VM
(attempt 2: performance PASS with 162/162 comparisons and 108/108 stability
checks after the 16-round paired warm-up, transfer phases and the 2-hour soak
complete); published on 2026-11-07 from the sealed bundle, assets re-downloaded and verified
by `publish_qualified.py`.

3.0.3 is a security patch release with no incompatible changes from 3.0.2 and
no change to the public operations. It affects only the SFTP provider's SSH
client and the async runtime.

- `crypto-bigint` 0.7.10 (RUSTSEC-2026-0387, 2026-10-23): timing variability
  in operations meant to be constant-time, reached through `russh` →
  `p256`/`p384` → `ecdsa` → `elliptic-curve` (SFTP). **3.0.2 and earlier
  ship `crypto-bigint` 0.7.5**; versions 0.7.6–0.7.8 are yanked upstream.
  `ctutils` follows to 0.4.8; fuzz lockfile aligned (#34).
- `tokio` 1.53.3 and `ctutils` (RUSTSEC-2026-0386, RUSTSEC-2026-0385) (#32).

Release tooling, not shipped in the artifacts: the stability gate measures
after 16 paired warm-up rounds (#36), and a watchdog bounds every wait of the
transfer measurement so a stalled worker fails the phase instead of hanging
it (#37).

Qualification notes: the campaign ran on a new dedicated VM (Ubuntu 24.04,
8 vCPU, 16 GB), the previous one having been retired. Attempt 1 passed every
phase except the soak, which stopped after 1811 s on a single SDK `get`
against the FTP fixture that returned `FTP_REQUEST_FAILED`, classified
`transient`, retry `safe`, remote effect `rolled_back` — the declared
behaviour; the soak script stops at the first error by design, and resources
were stable. Attempt 2, a fresh measurement of every phase, passed: the soak
completed 7232 s with 117 cycles in each upload mode. The 3.0.2 Linux CLI used
as performance baseline was verified against the published `SHA256SUMS`.
