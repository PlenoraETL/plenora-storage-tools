# plenora-storage-tools

Libreria Rust, CLI e SDK Python per accedere a nove sistemi di storage con gli stessi
contratti pubblici: S3-compatible, SFTP, FTP, filesystem locale, FTPS,
Azure Blob / ADLS Gen2, SMB, Google Cloud Storage e WebDAV.

La [release 2.1.0](https://github.com/PlenoraETL/plenora-storage-tools/releases/tag/v2.1.0)
è pubblicata; il [resoconto di qualifica](docs/release-2.1.0.md) riporta prove e limiti.
La 3.0.0 è in preparazione e non è pubblicata: [note](docs/release-notes-3.0.0.md)
e [migrazione](docs/migration-3.0.md) descrivono le incompatibilità.

La [2.1](docs/roadmap-2.1.0.md) aggiunge trasferimenti grandi su cinque provider
tramite [preparazione privata su disco](docs/large-transfers.md), disponibile
come opzione esplicita in Rust, CLI e SDK Python.

Il [perimetro di compatibilità](docs/compatibility-1.0.md), mantenuto nella serie 2.x,
riguarda le fixture dichiarate, tra cui MinIO, Azurite e fake-gcs-server.
Le prove su AWS S3, Azure Blob e GCS reali sono rinviate: i tre adapter restano
disponibili, ma i servizi cloud gestiti non sono ancora qualificati.

L'[indice della documentazione](docs/README.md) raccoglie le guide correnti e
separa i resoconti storici. L'inventario deriva dal codice in
[docs/STATO.md](docs/STATO.md). Il [modello di qualità](docs/database-alignment.md)
segue Database Tools. Il [confronto storico](docs/database-reference-review.md)
descrive la situazione della RC1; [Qualità 2.0](docs/quality-2.0.md) e il
[piano 2.0](docs/roadmap-2.0.0.md) registrano gli interventi e la qualifica.
Configurazione, credenziali, garanzie e sistemi effettivamente verificati sono
nella [guida ai nuovi provider](docs/provider-expansion.md).
La qualifica della release è separata dalla build:
[criteri di distribuzione](docs/release.md).

## Superfici iniziali

| Superficie | Stato | Artefatto |
| --- | --- | --- |
| Rust | disponibile v1 | `plenora-storage-core` + adapter registrati |
| CLI | disponibile v1 | `plenora-storage` |
| Runtime | disponibile v1 | binding transport-neutral; adapter di trasporto posseduto dal consumer |
| Python SDK | sync e asyncio | wheel `plenora-storage`; [guida](crates/plenora-storage-py/README.md) |

Le operazioni iniziali sono `storage.test`, `storage.list`, `storage.stat`,
`storage.get`, `storage.put`, `storage.copy` e `storage.delete`. Il core non
espone tipi MinIO o S3: le differenze di provider sono configurazione
versionata e capability pubbliche.

## Input pubblici

La libreria Rust riceve una `ProviderConnection`, una request tipizzata e un
`ExecutionControl`. Per `put` e `get`, i byte viaggiano rispettivamente come
`AsyncRead` e `AsyncWrite`; non vengono incorporati nel JSON.

La CLI riceve la stessa connessione da un file JSON con `--connection`. I
comandi di trasferimento aggiungono `--input` o `--output` per il file locale.
I file in `docker/*-connection.json` mostrano configurazioni complete per
tutti i provider. `credential_ref` punta a un resolver dell'host; non
contiene il segreto.

Le operazioni v1 non richiedono più `--allow-experimental-contracts`; il flag
resta accettato per compatibilità. Le policy di rete restano indipendenti. I valori
`--overwrite true|false`, `--publication-policy
best-effort|atomic-required` e `--ignore-missing true|false` sono obbligatori
dove applicabili: il protocollo non assume un default per una decisione
distruttiva. FTP e FTPS rifiutano sempre `overwrite=false` e `atomic-required`, invece
di degradare a check-then-write.

Per elenchi su più pagine usare `list --all --max-items 100`: la CLI mantiene
la sessione per l'intera enumerazione e restituisce un solo envelope JSON.
Il totale resta limitato da `--max-list-items`. Senza `--all`, una pagina
incompleta produce un errore esplicito; `--cursor` non è riutilizzabile tra
processi. La libreria Rust conserva la paginazione per Engine.

## Sicurezza

- HTTPS, verifica della host key SSH e reti pubbliche sono il default.
- HTTP, FTP in chiaro, reti private e host key SSH non verificata richiedono
  autorizzazioni separate dell'host.
- L'endpoint viene risolto una sola volta e la connessione usa gli indirizzi
  già validati, quindi una seconda risoluzione non può raggiungere un
  indirizzo che la policy ha appena rifiutato. Per lo stesso motivo il client
  HTTP (S3, Azure, GCS e WebDAV) non segue redirect e non usa proxy, e il canale dati FTP passivo riusa
  l'indirizzo di controllo già validato prendendo dalla risposta PASV soltanto
  la porta.
- Le richieste contengono `credential_ref`, mai access key o segreti inline. Il
  divieto è applicato dal core su ogni operazione, non solo dal runtime, e la
  configurazione è una mappa piatta di valori scalari, così un segreto non può
  nascondersi sotto il livello in cui i nomi vengono ispezionati.
- Gli errori pubblici sono tipizzati e redatti.
- Upload e download hanno limiti espliciti e non pubblicano file locali
  parziali. Con `--overwrite false` la pubblicazione crea la destinazione con
  un link atomico che fallisce se il nome è già occupato: non esiste finestra
  di probe e nessun file non creato da questo comando viene mai sostituito o
  rimosso. Richiede un filesystem che supporti gli hard link.
- Le upload condizionali (`overwrite=false` su S3) devono essere bufferizzate in
  memoria. Nella modalità predefinita anche local, Azure, GCS, SMB e WebDAV
  bufferizzano put/copy, con limite `--max-buffered-put-bytes`. Per questi cinque
  provider `--spool-uploads` abilita la preparazione su disco e usa il limite
  totale `--max-transfer-bytes`, conservando i vincoli di pubblicazione del provider.
- `copy` rifiuta sorgente e destinazione uguali prima di qualunque mutazione.

## Sviluppo Docker

Il gate locale usa MinIO, OpenSSH/SFTP, Pure-FTPd, un job one-shot che prepara
il bucket e un container Rust. Il runner Rust è one-shot; i tre server di test
restano in esecuzione finché non vengono fermati con `docker compose down`.

```powershell
docker compose build storage-rust
bash scripts/prepare-fixtures.sh
bash scripts/prepare-extended-fixtures.sh
docker compose -f docker-compose.yml -f compose.extended.yml run --rm --no-deps storage-rust
```

La preparazione richiede Bash, OpenSSL e Docker Compose (su Windows usare
WSL o la VM). Genera una CA temporanea per MinIO HTTPS e legge il fingerprint
SSH della fixture. `verify.sh` abilita anche i test `ignored`, verifica le
sette operazioni sui nove provider e controlla conflitti e limiti. Senza
fixture, `cargo test` segnala esplicitamente i test d'integrazione esclusi.

Per probe manuali:

```powershell
docker compose run --rm --no-deps storage-rust target/debug/plenora-storage --format json --allow-experimental-contracts --allow-insecure-http --allow-private-network test --connection docker/minio-connection.json
docker compose run --rm --no-deps storage-rust target/debug/plenora-storage --format json --allow-experimental-contracts --allow-private-network --allow-unverified-ssh test --connection docker/sftp-connection.json
docker compose run --rm --no-deps storage-rust target/debug/plenora-storage --format json --allow-experimental-contracts --allow-private-network --allow-insecure-ftp test --connection docker/ftp-connection.json
```

La console MinIO è esposta su `http://localhost:9001`; l'API S3 è esposta su
`http://localhost:9000` e raggiunta dal container Rust come
`http://minio:9000`. SFTP è esposto su `localhost:2222`; FTP su
`localhost:2121`, con porte passive `30000-30009`.

## Verifica Rust

```powershell
cargo fmt --all -- --check
cargo clippy --workspace --all-targets --all-features --locked -- -D warnings
python scripts/check_anti_panic.py
cargo test --workspace --all-targets --locked
```

## Contratti

I contratti component-owned vivono sotto `contracts/`. Il profilo comune di
riferimento è `plenora-storage-tools-profile-v1` in `plenora-contracts`. La
matrice e le decisioni ratificabili sono in
`contracts/STORAGE-OPERATIONS-1.0-PROPOSAL.md`; esempi validi e invalidi sono
eseguiti dai test black-box del crate core. Ogni build produce un manifest di
adozione v4 associato ai digest dei crate e della CLI. La
[matrice di adozione](docs/contract-adoption.md) descrive regole ed evidenze.

## Licenza

Licenza proprietaria: Copyright (c) Plenora ETL. All rights reserved. Il testo
è in [`LICENSE`](LICENSE) e accompagna crate, archivi CLI e sorgenti
distribuiti. Il fork `crates/plenora-smb2` deriva dal crate `smb2` e conserva
la licenza upstream MIT OR Apache-2.0 con i relativi testi
([provenienza](crates/plenora-smb2/PROVENANCE.md)).

## Candidati di release

`cargo fetch --locked`, poi `python scripts/build_release.py` producono binari,
crate Rust, wheel Python testata, SBOM, contratti e SHA-256 in `dist/`. Installare
prima `maturin==1.15.0`. Il flag `--allow-dirty` serve solo
per candidati locali da modifiche non committate. La verifica compila un
consumer esterno e la CLI dagli archivi estratti. La procedura operativa e i
limiti supportati sono in [docs/release.md](docs/release.md).

La distribuzione richiede `release-qualification.json` con stato
`qualified_for_publication`, generato solo dopo i gate sul commit definitivo.
Una build riuscita da sola non sostituisce questa qualifica.
