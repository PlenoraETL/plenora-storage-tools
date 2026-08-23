# plenora-storage-tools

Libreria Rust provider-neutral per accedere a sistemi di storage attraverso
contratti pubblici versionati. Gli adapter iniziali sono SFTP, FTP e
S3-compatible. La conformance locale usa container reali per tutti e tre.

> **Stato del progetto:** sperimentale. Contratti e API possono cambiare; non
> viene ancora dichiarata alcuna garanzia di compatibilità con prodotti o
> versioni specifiche dei provider.

## Superfici iniziali

| Superficie | Stato | Artefatto |
| --- | --- | --- |
| Rust | iniziale | `plenora-storage-core` + adapter registrati |
| CLI | iniziale | `plenora-storage` |
| Runtime | pianificata | riferimenti credential/artifact opachi |
| Python SDK | pianificata | successiva alla stabilizzazione dei contratti |

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
SFTP, FTP e S3-compatible. `credential_ref` punta a un resolver dell'host; non
contiene il segreto.

## Sicurezza

- HTTPS, verifica della host key SSH e reti pubbliche sono il default.
- HTTP, FTP in chiaro, reti private e host key SSH non verificata richiedono
  autorizzazioni separate dell'host.
- Le richieste contengono `credential_ref`, mai access key o segreti inline.
- Gli errori pubblici sono tipizzati e redatti.
- Upload e download hanno limiti espliciti e non pubblicano file locali
  parziali.

## Sviluppo Docker

Il gate locale usa MinIO, OpenSSH/SFTP, Pure-FTPd, un job one-shot che prepara
il bucket e un container Rust. Il runner Rust è one-shot; i tre server di test
restano in esecuzione finché non vengono fermati con `docker compose down`.

```powershell
docker compose build storage-rust
docker compose up -d --wait minio sftp ftp
docker compose run --rm minio-init
docker compose run --rm --no-deps storage-rust
```

Per probe manuali:

```powershell
docker compose run --rm --no-deps storage-rust target/debug/plenora-storage --format json --allow-insecure-http --allow-private-network test --connection docker/minio-connection.json
docker compose run --rm --no-deps storage-rust target/debug/plenora-storage --format json --allow-private-network --allow-unverified-ssh test --connection docker/sftp-connection.json
docker compose run --rm --no-deps storage-rust target/debug/plenora-storage --format json --allow-private-network --allow-insecure-ftp test --connection docker/ftp-connection.json
```

La console MinIO è esposta su `http://localhost:9001`; l'API S3 è esposta su
`http://localhost:9000` e raggiunta dal container Rust come
`http://minio:9000`. SFTP è esposto su `localhost:2222`; FTP su
`localhost:2121`, con porte passive `30000-30009`.

## Verifica Rust

```powershell
cargo fmt --all -- --check
cargo clippy --workspace --all-targets --all-features --locked -- -D warnings
cargo test --workspace --all-targets --locked
```

## Contratti

I contratti component-owned vivono sotto `contracts/`. Il profilo comune di
riferimento è `plenora-storage-tools-profile-v1` in `plenora-contracts`.

## Licenza

MIT OR Apache-2.0.
