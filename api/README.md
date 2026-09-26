# Baseline delle API

Questa baseline nasce dal codice `58fb010f88d896fd463a29accca3e4ac74946480`
della serie **1.0.0-alpha.1**, con Rust **1.92.0** e `public-api` **0.51.0**.
È un riferimento di compatibilità durante lo sviluppo; non è una qualifica
di produzione e non implica che esista già una release stabile.

- `rust/<target>/*.txt`: superficie risolta da rustdoc di tutti i sette crate
  pubblici con tutte le feature, inclusi metodi, re-export, trait e auto-trait.
  Il fork SMB ha un file distinto; non viene nascosto nel totale del prodotto.
- `cli.json`: modello Clap compilato, valori ammessi, obbligatorietà, default,
  protocollo ed exit code. Non include il testo descrittivo dell'help.
- `python.json`: esportazioni, firme, annotazioni, type alias, dataclass,
  proprietà, API del token e attributi degli errori della wheel installata.
- `metadata.json`: feature dichiarate, requisiti Rust/Python e digest canonici
  degli schemi JSON del repository, compresi i contratti upstream bloccati.

Il gate è conservativo: blocca **ogni differenza**, anche un'aggiunta che potrebbe
essere compatibile. Non pretende di risolvere automaticamente tutte le regole
SemVer. Le prove di comportamento rimangono obbligatorie per effetti, retry,
atomicità e significato dei risultati; l'uguaglianza delle firme non li dimostra.
Le combinazioni arbitrarie di feature non sono tutte inventariate: gli snapshot
Rust riguardano `--all-features`; la matrice delle feature singole resta il gate
separato `scripts/check_features.py`.

## Riproduzione

```sh
cargo fetch --locked
cargo fetch --locked --manifest-path tools/api-inventory/Cargo.toml
python scripts/check_rust_api.py
python scripts/test_rust_api_gate.py
python scripts/check_api_metadata.py
cargo test --locked -p plenora-storage-cli --bin plenora-storage
# Usare l'interprete nel quale la wheel è stata installata:
python -I scripts/check_python_api.py
```

I report e i diff Rust sono sotto `target/api-current`. Il generatore abilita
`RUSTC_BOOTSTRAP=1` soltanto nei subprocessi di documentazione per ottenere il
JSON rustdoc con il compilatore stabile fissato. Non modifica i flag delle build
di prodotto. Il formato JSON è instabile: aggiornamenti di rustc o del parser
richiedono una nuova verifica della baseline su entrambi i target.

Le dipendenze dello strumento sono isolate in `tools/api-inventory/Cargo.lock`;
non vengono collegate alla libreria, alla CLI o alla wheel.
Il parser è [public-api](https://github.com/cargo-public-api/cargo-public-api/tree/v0.51.0/public-api).
I riferimenti ai tipi esterni restano nomi qualificati; il gate non inventaria
le intere API delle dipendenze. Un riferimento locale non risolto interrompe
la generazione invece di produrre uno snapshot incompleto.

## Cambiamenti intenzionali

Per esaminare un cambiamento, generare file candidati:

```sh
python scripts/check_rust_api.py --candidate
python scripts/snapshot_cli_api.py
python -I scripts/check_python_api.py --candidate target/api-current/python.json
python scripts/check_api_metadata.py --candidate target/api-current/metadata.json
```

Questi comandi non riscrivono la baseline. Esaminare i diff, classificare la
compatibilità, aggiornare i test e la guida di migrazione, quindi includere i
file approvati in una modifica esplicita. Un'incompatibilità nella serie stabile
1.x richiede una nuova major; non la si rende compatibile ricopiando lo snapshot.
All'uscita della 1.0 si conserverà anche il riferimento al suo commit qualificato.
Le future verifiche devono preservare quel riferimento, non rincorrere `HEAD`.

I gate vengono eseguiti in [CI](../.github/workflows/ci.yml) e prima del
[packaging della release](../.github/workflows/release-candidate.yml).
Le regressioni negative compilano piccole API modificate per dimostrare che
metodi rimossi, tipi cambiati, enum estesi e auto-trait persi vengono rilevati.
