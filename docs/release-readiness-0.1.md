# Release 0.1.0: perimetro e criteri di distribuzione

Rust e CLI includono S3-compatible, SFTP e FTP. La release promuove le sette
operazioni v1 ad `available`; il vecchio opt-in experimental resta accettato
per compatibilità. Le autorizzazioni per trasporti insicuri e reti private
rimangono indipendenti ed esplicite.

Il file decisivo per la distribuzione è
`dist/0.1.0/release-qualification.json`, con stato
`qualified_for_publication`. Identifica commit, digest dei sorgenti, binari,
manifest di adozione ed evidenze dei gate. Non distribuire semplicemente
l'ultimo contenuto di `target/release`.

## Chiusura dell'audit

L'[audit iniziale](release-audit-baseline.md) fotografa la baseline
`9b7188e7d83e65da30ba18724a6b000efae5bc70`. Il
[riepilogo del primo candidato](release-evidence-20260918.json) è storico:
i suoi digest non identificano la release promossa.

| Punto | Chiusura |
| --- | --- |
| STOR-REL-001 | `list --all` usa un Engine persistente per l'intera invocazione, con totale limitato; cursori tra processi e pagine incomplete falliscono esplicitamente. |
| STOR-REL-002 | Probe FTP delle directory sotto deadline/cancellazione, con server silenzioso di regressione. |
| STOR-REL-003 | SFTP negozia POSIX rename v1 prima di mutare, sostituisce oggetti esistenti e conserva l'esito ambiguo quando manca la risposta. |
| STOR-REL-004 | File di connessione regolare, lettura limitata a 1 MiB + 1 byte sotto controllo di esecuzione. |
| STOR-REL-005 | Gli effetti delle directory preparatorie sopravvivono all'errore e alla pulizia del solo staging. |
| STOR-REL-006 | Upload dichiaratamente fuori limite rifiutato prima di aprire la destinazione. |
| STOR-REL-007 | Preflight specifico dei provider prima di aprire un artifact sink. |
| STOR-REL-008 | Cinque crate con versioni e licenze, verificati da consumer esterno agli archivi. |
| STOR-REL-009 | Binari Linux/Windows, archivi, checksum, pipeline e procedura di installazione/rollback. |
| STOR-REL-010 | Manifest di adozione v4 con digest reali, validazione strutturale e semantica, suite runtime eseguita dal crate distribuito. |
| STOR-REL-011 | Skip espliciti; gate Linux senza skip; test di sicurezza, conflitti, concorrenza, commit ambiguo e recovery. |
| STOR-REL-012 | Audit e policy dipendenze/licenze; rustls corretto per RUSTSEC-2026-0285; nessuna eccezione agli advisory. |
| STOR-REL-013 | Matrice e limiti della [guida operativa](release.md). |

Il confronto con il profilo comune ha inoltre corretto gli UUID runtime:
identità non canoniche sono rifiutate prima dell'invocazione, causation è
preservata e i mismatch di route producono errori `protocol`.

## Matrice qualificata

- Linux x86_64 nel container Rust 1.92 / Debian Bookworm, sulla VM dedicata
  `192.168.2.134`; progetto Compose isolato `storage-release`.
- Windows x86_64 nativo; binario finale esercitato contro gli stessi server
  della VM con S3 HTTP autorizzato esplicitamente, SFTP con pin e FTP opt-in.
- MinIO HTTPS con CA valida e rifiuto del nome TLS errato nel gate Linux;
  fingerprint SSH valido/errato; immagini server bloccate nel compose.
- Le sette operazioni sui tre provider, file vuoto e oltre 8 MiB, checksum,
  paginazione, sovrascrittura, limiti, create-if-absent concorrenti e effetti
  delle directory preparatorie.
- Multipart S3: richiesta di commit ritardata e risposta persa, sotto timeout
  e SIGTERM. Si verifica lo stato reale del server e il recupero dell'oggetto;
  il client restituisce `unknown`/`requires_recovery` in tutti i casi ambigui.
- SFTP: timeout/cancellazione prima e dopo il commit, cleanup confermato o
  non dimostrabile, senza eliminazione della destinazione finale.
- Schema CLI/error/capabilities e suite runtime con tutte le operazioni,
  boundary degli artifact, integrità e identità. La [matrice di adozione](contract-adoption.md)
  collega i contratti alle prove.

Non sono promessi FTPS, SSH a chiave, sandbox contro symlink ostili, AWS S3,
altri server o sistemi operativi, SLO o rollback dopo ogni tipo di guasto.
Questi limiti sono parte del perimetro pubblico, non gate occultamente saltati.

## Riproduzione dei gate

```sh
cargo fmt --all -- --check
cargo clippy --workspace --all-targets --all-features --locked -- -D warnings
cargo test --workspace --all-targets --locked
docker compose build storage-rust
bash scripts/prepare-fixtures.sh
docker compose run --rm --no-deps storage-rust
cargo fetch --locked
python scripts/build_release.py
```

Eseguire poi le prove sui binari in `dist/` tramite `PLENORA_CLI_BIN`:
`audit_release_readiness.py`, `qualify_cli.py` e, nel runner Linux,
`qualify_commit_faults.py`. Conservare i JSON di qualifica e i log delle suite.
Eseguire cargo-audit 0.22.2 e cargo-deny 0.20.2 sul lockfile definitivo.

La chiusura avviene con
`python scripts/qualify_release.py dist/0.1.0 --evidence <directory-log>`.
Lo script rifiuta un checkout modificato e richiede prove dei due target
associate agli stessi binari e sorgenti. La pubblicazione è separata:
la pipeline produce artefatti verificabili e non esegue upload a crates.io,
tag o pubblicazione di release GitHub.
