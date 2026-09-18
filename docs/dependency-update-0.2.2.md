# Aggiornamento dipendenze 0.2.2

Le versioni stabili delle dipendenze dirette sono state verificate sui registry
crates.io e PyPI il 19 settembre 2026. I manifest richiedono queste versioni
minime; `Cargo.lock` fissa anche le dipendenze transitive compatibili.
Il workspace e lo SDK Python passano a 0.2.2, mantenendo Rust 1.92 e i contratti v1.

| Area | Dipendenze principali aggiornate |
| --- | --- |
| FTP / FTPS | suppaftp 12.0.1 |
| SFTP | russh 0.63.3, russh-sftp 3.0.0 |
| S3 / HTTP | object_store 0.14.2, reqwest 0.13.5 |
| Validazione / XML | jsonschema 0.56.0, quick-xml 0.42.0 |
| Runtime / CLI | tokio 1.53.1, tokio-util 0.7.19, clap 4.6.7 |
| Integrità / SMB | sha2 0.11.0, aes 0.9.3, aes-gcm 0.11.1, ccm 0.6.1, lz4_flex 0.14.0 |
| Fixture Python | pyftpdlib 2.2.0, pyOpenSSL 26.4.0, WsgiDAV 4.3.5, cheroot 11.1.2, azure-storage-blob 12.30.2 |

PyO3 0.29.2, maturin 1.15.0, rustls 0.23.45 e smb2 upstream 0.22.1
erano già alle versioni stabili correnti. Il fork SMB conserva la correzione
per la negoziazione della cifratura; le modifiche sono descritte nella sua
[provenienza](../crates/plenora-smb2/PROVENANCE.md).
Le dipendenze transitive restano vincolate ai requisiti dei rispettivi upstream:
in particolare `ssh-key` è ancora una release candidate richiesta da russh.

Gli adapter FTP usano ora `TransferStream::finish()` per chiudere il canale dati
e verificare la risposta finale. Per gli upload e le copie FTPS attendono anche
la chiusura TLS del server prima di rilasciare il socket: i test su Windows hanno
rilevato che una chiusura anticipata può troncare trasferimenti oltre 8 MiB.
Il controllo della dimensione pubblicata rimane obbligatorio.
SFTP adatta il controllo della host key alla
nuova API, mantenendo il pin SHA-256 e rifiutando certificati SSH non previsti
dal contratto. La codifica esadecimale SHA-256 rimane invariata con sha2 0.11.
La build Python seleziona la wheel della versione corrente anche quando nella
directory di output rimangono wheel precedenti.

I gate applicabili sono quelli della [CI](../.github/workflows/ci.yml):
test Rust sui due sistemi, fixture remote sulla VM, Clippy, feature isolate,
SDK installato, documentazione, SBOM, advisory, licenze e sorgenti.

Verifica eseguita sul sorgente aggiornato:

- Windows: 1.139 test Rust superati; i nove test riservati alle fixture sono
  eseguiti nella suite Linux.
- Linux sulla VM dedicata: 1.149 test Rust superati, nessuno ignorato.
- CLI e SDK Python: sette operazioni su ciascuno dei nove provider, su entrambi
  i sistemi; 13 test della wheel installata per sistema e verifica asyncio.
- Format, Clippy, undici selezioni di feature (nessuna, nove singole, tutte),
  regressioni e interruzioni, documentazione e quattro test SBOM superati.
- Audit RustSec, controllo del nome upstream SMB, licenze e sorgenti superati
  senza eccezioni o vulnerabilità note.

Log e report locali sono in `target/release-readiness/dependency-update-*`.
Le evidenze della 0.2.1 rimangono storiche: la distribuzione della 0.2.2 richiede
una nuova qualifica degli artefatti finali secondo la [procedura di release](release.md).
