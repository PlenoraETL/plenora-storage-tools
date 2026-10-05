# Inventario delle API pubbliche

<!-- Generato da scripts/render_api_inventory.py. Non modificare a mano. -->

Baseline di sviluppo della serie 1.0: [regole e riproduzione](../api/README.md).
Le righe Rust includono implementazioni derivate e blanket impl; il loro
numero non misura copertura o maturità. I file contengono le firme complete.

## Rust

| Target | Crate | Righe API |
| --- | --- | --- |
| `x86_64-pc-windows-msvc` | [plenora-smb2](../api/rust/x86_64-pc-windows-msvc/plenora-smb2.txt) | 28085 |
| `x86_64-pc-windows-msvc` | [plenora-storage-core](../api/rust/x86_64-pc-windows-msvc/plenora-storage-core.txt) | 5795 |
| `x86_64-pc-windows-msvc` | [plenora-storage-engine](../api/rust/x86_64-pc-windows-msvc/plenora-storage-engine.txt) | 210 |
| `x86_64-pc-windows-msvc` | [plenora-storage-ftp](../api/rust/x86_64-pc-windows-msvc/plenora-storage-ftp.txt) | 307 |
| `x86_64-pc-windows-msvc` | [plenora-storage-providers](../api/rust/x86_64-pc-windows-msvc/plenora-storage-providers.txt) | 1131 |
| `x86_64-pc-windows-msvc` | [plenora-storage-s3](../api/rust/x86_64-pc-windows-msvc/plenora-storage-s3.txt) | 215 |
| `x86_64-pc-windows-msvc` | [plenora-storage-sftp](../api/rust/x86_64-pc-windows-msvc/plenora-storage-sftp.txt) | 203 |
| `x86_64-unknown-linux-gnu` | [plenora-smb2](../api/rust/x86_64-unknown-linux-gnu/plenora-smb2.txt) | 28085 |
| `x86_64-unknown-linux-gnu` | [plenora-storage-core](../api/rust/x86_64-unknown-linux-gnu/plenora-storage-core.txt) | 5795 |
| `x86_64-unknown-linux-gnu` | [plenora-storage-engine](../api/rust/x86_64-unknown-linux-gnu/plenora-storage-engine.txt) | 209 |
| `x86_64-unknown-linux-gnu` | [plenora-storage-ftp](../api/rust/x86_64-unknown-linux-gnu/plenora-storage-ftp.txt) | 307 |
| `x86_64-unknown-linux-gnu` | [plenora-storage-providers](../api/rust/x86_64-unknown-linux-gnu/plenora-storage-providers.txt) | 1131 |
| `x86_64-unknown-linux-gnu` | [plenora-storage-s3](../api/rust/x86_64-unknown-linux-gnu/plenora-storage-s3.txt) | 215 |
| `x86_64-unknown-linux-gnu` | [plenora-storage-sftp](../api/rust/x86_64-unknown-linux-gnu/plenora-storage-sftp.txt) | 202 |

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

Requisiti: Rust **1.92**, Python **>=3.10**.
[Metadati](../api/metadata.json): 31 schemi JSON, feature dei crate e riferimento upstream.

## Gate

[Workflow Rust sui due target](../.github/workflows/api-compatibility.yml),
test CLI e test della wheel nella [CI completa](../.github/workflows/ci.yml).
Il workflow di release richiede gli stessi gate prima del packaging.
