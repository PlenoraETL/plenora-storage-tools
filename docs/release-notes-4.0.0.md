# Plenora Storage Tools 4.0.0

4.0.0 is a major release.

## Breaking changes

- Python 3.14 is the minimum supported runtime. The native SDK wheels use the
  cp314 stable ABI; earlier Python runtimes are not supported.
- No compatibility with earlier Storage releases is provided.

## Changes

- Adopt the approved Plenora dependency snapshot and its Linux CI checker,
  including public dependency pins, base64 0.23.1 and proptest 1.12.0.
- Align compatible transitive dependencies with the family references,
  retaining AWS-LC and the exceptions declared in the common manifest.
- Fixture certificates now pass strict TLS verification; fixture startup checks
  application readiness and reports sanitized failures without retrying
  permanent TLS errors.
- Campaign and CI orchestration rebuilt around a single campaign model
  (state, lease and epoch transitions, file permissions, time budgets, durable
  writes) and an executable CI plan with execution receipts.

## Distribution

Rust crates, source archive, CLI for Linux x86_64 and Windows x86_64, and
native Python wheels for both platforms. Nine providers and seven operations.
Tests run against local fixtures; they do not certify real cloud accounts or
arbitrary remote servers.

## Release evidence policy

`storage-release-no-soak-v1` removes soak and its calibration from the release
campaign and required evidence. The bundle cannot claim a passed soak.
Owner decision, 16 November 2026: «il soak non fa parte del cancello di questa release; una campagna soak della famiglia è prevista più avanti».
The manual tools `stress_python.py`, `soak_policy.py`, `calibrate_cycles.py`
and their tests remain available for that later work. Earlier campaign
failures retain their original results; this change is not qualification evidence.
Parser release evidence replays all seeds and uses a 10,000-execution budget
per ASan target; timed exploration remains scheduled separately. Previous
release binaries and performance A/B are no longer release dependencies.
No absolute performance SLO is claimed. Large transfers, spool boundaries,
concurrency, platform ABIs and real timeout/keepalive tests remain required.
Local oracle calibration is not a qualification of the final 4.0.0 artifacts.

## Correzioni del processo di pubblicazione


Per 4.0.0 il sorgente condiviso si confronta sul flusso tar non compresso:
SHA-256 del tar prodotto da `git archive`, identico su ogni piattaforma per nomi,
modi, ordine e date dei membri. Resta fuori solo il compressore gzip: CPython
3.14 su Windows usa zlib-ng, su Linux zlib, e i byte compressi differiscono.
Inventario e provenienza Git restano obbligatori.

`verify_release.py` confronta questo digest tra target già durante l'assemblaggio;
`release_publication.py` la ricontrolla e pubblica i byte originali del gzip
Linux qualificato. I gzip Windows/Linux possono avere digest diversi (zlib-ng
vs zlib): i due manifest e report consumer conservano i rispettivi digest e il
bundle pubblico di qualifica conserva entrambi. Nessuna ricompressione cambia
l'asset Linux o le sue attestazioni. CLI, wheel, crate, contratti, report e
contenitori di qualifica mantengono digest esatti per target o singola istanza;
non si pretende uguaglianza tra build indipendenti. Le SBOM vengono confrontate
come oggetti JSON, preservando i digest esatti degli input e degli artefatti.

La campagna rifiuta immediatamente byte tracciati diversi dai blob Git; tutti
i semi fuzz sono binari. I comandi Git VM proteggono i permessi dell'index e
il coordinatore chiude il progetto Compose anche su errore, prima di rilasciare
l'ammissione. Queste modifiche richiedono nuovi RC, CI e campagna sul commit
finale, quindi un nuovo input di pubblicazione: non qualificano retroattivamente
gli artefatti del tentativo precedente.
