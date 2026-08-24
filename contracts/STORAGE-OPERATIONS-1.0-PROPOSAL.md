# Plenora Storage Operations 1.0 — proposta

Stato del contratto: **normative e ratificabile**. Stato dell'artefatto:
**experimental**, fino a una release qualificata.

Questa proposta definisce il bordo osservabile delle sette operazioni storage.
Non è una dichiarazione di conformità, non stabilizza una release e non assegna
semantica provider-independent a funzionalità che i provider non garantiscono.

## Matrice pubblica

| Operazione | Versione | Input | Output | Content type | Superfici target | Side effect massimo | Artifact | Controlli |
|---|---:|---|---|---|---|---|---|---|
| `storage.test` | 1 | `plenora-storage-test-input-v1` | `plenora-storage-test-output-v1` | `application/json` | Rust, CLI, runtime | `none` | nessuno | cancellation, deadline |
| `storage.list` | 1 | `plenora-storage-list-input-v1` | `plenora-storage-list-output-v1` | `application/json` | Rust, CLI, runtime | `none` | nessuno | cancellation, deadline |
| `storage.stat` | 1 | `plenora-storage-stat-input-v1` | `plenora-storage-stat-output-v1` | `application/json` | Rust, CLI, runtime | `none` | nessuno | cancellation, deadline |
| `storage.get` | 1 | `plenora-storage-get-input-v1` | `plenora-storage-get-output-v1` | `application/json` | Rust, CLI, runtime | `remote` | sink opaco | cancellation, deadline |
| `storage.put` | 1 | `plenora-storage-put-input-v1` | `plenora-storage-put-output-v1` | `application/json` | Rust, CLI, runtime | `remote` | source opaco | cancellation, deadline |
| `storage.copy` | 1 | `plenora-storage-copy-input-v1` | `plenora-storage-copy-output-v1` | `application/json` | Rust, CLI, runtime | `remote` | nessuno | cancellation, deadline |
| `storage.delete` | 1 | `plenora-storage-delete-input-v1` | `plenora-storage-delete-output-v1` | `application/json` | Rust, CLI, runtime | `remote` | nessuno | cancellation, deadline |

Tutti gli identificatori `*-v1` sono immutabili: un cambiamento incompatibile
richiede un nuovo identificatore e una nuova versione dell'operazione.
L'idempotency key non è supportata da nessuna operazione v1 e deve essere
rifiutata prima dell'invocazione.

`storage.get` usa la classe conservativa `remote` perché l'artifact sink può
essere una risorsa esterna, anche se la lettura dello storage non muta l'oggetto
sorgente.

## Input comuni e artifact

Ogni input contiene `schema_version: 1` e una `connection` composta da
configurazione non segreta e `credential_ref`. I segreti vengono risolti
soltanto dall'host autorizzato.

Gli envelope runtime persistibili:

- non contengono percorsi locali;
- usano riferimenti `artifact://...` opachi;
- usano `artifact_sink` per `storage.get` e `artifact_source` per
  `storage.put`;
- dichiarano esplicitamente `overwrite` per ogni destinazione;
- non incorporano i byte nel JSON.

La superficie Rust traduce source e sink in `AsyncRead` e `AsyncWrite`. La CLI
li traduce in file locali autorizzati dall'invocazione. Il consumer runtime
risolve i riferimenti opachi e possiede l'adapter verso `runtime-tools`.
`plenora-storage-tools` pubblica DTO, selector e validazione di routing, ma non
implementa né dipende da `CapabilityHandler`.

## Risultati

`test` riporta provider e raggiungibilità. `list` restituisce metadati ordinati
e un cursore opaco provider-neutral. `stat` e `copy` restituiscono metadati
dell'oggetto. `get` e `put` restituiscono byte trasferiti e SHA-256 calcolato
sui byte attraversati dalla superficie. `delete` distingue una cancellazione
effettiva da un missing ignorato tramite `deleted`.

Source, sink e risultati di trasferimento includono metadata bounded:
`content_type`, `size` e `sha256`, tutti nullable quando non conosciuti o non
calcolati. ETag, version ID del provider e SHA-256 sono campi distinti e
opzionali: non sono dichiarati equivalenti e i valori mancanti non vengono
sintetizzati.

## Errori ed esiti ambigui

Ogni errore preserva `category`, `phase`, `remote_effect` e `retry` secondo
`plenora-error-v1`. Gli errori di schema, routing, policy, riferimento o
credenziale sono rilevati prima dell'effetto e usano `remote_effect: none`.

Timeout e cancellazione non provano rollback. Dopo l'inizio di una scrittura,
di una delete, di una copy o della pubblicazione verso un artifact sink,
l'assenza di prova usa `remote_effect: unknown` e
`retry.kind: requires_recovery`. Un sink o oggetto sicuramente parziale può
usare `partial`, sempre senza retry automatico.

Le categorie pubbliche previste comprendono `invalid_configuration`,
`unsupported`, `not_found`, `conflict`, `authentication`, `authorization`,
`timeout`, `cancelled`, `resource_limit`, `io`, `protocol`, `transient`,
`execution` e `internal`. I messaggi sono diagnostici, bounded e redatti.

## Capability Discovery 2.0

Ogni artefatto descrive soltanto la superficie realmente invocabile:

- il crate core pubblica `rust`;
- il binario pubblica `cli`;
- un consumer che implementa e registra l'adapter può pubblicare `runtime`.

Durante questa fase le sette operazioni hanno status `experimental`. Gli
attributi provider riportano soltanto provider compilati e operazioni reali.
La modalità di trasferimento distingue stream Rust, file CLI e artifact
reference runtime.

## Decisioni deliberate

- Provider e credenziali non fanno parte dell'identità dell'operazione.
- `overwrite` non ha default ed è sempre esplicito.
- `ignore_missing` non ha default ed è sempre esplicito.
- Deadline e cancellazione sono cooperative.
- Nessuna operazione v1 accetta idempotency key.
- `overwrite=false` richiede create-if-absent atomico oppure viene rifiutato
  prima della mutazione. Il supporto è dichiarato separatamente per `put` e
  `copy`; nessun provider usa check-then-write come fallback.
- S3 usa primitive condizionali native. SFTP usa `O_EXCL` per il
  create-if-absent e temporary-name + rename soltanto sulle connessioni che
  qualificano `atomic_rename`; `atomic_required` fallisce chiuso altrimenti.
  L'adapter S3 qualifica il `put` condizionale ma rifiuta il `copy`
  create-if-absent, perché la primitiva disponibile non lo garantisce. FTP
  dichiara i limiti e rifiuta sia `overwrite=false` sia `atomic_required`.
- I cursori list sono opachi, bounded a 512 byte, process-local, limitati a
  1024 token attivi e validi 15 minuti. Sono legati a provider, connessione,
  prefix e `max_items`; scope mismatch, scadenza, eviction, chiusura o riavvio
  invalidano il token. Non forniscono snapshot isolation.
- Nessuna composizione cross-component è dichiarata: un artifact reference non
  è, da solo, un contratto di contenuto condiviso. Un futuro edge richiederà
  un contratto reviewed per il contenuto trasferito.
- Python SDK non è una superficie target della proposta v1.

## Decisioni ancora aperte

Non restano decisioni pubbliche bloccanti per il profilo v1. La promozione
dell'artefatto da `experimental` è separata e richiede il punto 30: manifest
v4, digest e release qualificata, esplicitamente fuori da questo lavoro.
