# Contratto pubblico verso la 1.0

Questa baseline descrive il comportamento del codice della serie alfa, non
attesta il completamento di M1 o la compatibilità di una release stabile.
L'inventario di provider, operazioni e contratti resta quello generato in
[STATO.md](STATO.md). Le garanzie dei provider e i limiti di trasferimento
restano vincolati alle prove descritte in [release.md](release.md).

## Superfici e responsabilità

| Aspetto | Rust | CLI | Python |
| --- | --- | --- | --- |
| Ingresso applicativo | `plenora_storage_engine::build_engine` | `plenora-storage --format json` | `Engine` e `AsyncEngine` |
| Operazioni | `test`, `list`, `stat`, `get`, `put`, `copy`, `delete` | Comandi omonimi | Metodi omonimi, con firme sync/async corrispondenti |
| Trasferimenti | Core su `AsyncRead`/`AsyncWrite`; helper `put_from_file` e `get_to_file` | Percorsi `--input` e `--output`, stessi helper Rust | Percorsi `str`/`PathLike[str]`, stessi helper Rust |
| Connessione | `ProviderConnection` | Documento JSON tramite `--connection` | `Connection` |
| Credenziali | `CredentialResolver` dell'host | `EnvironmentCredentialResolver` | Callback sincrono oppure resolver di ambiente |
| Deadline | `ExecutionControl.deadline`, `Instant` monotono | `--deadline` assoluta RFC 3339 | `timeout_ms` relativo all'invocazione nativa |
| Cancellazione | `CancellationToken` | Ctrl+C; SIGTERM su Unix | Token e cancellazione del task asyncio |
| Paginazione | Cursor associato all'engine | `list --all` segue le pagine nello stesso processo | Cursor associato all'engine |

`overwrite`, `publication_policy` e `ignore_missing` sono scelte esplicite
delle operazioni interessate. `atomic_required` non autorizza un fallback
best effort. La CLI usa i valori `atomic-required` e `best-effort`; Rust usa
le varianti dell'enum e Python i nomi JSON con underscore.

La factory e la discovery non risolvono credenziali né aprono connessioni.
I resolver sincroni devono terminare: una deadline cooperativa non interrompe
forzatamente un callback dell'host. Le referenze ai segreti restano nella
configurazione; il loro contenuto viene risolto al momento dell'uso.

## Lifecycle e cancellazione

`close()` è idempotente, impedisce nuove operazioni e invalida i cursor;
non cancella le operazioni già in corso. La discovery resta consultabile.
Python supporta `with Engine()` e `async with AsyncEngine()`;
`AsyncEngine.close()` rimane alias asincrono di `aclose()`.

La cancellazione asyncio attende l'esito del lavoro nativo. Usare
`cancellation_outcome(error)` anche quando `wait_for` restituisce un timeout:
un risultato indica il completamento dell'operazione, un `PlenoraError`
descrive il fallimento definitivo, `None` indica un esito non disponibile.
Quest'ultimo caso non dimostra l'assenza di effetti. Una deadline non garantisce
un limite rigido al tempo necessario per cleanup e riconciliazione.

## Errori ed effetti

`StorageError` Rust è lo stesso documento serializzato in `error` dalla CLI
e tradotto negli attributi di `StorageError` Python, sottoclasse di
`PlenoraError`. I campi decisionali sono `code`, `category`, `phase`,
`remote_effect`, `retry`, `provider` e `details`. Non analizzare il testo di
`message` per decidere il retry. Se un input viola più vincoli, non è garantito
quale validazione venga segnalata per prima.

Le categorie sono definite in
[error.rs](../crates/plenora-storage-core/src/error.rs) e validate contro il
[contratto comune](../contracts/upstream/error-v1.schema.json).
Non modificare la semantica degli effetti quando si traduce un errore:

| Effetto | Interpretazione per il consumer |
| --- | --- |
| `none` | Nessun effetto della chiamata attestato dal percorso eseguito |
| `rolled_back` | Cleanup verificato degli effetti prodotti |
| `partial` | Effetti parziali: usare la disposizione di retry fornita |
| `committed` | Pubblicazione avvenuta; evitare una seconda mutazione cieca |
| `unknown` | Riconciliare lo stato prima di ripetere una mutazione |

