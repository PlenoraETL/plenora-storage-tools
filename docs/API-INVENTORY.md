# Inventario delle API pubbliche

<!-- Generato da scripts/render_api_inventory.py. Non modificare a mano. -->

Baseline di sviluppo della serie 1.0: [regole e riproduzione](../api/README.md).
Le righe Rust includono implementazioni derivate e blanket impl; il loro
numero non misura copertura o maturità. I file contengono le firme complete.

## Rust

| Target | Crate | Righe API |
| --- | --- | --- |
| `x86_64-pc-windows-msvc` | [plenora-smb2](../api/rust/x86_64-pc-windows-msvc/plenora-smb2.txt) | 28468 |
| `x86_64-pc-windows-msvc` | [plenora-storage-core](../api/rust/x86_64-pc-windows-msvc/plenora-storage-core.txt) | 5864 |
| `x86_64-pc-windows-msvc` | [plenora-storage-engine](../api/rust/x86_64-pc-windows-msvc/plenora-storage-engine.txt) | 213 |
| `x86_64-pc-windows-msvc` | [plenora-storage-ftp](../api/rust/x86_64-pc-windows-msvc/plenora-storage-ftp.txt) | 311 |
| `x86_64-pc-windows-msvc` | [plenora-storage-providers](../api/rust/x86_64-pc-windows-msvc/plenora-storage-providers.txt) | 1142 |
| `x86_64-pc-windows-msvc` | [plenora-storage-s3](../api/rust/x86_64-pc-windows-msvc/plenora-storage-s3.txt) | 218 |
| `x86_64-pc-windows-msvc` | [plenora-storage-sftp](../api/rust/x86_64-pc-windows-msvc/plenora-storage-sftp.txt) | 205 |
| `x86_64-unknown-linux-gnu` | [plenora-smb2](../api/rust/x86_64-unknown-linux-gnu/plenora-smb2.txt) | 28468 |
| `x86_64-unknown-linux-gnu` | [plenora-storage-core](../api/rust/x86_64-unknown-linux-gnu/plenora-storage-core.txt) | 5864 |
| `x86_64-unknown-linux-gnu` | [plenora-storage-engine](../api/rust/x86_64-unknown-linux-gnu/plenora-storage-engine.txt) | 212 |
| `x86_64-unknown-linux-gnu` | [plenora-storage-ftp](../api/rust/x86_64-unknown-linux-gnu/plenora-storage-ftp.txt) | 311 |
| `x86_64-unknown-linux-gnu` | [plenora-storage-providers](../api/rust/x86_64-unknown-linux-gnu/plenora-storage-providers.txt) | 1142 |
| `x86_64-unknown-linux-gnu` | [plenora-storage-s3](../api/rust/x86_64-unknown-linux-gnu/plenora-storage-s3.txt) | 218 |
| `x86_64-unknown-linux-gnu` | [plenora-storage-sftp](../api/rust/x86_64-unknown-linux-gnu/plenora-storage-sftp.txt) | 204 |

## Python

La [baseline della wheel](../api/python.json) comprende:

- `AsyncEngine`: class.
- `CancellationToken`: class.
- `Connection`: class.
- `Engine`: class.
- `EngineConfig`: class.
- `PlenoraError`: class.
- `StorageError`: class.
- `__version__`: str.
- `cancellation_outcome`: function.
- `version`: function.

Gli alias usati nelle annotazioni e gli attributi di errore sono inclusi
nello stesso snapshot; la versione della wheel è verificata separatamente.

## CLI e contratti

Protocollo CLI: **2**. [Modello compilato](../api/cli.json)
con parametri, default, obbligatorietà, valori ammessi ed exit code.

Comandi: `capabilities`, `test`, `list`, `stat`, `get`, `put`, `copy`, `delete`, `help`.

Requisiti: Rust **1.98**, Python **>=3.10**.
[Metadati](../api/metadata.json): 32 schemi JSON, feature dei crate e riferimento upstream.

## Gate

[Workflow Rust sui due target](../.github/workflows/api-compatibility.yml),
test CLI e test della wheel nella [CI completa](../.github/workflows/ci.yml).
Il workflow di release richiede gli stessi gate prima del packaging.
