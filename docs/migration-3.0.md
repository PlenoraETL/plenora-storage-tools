# Migrazione alla serie 3.0

La 3.0 conserva le sette operazioni, i nove provider, il protocollo CLI 2 e le
chiamate sync/async dello SDK Python. È una major perché rifiuta input che la
2.1 accettava e perché cambia alcune firme Rust pubbliche. Le voci seguenti
elencano tutto ciò che un consumer può dover cambiare; il
[CHANGELOG](../CHANGELOG.md) riporta l'elenco completo.

## Wire e contratti

- `null` non vale più come assente:
  - i metadati runtime facoltativi `plenora.execution.deadline`,
    `plenora.idempotency.key` e `plenora.message.causation_id` vanno omessi
    quando assenti; un `null` è rifiutato in validazione, senza effetti.
    Prima un `deadline: null` avviava l'operazione senza deadline;
  - `tls_ca_pem: null` nelle connessioni FTP/FTPS è rifiutato: lo schema
    tipizza la chiave come stringa;
  - in lettura, le chiavi annullabili ma obbligatorie (`content_type`, `size`
    e `sha256` dei metadati artifact, e quelle dei risultati di oggetti, liste
    e trasferimenti) devono essere presenti, eventualmente con `null`.
- I selettori di versione runtime non canonici (`01`, `+1`) sono rifiutati.
- Il componente adotta `plenora-contracts` alla revisione
  `1e902dfaab5819c1d9ce785878d5b26dbeae48b3`; le deviazioni dichiarate sono in
  [adozione dei contratti](contract-adoption.md).
- Un errore `plenora-error-v1` che contiene `execution_id` ora è letto invece
  di essere rifiutato. Il componente non produce `execution_id`: l'envelope
  d'errore emesso non cambia.

## Rust

- `StorageError` ha il campo pubblico `execution_id`. Il codice che costruisce
  `StorageError` con un literal di struct o lo destruttura in modo esaustivo
  deve aggiungere il campo (o usare `..`); i costruttori
  (`StorageError::new` e i metodi dedicati) restano invariati.
- `plenora-smb2`: gli accessor di `Connection` che leggono lo stato,
  `SmbClient::diagnostics`, `NonceGenerator::next`, la derivazione delle
  chiavi e gli helper crittografici Kerberos restituiscono `Result`; sono
  nuovi `Error::Internal` e `ErrorKind::Internal`. `MockTransport` è
  disponibile solo con la feature `testing`. Le API dei crate
  `plenora-storage-*` non cambiano per questo.
- Un errore interno del provider SMB è riportato con categoria `internal`
  invece di `io`.

## Python

- `PlenoraError` espone `execution_id` (oggi sempre `None` per gli errori
  prodotti dal componente).

## Licenza

Crate, archivi CLI, sorgenti e wheel sono distribuiti con la licenza
proprietaria Plenora ETL (file `LICENSE` alla radice) al posto di
MIT OR Apache-2.0. Il fork `plenora-smb2` conserva la licenza upstream.

## Aggiornamento

Sostituire archivi Rust/CLI e wheel con quelli della release 3.0.0 qualificata,
verificarne i checksum e ripetere il consumer della propria applicazione,
includendo i casi in cui si inviavano `null`. La distribuzione resta
esclusivamente GitHub Releases. La qualifica della 3.0.0 richiede evidenze
nuove sugli artefatti finali: le ricevute della 2.1.0 non la attestano.