Anche staging e directory preparatorie contano come effetti. Per esempio, un
download fallito dopo la creazione dello staging diventa `rolled_back` soltanto
se la sua rimozione riesce; altrimenti resta `unknown` con `requires_recovery`.
Il sink Rust fornito dall'host può contenere byte parziali dopo un errore: la
garanzia di pubblicazione del file locale appartiene agli helper su file.

Gli errori di accesso agli artefatti locali conservano i codici esistenti:

| Punto | Codice | Fase |
| --- | --- | --- |
| Metadati del file sorgente | `INPUT_METADATA_FAILED` | `read` |
| Apertura del file sorgente | `INPUT_OPEN_FAILED` | `read` |
| Creazione del file temporaneo di download | `OUTPUT_STAGING_CREATE_FAILED` | `prepare` |
| Sincronizzazione del file temporaneo | `OUTPUT_STAGING_SYNC_FAILED` | `write` |
| Pubblicazione del download | `OUTPUT_*` secondo la causa | `commit` |

Quando il sistema operativo distingue la causa, un file mancante è
`not_found`, un accesso negato è `authorization`, un conflitto è `conflict`,
disco pieno o quota esaurita sono `resource_limit`; le altre cause restano
`io`. La fase e l'effetto dipendono dal punto di fallimento, non dalla sola
categoria. Nessun testo grezzo dell'errore OS o percorso entra nel messaggio.
Questa classificazione non presume che ogni server remoto distingua le cause.

Gli errori dei callback Python di credenziali, percorsi e serializzazione sono
redatti. Gli input non serializzabili diventano `SDK_INPUT_INVALID`, senza
effetti. Gli errori di sintassi di una chiamata Python, come argomenti mancanti
o keyword sconosciute, restano `TypeError`; `KeyboardInterrupt` e `SystemExit`
non vengono convertiti in errori di configurazione.

## Protocollo ed exit code CLI

La versione del prodotto è distinta dal protocollo CLI v2 e dagli schemi di
operazione v1. In modalità JSON la CLI emette un envelope su una riga; il codice
di uscita non sostituisce i campi di errore.

| Exit code | Categorie |
| --- | --- |
| 0 | Successo |
| 2 | `invalid_configuration` |
| 3 | `unsupported` |
| 4 | `resource_limit` |
| 5 | `io`, `not_found`, `conflict`, `protocol`, `authentication`, `authorization`, `timeout`, `transient` |
| 6 | `execution` |
| 70 | `internal` |
| 130 | `cancelled` |

## Compatibilità e verifiche

Nella serie stabile 1.x richiedono una nuova major: rimozioni o cambiamenti
incompatibili delle firme pubbliche, campi obbligatori, significato di errori
ed effetti, policy di pubblicazione e requisiti minimi Rust/Python. Anche
l'aggiunta di una variante a un enum Rust esaustivo può essere incompatibile.
Le evoluzioni degli schemi devono rispettare i contratti bloccati; una nuova
versione del prodotto non li sostituisce implicitamente. Gli aggiustamenti
della serie alfa vengono tracciati nella [migrazione](migration-1.0.md).

La [CI](../.github/workflows/ci.yml) esegue già i gate di questa baseline:

- `cargo test --workspace --all-targets --locked`: contratti Rust, ciclo di vita,
  mapping degli errori e confronto diretto tra helper Rust ed envelope CLI.
- `scripts/build_python.py` e `scripts/check_installed_sdk.py`: test della wheel
  installata, parità sync/async, redazione e risultati della cancellazione;
  matrice CPython 3.10–3.14 su Windows e Linux.
- `scripts/check_features.py`: discovery coerente con i provider compilati.
- `scripts/check_docs.py`: inventario generato, versioni e link della documentazione.

Restano da chiudere prima del congelamento M1 l'inventario completo delle firme
pubbliche Rust e una verifica automatica di compatibilità rispetto al baseline
stabile scelto. Questi test comportamentali non sostituiscono tale controllo.
