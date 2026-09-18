# Stato del prodotto

<!-- Generato da scripts/render_state.py. Non modificare a mano. -->

Versione sorgente: `0.2.1`. Rust minimo: `1.92`.

Questo inventario descrive il codice compilato. Non certifica una release:
la qualifica richiede le evidenze vincolate al commit e ai digest degli artefatti.

## Crate del workspace

| Crate | Versione |
| --- | --- |
| `plenora-storage-core` | `0.2.1` |
| `plenora-storage-s3` | `0.2.1` |
| `plenora-storage-sftp` | `0.2.1` |
| `plenora-storage-ftp` | `0.2.1` |
| `plenora-storage-providers` | `0.2.1` |
| `plenora-smb2` | `0.2.1` |
| `plenora-storage-cli` | `0.2.1` |
| `plenora-storage-engine` | `0.2.1` |
| `plenora-storage-py` | `0.2.1` |

## Provider compilati nella distribuzione completa

| Feature | Contratto di connessione | Operazioni |
| --- | --- | --- |
| `azure` | `plenora-storage-azure-connection-v1` | 7 |
| `ftp` | `plenora-storage-ftp-connection-v1` | 7 |
| `ftps` | `plenora-storage-ftps-connection-v1` | 7 |
| `gcs` | `plenora-storage-gcs-connection-v1` | 7 |
| `local` | `plenora-storage-local-connection-v1` | 7 |
| `s3` | `plenora-storage-s3-connection-v1` | 7 |
| `sftp` | `plenora-storage-sftp-connection-v1` | 7 |
| `smb` | `plenora-storage-smb-connection-v1` | 7 |
| `webdav` | `plenora-storage-webdav-connection-v1` | 7 |

Le feature sono condivise da engine, CLI e binding Python. `default = full`;
`--no-default-features --features local,s3` compila solo i provider richiesti.

## Operazioni

| Operazione | Input | Output |
| --- | --- | --- |
| `storage.test` | `plenora-storage-test-input-v1` | `plenora-storage-test-output-v1` |
| `storage.list` | `plenora-storage-list-input-v1` | `plenora-storage-list-output-v1` |
| `storage.stat` | `plenora-storage-stat-input-v1` | `plenora-storage-stat-output-v1` |
| `storage.get` | `plenora-storage-get-input-v1` | `plenora-storage-get-output-v1` |
| `storage.put` | `plenora-storage-put-input-v1` | `plenora-storage-put-output-v1` |
| `storage.copy` | `plenora-storage-copy-input-v1` | `plenora-storage-copy-output-v1` |
| `storage.delete` | `plenora-storage-delete-input-v1` | `plenora-storage-delete-output-v1` |

## Contratti bloccati

```json
{
  "repository": "https://github.com/PlenoraETL/plenora-contracts.git",
  "revision": "f811f21f072b34896efdb6e110bee34d756153df"
}
```

## Superfici e limiti

- Rust: core neutrale e factory applicativa `plenora_storage_engine::build_engine`.
- CLI: protocollo JSON e trasferimenti su file.
- Runtime Binding: nel core, con resolver posseduti dal consumer.
- Python: wheel PyO3, API sincrona e asyncio, trasferimenti su file e tipi PEP 561.
- Il catalogo esposto da Python descrive i provider Rust della wheel;
  non dichiara automaticamente conformità al profilo Python upstream.
- Limiti, sistemi qualificati e prove richieste: [allineamento](database-alignment.md),
  [provider](provider-expansion.md) e [release](release.md).
