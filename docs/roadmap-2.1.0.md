# Piano 2.1.0: trasferimenti grandi sui provider aggiuntivi

Stato: completato; la [2.1.0 è pubblicata](release-2.1.0.md) con qualifica e
verifica degli asset scaricati. Le prove di sviluppo seguenti conservano il
proprio ambito e non vengono riutilizzate come evidenze degli artefatti finali.

## Obiettivo e compatibilità

Consentire put e copy da almeno 1 GiB su local, Azure, GCS, SMB e WebDAV con
memoria limitata, su Rust, CLI e wheel Python. Le firme e le configurazioni
esistenti devono conservare significato: in particolare, non ignorare
silenziosamente `max_buffered_put_bytes` impostato da un consumer.

La strategia implementata è un'opzione esplicita e additiva per trasferimenti
preparati su disco. Le chiamate esistenti mantengono il comportamento buffered;
la nuova opzione usa `max_transfer_bytes` per il totale e buffer piccoli per
lettura e invio. Il file temporaneo privato permette di verificare lunghezza,
limite e checksum prima di modificare la destinazione. Non è un protocollo di
resume e non deve essere presentato come tale.

Il motore deve esporre una factory additiva e discovery coerente con la
strategia selezionata. CLI e SDK devono usare quella factory; nessuna nuova
implementazione del trasferimento nelle superfici. Evitare campi obbligatori
aggiunti a struct Rust pubbliche costruibili dai consumer.

## Lavoro per backend

| Provider | Implementazione da provare | Vincolo da conservare |
| --- | --- | --- |
| Local | Copia a blocchi dal file preparato allo staging nella directory capability | Rename/hard-link atomico; nessuna fuga dalla root; cleanup del solo staging posseduto |
| Azure | Singolo Put Blob condizionale letto dal file, massimo 5.000 MiB | `overwrite=false` controllato dal server; metadati; esito ambiguo dopo invio senza retry automatico |
| GCS | Corpo multipart letto a blocchi dal file preparato | Boundary assente nei dati anche tra blocchi; `ifGenerationMatch=0`; risposta riferita alla chiave e lunghezza attese |
| SMB | Writer a blocchi dal file preparato | Creazione esclusiva quando richiesta; nessuna nuova promessa di atomicità |
| WebDAV | Body HTTP a blocchi e lunghezza esplicita | `If-None-Match: *`; server della matrice verificata; nessuna nuova promessa di atomicità |

`object_store` 0.14.2 non espone una modalità create-if-absent in
`PutMultipartOptions`. Non basta sostituire `put_opts` con multipart:
la pubblicazione Azure usa quindi la
[API Put Blob](https://learn.microsoft.com/en-us/rest/api/storageservices/put-blob),
con condizione `If-None-Match: *` e limite di 5.000 MiB della versione REST
selezionata. Non vengono creati blocchi multipart da riprendere o ripulire.
Firma SharedKey e token Bearer richiedono prove distinte; i test HTTP locali
non sostituiscono la verifica della firma su Azurite o su Azure reale.

La [API GCS objects.insert](https://docs.cloud.google.com/storage/docs/json_api/v1/objects/insert)
consente dati e metadati in una richiesta multipart. La qualifica deve restare
distinta da una prova su account GCS reale.

## Gate di accettazione

- API Rust e contratti pubblici compatibili; vecchi consumer senza modifiche.
- Test unitari: sorgente interrotta, lunghezza falsa, limite totale, checksum,
  file temporaneo privato e cleanup, inclusa cancellazione prima della mutazione.
- Fixture: put/copy/get da 1 GiB con checksum su tutti e cinque i provider;
  creazione concorrente senza overwrite e destinazione precedente conservata.
- Fault injection: spazio temporaneo esaurito, rete interrotta durante upload
  e commit, risposta perduta, cleanup fallito. Verificare categoria, fase,
  effetto e retry e assenza di payload o credenziali negli errori pubblici.
- Misure di RSS sotto 256 MiB per processo nelle prove grandi; campagne con
  4 e 16 worker su payload piccoli, con riserva disco prima dell'avvio.
- SDK installato e CLI sui due target; matrice Python 3.10–3.14, typing,
  coverage, fuzz e scansione degli artefatti con gli stessi gate della 2.0.
- Soak di due ore sugli artefatti finali, prove delle distribuzioni scaricate
  e pubblicazione solo su GitHub Releases.

Ogni gate nuovo deve entrare nei workflow esistenti e nel validatore finale.
I test dei vecchi limiti restano attivi nella modalità predefinita; le prove
della nuova modalità devono essere distinte e vincolate ai digest finali.
Nessun account cloud reale o server commerciale è qualificato per inferenza.

## Prove di sviluppo precedenti alla qualifica

Le tre superfici espongono l'opzione descritta nella
[guida ai trasferimenti grandi](large-transfers.md). I test locali Windows
esercitano preparazione, limiti, checksum, pubblicazione local, SDK installato
e fault HTTP; questi risultati di sviluppo non qualificano una distribuzione.
Il commit di sviluppo `dfdcd9ca977336f50d82ec6b03862fa6f5268840` ha superato
la [CI completa](https://github.com/PlenoraETL/plenora-storage-tools/actions/runs/36573404990).
Sulla VM dedicata sono passati le sei fixture aggiuntive in modalità privata,
due round da 1 GiB su tutti e nove i provider, 72 trasferimenti con 4 worker
e 144 con 16 worker. Nei cinque nuovi percorsi il picco RSS rilevato è
21.241.856 byte. Lo SDK installato ha completato una prova diagnostica di
121,266 secondi, con tre cicli per ciascuna modalità: non è il soak di rilascio.
All'epoca restavano obbligatorie tutte le prove sui nuovi artefatti finali,
incluso il soak di due ore. I report di sviluppo non sono stati riutilizzati
come ricevuta finale; il [resoconto della release](release-2.1.0.md) descrive
le campagne complete e i tentativi falliti conservati.

La qualifica per target produce tre report separati `spooled-*.json`.
Il validatore della release li richiede dalla 2.1, insieme ai trasferimenti
`transfers-spooled/` e al soak con entrambe le modalità nello stesso intervallo
di due ore. Le prove della modalità predefinita restano obbligatorie.

Il budget del crate providers viene rivisto per i moduli comuni di preparazione
e per i cinque percorsi di pubblicazione da file: 3.600 righe di codice e
4.200 fisiche, mantenendo il limite di 400 righe di codice per file. Il codice
comune possiede il temporaneo e i controlli; i backend possiedono il protocollo.
Gli altri budget rimangono invariati.
