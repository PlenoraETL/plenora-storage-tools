# Chiusura della release 2.0.0

Release del 28 settembre 2026, pubblicata esclusivamente su
[GitHub Releases](https://github.com/PlenoraETL/plenora-storage-tools/releases/tag/v2.0.0).
Il tag identifica `7dd45b12e46f4d7c8d7499f25976a122b1fbfac7`;
l'archivio sorgente qualificato ha SHA-256
`e5e708434797f6e9f70c1678dda5a8d311442081bac33011cd49dafeda81b752`.
Questo resoconto viene aggiunto dopo la pubblicazione: non modifica il tag,
gli archivi o i documenti storici contenuti nella distribuzione.

Le prove sono nella [CI finale](https://github.com/PlenoraETL/plenora-storage-tools/actions/runs/36408884948),
nella [build del candidato](https://github.com/PlenoraETL/plenora-storage-tools/actions/runs/36408896190)
e nel [workflow di pubblicazione](https://github.com/PlenoraETL/plenora-storage-tools/actions/runs/36465383544).
Gli asset pubblici includono `release-qualification.json`, checksum e bundle
con i report consumati dai gate. La ricevuta, non il nome di un file o il solo
tag, associa le prove ai digest degli artefatti.

## Chiusura del piano

| Ambito | Risultato ed evidenza |
| --- | --- |
| A1 — Responsabilità | Nove adapter e dispatch CLI separati secondo la [mappa architetturale](architecture.md); Rust, CLI e SDK conservano la baseline pubblica verificata sui due target |
| A2 — Documentazione Rust | `missing_docs` negato, rustdoc e doctest in CI; quattro deroghe Clippy globali sostituite da documentazione o eccezioni locali motivate |
| A3 — Dimensione | [Baseline 1.0](code-size-baseline-1.0.0.json), conteggi del prodotto e fork distinti, budget per componente e file; gate e casi di rifiuto eseguiti |
| A4 — Commenti | Controllo Rust, Python/stub, shell, PowerShell, TOML e YAML; esclusioni e limiti lessicali espliciti in [Qualità 2.0](quality-2.0.md) |
| A5 — Dipendenze | Pin privati ed eccezioni del confine pubblico verificati, audit/deny superati; CLI e wheel finali dei due target scansionate con Syft 1.52.0 e output CycloneDX 1.6 |
| A6 — Guide correnti | Indice, architettura, [migrazione](migration-2.0.md), regole di qualità e procedura di release aggiornati; questo resoconto collega gli esiti finali |

Il riferimento resta Database Tools
`850723d86be9d0cb5d8b0643da6cae971c7ec068`. Il lavoro chiude gli ambiti approvati;
non assegna una percentuale di maturità e non certifica nuovamente Database Tools.
Le [differenze intenzionali](quality-2.0.md#differenze-rispetto-al-riferimento)
rimangono documentate.

## Verifiche degli artefatti

- Rust: 1.171 test passati e nessuno ignorato nel log Linux del candidato;
  1.158 passati e 12 ignorati su Windows. I test ignorati non sono successi:
  le prove Rust con server sono eseguite su Linux e la qualifica CLI/SDK Windows
  usa separatamente le fixture della VM.
- CLI e SDK: Linux e Windows x86_64; wheel installate su CPython 3.10–3.14
  su entrambi i target, inclusi typing ed esempi sync/async.
- API, feature isolate, parser fuzz, coverage per componente, fault injection,
  consumer degli archivi e audit delle dipendenze: gate superati.
- Prestazioni: confronto di due campagne da 30 round nello stesso laboratorio,
  CLI 1.0 pubblicata contro CLI 2.0 finale; budget di tempi e memoria rispettati.
- Trasferimenti: checksum, pubblicazione e limiti verificati; concorrenza a
  4 e 16 worker. La prova da 1 GiB è passata per due round completi; i percorsi
  buffered verificano il rifiuto prima della mutazione, non un trasferimento
  che superi il limite documentato.
- Soak della wheel Linux finale: PASS, 7.208,121 secondi, 150 cicli, nove
  provider e quattro worker; limiti di memoria, thread e descrittori rispettati.
- Pubblicazione: qualifica del bundle e controlli degli asset dopo download
  su entrambi i target; successiva verifica locale dei checksum pubblicati.

## Tentativo da 1 GiB ripetuto

La prima campagna ha fallito nella copia S3 con `PROVIDER_MUTATION_FAILED`,
fase `commit`, effetto `unknown`, retry `requires_recovery`. La VM aveva poco
spazio libero. Sono stati liberati 8,3 GiB di cache di compilazione, conservando
il [report fallito](evidence/2.0.0/large-transfer-first-attempt.json.gz).
L'errore pubblico redatto non prova da solo che la causa fosse lo spazio:
questa rimane una spiegazione plausibile, non un fatto dimostrato.

Sugli stessi byte finali e senza modifiche al codice è stata eseguita una
[nuova campagna completa di due round](evidence/2.0.0/large-transfer-recheck.json.gz),
con esito PASS per tutti i provider. La ricevuta finale consuma questa nuova
prova; il fallimento precedente non è stato trasformato in un successo.
Prestazioni e soak conservano le proprie campagne indipendenti.

## Perimetro conservato

La qualifica riguarda filesystem locale, MinIO, OpenSSH, Pure-FTPd,
pyftpdlib TLS, Azurite, fake-gcs-server, Samba e il profilo WsgiDAV documentato.
AWS S3, Azure Blob e GCS con account reali restano **non qualificati**.
Per WebDAV vale WsgiDAV 4.3.5 con serializzazione applicativa in un processo;
non si estende la prova ad altri server o configurazioni.

I contratti e i limiti di buffering restano quelli della 1.0. Le scansioni
native riportano componenti rilevati e import: non certificano tutte le
librerie incorporate o l'ambiente del consumer. Il soak di due ore resta la
policy provvisoria concordata, non una promessa di affidabilità indefinita.
