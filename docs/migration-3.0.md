# Migrazione alla serie 3.0

La 3.0 conserva le sette operazioni, i nove provider, il protocollo CLI 2 e le
chiamate sync/async dello SDK Python. È una major perché rifiuta input che la
2.1 accettava, cambia il wire del binding runtime, cambia firme Rust pubbliche
e alza la versione minima di Rust. Le voci seguenti elencano ciò che un
consumer può dover cambiare; il [CHANGELOG](../CHANGELOG.md) riporta l'elenco
completo.

## Wire e contratti

- `null` non vale più come assente:
  - i metadati runtime facoltativi (`plenora.execution.deadline`,
    `plenora.message.causation_id`) vanno omessi quando assenti; un `null` è
    rifiutato con `protocol` in validazione, senza effetti. Prima un
    `deadline: null` avviava l'operazione senza deadline;
  - `tls_ca_pem: null` nelle connessioni FTP/FTPS è rifiutato: lo schema
    tipizza la chiave come stringa;
  - in lettura, le chiavi annullabili ma obbligatorie (`content_type`, `size`
    e `sha256` dei metadati artifact, e quelle dei risultati di oggetti, liste
    e trasferimenti) devono essere presenti, eventualmente con `null`.
- Binding runtime allineato alla matrice comune delle librerie Plenora:
  - ogni risultato ha un `message.id` nuovo, un UUID v4 dalla sorgente casuale
    del sistema operativo, e `causation_id` uguale al `message.id` della
    richiesta. Prima il risultato riusava l'id della richiesta, quindi due
    richieste senza id producevano risultati con lo stesso id;
  - un valore ben formato ma non annunciato (operazione, versione, capability,
    contratto d'ingresso) è `unsupported`; un valore assente, malformato o non
    canonico (per esempio il selettore di versione `01` o `+1`) è `protocol`.
    Tutti i rifiuti prima dell'invocazione sono `validate`/`none`/`never`;
  - nei metadati di un rifiuto operazione, versione e correlazione compaiono
    solo se ben formati, altrimenti la chiave è omessa (non più `"0"`,
    `storage.unknown` o l'UUID nullo);
  - la chiave di idempotenza è `plenora.execution.idempotency_key`; storage
    non supporta l'idempotenza, quindi la sua presenza è rifiutata con
    `unsupported` (vuota: `protocol`). `plenora.idempotency.key` non è più
    letta; come ogni chiave `plenora.*` non riservata, è ignorata;
  - la deadline accetta ogni grafia RFC 3339 di UTC (`Z`/`z`, `+00:00`,
    `T`/`t`, frazioni) e rifiuta gli altri offset e `-00:00`. Una deadline già
    scaduta è `timeout` con retry `never` (prima `safe`), anche da CLI e SDK
    Python. Una deadline anche nel payload è `invalid_configuration`;
  - S3, SFTP e FTP: una pubblicazione provata seguita da una rilettura dei
    metadati fallita è `committed`/`cleanup`/`never`.
  I comportamenti marcati come proposta in
  [adozione dei contratti](contract-adoption.md) seguono la bozza
  `plenora-contracts#21`, non ancora normativa.
- Un get il cui sink accetta parte del trasferimento e poi fallisce (scrittura
  o flush) è `STORAGE_GET_SINK_PARTIAL` con `partial`/`never` per tutti i
  provider, con la categoria e la fase del guasto del sink (per esempio
  `resource_limit` per il disco pieno). Senza byte consegnati resta
  `unknown`/`requires_recovery`. CLI e SDK ricevono lo stesso codice, con
  `rolled_back` perché il file temporaneo viene rimosso.
- Il componente adotta `plenora-contracts` alla revisione
  `1e902dfaab5819c1d9ce785878d5b26dbeae48b3` ed esegue tutti i dodici vettori
  runtime storage, senza deviazioni dichiarate; la
  [matrice di adozione](contract-adoption.md) riporta regole e prove.
- Un errore `plenora-error-v1` che contiene `execution_id` ora è letto invece
  di essere rifiutato. Il componente non produce `execution_id`: l'envelope
  d'errore emesso non cambia.

## Rust

- La versione minima di Rust (`rust-version`) passa da 1.92 a 1.98; la
  toolchain di build e qualifica è 1.98.1.
- `StorageError` ha il campo pubblico `execution_id`. Il codice che costruisce
  `StorageError` con un literal di struct o lo destruttura in modo esaustivo
  deve aggiungere il campo (o usare `..`); i costruttori
  (`StorageError::new` e i metodi dedicati) restano invariati.
- `RuntimeBinding::invoke` e `invoke_json` restituiscono
  `StorageResult<RuntimeResultEnvelope>`: falliscono solo con
  `RUNTIME_RESULT_IDENTITY_UNAVAILABLE` quando la sorgente casuale non è
  disponibile, prima di qualunque invocazione.
- I campi di `RuntimeResultMetadata` che riflettono operazione, versione e
  correlazione sono `Option`, per poter omettere i valori non validi.
- `plenora-smb2`:
  - gli accessor di `Connection` che leggono lo stato,
    `SmbClient::diagnostics`, `NonceGenerator::next`, la derivazione delle
    chiavi e gli helper crittografici Kerberos restituiscono `Result`; sono
    nuovi `Error::Internal` e `ErrorKind::Internal`;
  - le letture di un file intero (`Tree::read_file_pipelined`,
    `Tree::read_file_pipelined_with_progress`, `SmbClient::read_file_pipelined`,
    `SmbClient::read_file_with_progress`, `FileDownload::collect`,
    `FileDownload::collect_with_progress`) richiedono un limite `max_bytes`;
    una dimensione dichiarata dal server oltre il limite, o un server che
    invia più del limite, è il nuovo `Error::DeclaredSizeOverLimit`
    (`ErrorKind::TooLarge`) prima di riservare memoria;
  - il cifrario Kerberos segue l'etype dichiarato: una chiave di lunghezza
    diversa da quella dell'etype è `InvalidData`. È nuovo
    `EncryptionType::key_len`;
  - i decoder (NEGOTIATE, LOCK, copychunk, srvsvc, ccache Kerberos) rifiutano
    con `InvalidData` un conteggio che i byte ricevuti non possono contenere,
    invece di riservare memoria per esso;
  - `MockTransport` è disponibile solo con la feature `testing`.
  Le API dei crate `plenora-storage-*` non cambiano per queste voci.
- Un errore interno del provider SMB è riportato con categoria `internal`
  invece di `io`.
- Le dipendenze `async-trait`, `bytes`, `serde`, `serde_json`, `tokio` e
  `tokio-util` restano requisiti compatibili (deviazione dichiarata
  `DEP-RANGE-1`), così un consumer può risolvere la propria patch.

## Python

- `PlenoraError` espone `execution_id` (oggi sempre `None` per gli errori
  prodotti dal componente).
- Un download il cui file di destinazione fallisce dopo aver ricevuto parte dei
  byte riporta `STORAGE_GET_SINK_PARTIAL`.

## Licenza

Crate, archivi CLI, sorgenti e wheel sono distribuiti con la licenza
proprietaria Plenora ETL (file `LICENSE` alla radice) al posto di
MIT OR Apache-2.0. Il fork `plenora-smb2` conserva la licenza upstream.

## Distribuzione

Ogni release pubblica anche, per ciascun target, l'SBOM CycloneDX e il
manifest di adozione dei contratti come asset separati. CLI, wheel e sorgenti
hanno attestazioni di provenienza SLSA e SBOM firmate dal workflow che li
compila; la pubblicazione le verifica prima di procedere.

## Aggiornamento

Sostituire archivi Rust/CLI e wheel con quelli della release 3.0.0 qualificata,
verificarne i checksum e ripetere il consumer della propria applicazione,
includendo i casi in cui si inviavano `null`, deadline con offset e la chiave
di idempotenza. La distribuzione resta esclusivamente GitHub Releases. La
qualifica della 3.0.0 richiede evidenze nuove sugli artefatti finali: le
ricevute della 2.1.0 non la attestano.
