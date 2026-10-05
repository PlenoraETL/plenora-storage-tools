# Migrazione alla serie 3.0

La 3.0 conserva le sette operazioni, i nove provider, il protocollo CLI 2 e le
chiamate sync/async dello SDK Python. È una major perché rifiuta input che la
2.1 accettava e perché cambia alcune firme Rust pubbliche. Le voci seguenti
elencano tutto ciò che un consumer può dover cambiare; il
[CHANGELOG](../CHANGELOG.md) riporta l'elenco completo.

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
  - ogni risultato ha un `message.id` nuovo; `causation_id` è il `message.id`
    della richiesta. Prima il risultato riusava l'id della richiesta;
  - un valore ben formato ma non annunciato (operazione, versione) è
    `unsupported`; un valore assente, malformato o non canonico (per esempio
    il selettore di versione `01` o `+1`) è `protocol`. Tutti i rifiuti prima
    dell'invocazione sono `validate`/`none`/`never`;
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
  - un get che fallisce dopo aver consegnato byte al sink è `partial`/`never`
    (codice `STORAGE_GET_SINK_PARTIAL`); senza byte consegnati resta
    `unknown`/`requires_recovery`;
  - S3, SFTP e FTP: una pubblicazione provata seguita da una rilettura dei
    metadati fallita è `committed`/`cleanup`/`never`.
  I comportamenti marcati come proposta in
  [adozione dei contratti](contract-adoption.md) seguono la bozza
  `plenora-contracts#21`, non ancora normativa.
- Il componente adotta `plenora-contracts` alla revisione
  `1e902dfaab5819c1d9ce785878d5b26dbeae48b3` ed esegue tutti i dodici vettori
  runtime storage; la [matrice di adozione](contract-adoption.md) riporta
  regole ed eventuali deviazioni.
- Un errore `plenora-error-v1` che contiene `execution_id` ora è letto invece
  di essere rifiutato. Il componente non produce `execution_id`: l'envelope
  d'errore emesso non cambia.

## Rust

- `StorageError` ha il campo pubblico `execution_id`. Il codice che costruisce
  `StorageError` con un literal di struct o lo destruttura in modo esaustivo
  deve aggiungere il campo (o usare `..`); i costruttori
  (`StorageError::new` e i metodi dedicati) restano invariati.
- I campi di `RuntimeResultMetadata` che riflettono operazione, versione e
  correlazione sono `Option`, per poter omettere i valori non validi.
- `plenora-smb2`: gli accessor di `Connection` che leggono lo stato,
  `SmbClient::diagnostics`, `NonceGenerator::next`, la derivazione delle
  chiavi e gli helper crittografici Kerberos restituiscono `Result`; sono
  nuovi `Error::Internal` e `ErrorKind::Internal`. `MockTransport` è
  disponibile solo con la feature `testing`. Le API dei crate
  `plenora-storage-*` non cambiano per questo.
- Un errore interno del provider SMB è riportato con categoria `internal`
  invece di `io`.
- `plenora-smb2`: un file ccache Kerberos con un conteggio di componenti che i
  byte restanti non possono contenere è un errore `InvalidData` invece di
  un'allocazione illimitata.

## Python

- `PlenoraError` espone `execution_id` (oggi sempre `None` per gli errori
  prodotti dal componente).

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
includendo i casi in cui si inviavano `null`. La distribuzione resta
esclusivamente GitHub Releases. La qualifica della 3.0.0 richiede evidenze
nuove sugli artefatti finali: le ricevute della 2.1.0 non la attestano.
