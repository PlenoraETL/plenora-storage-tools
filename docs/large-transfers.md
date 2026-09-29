# Trasferimenti preparati su disco: 2.1

Questa funzionalità è implementata e richiede ancora la qualifica sugli
artefatti finali. Si abilita esplicitamente per put e copy su local,
Azure, GCS, SMB e WebDAV. Le chiamate esistenti conservano la preparazione in
memoria e il limite `max_buffered_put_bytes`.

## Utilizzo

In Rust, usare `plenora_storage_engine::build_engine_with_upload_strategy`
con gli stessi `EngineConfig` e resolver di `build_engine`, passando
`UploadStrategy::PrivateFile` come terzo argomento. Il motore risultante usa
le operazioni esistenti. `UploadStrategy::Buffered` mantiene il comportamento
predefinito; S3, SFTP, FTP e FTPS conservano i propri percorsi e limiti.

Nella CLI aggiungere `--spool-uploads` e impostare il limite totale ammesso,
espresso in byte. Per esempio, con una connessione local già configurata:

```sh
plenora-storage --spool-uploads --max-transfer-bytes 2147483648 put \
  --connection local.json --key backup.bin --input backup.bin \
  --overwrite false --publication-policy atomic-required
```

In Python il parametro è keyword-only e richiede un booleano:

```python
from plenora_storage import Engine, EngineConfig

with Engine(EngineConfig(max_transfer_bytes=2 * 1024**3), spool_uploads=True) as engine:
    engine.put(connection, "backup.bin", "backup.bin", overwrite=False,
               publication_policy="atomic_required")
```

`connection` è una `Connection` già costruita; per i provider remoti passare
anche il proprio `credential_resolver`. `AsyncEngine` accetta lo stesso
parametro e le sue operazioni si attendono con `await`.

## Limiti e spazio necessario

Il motore legge tutta la sorgente in un file temporaneo privato, calcola SHA-256
e verifica lunghezza e limite prima di pubblicare. La preparazione usa blocchi
da 64 KiB. `max_transfer_bytes` limita ciascuna operazione; non è un budget
aggregato per tutte le operazioni concorrenti. Serve spazio temporaneo per
un'intera sorgente per ogni put o copy in preparazione, oltre agli input,
agli eventuali staging di destinazione e ai download.

Azure applica anche il limite di 5.000 MiB del singolo
[Put Blob](https://learn.microsoft.com/en-us/rest/api/storageservices/put-blob).
Il valore ammesso è il minore fra questo limite e quello del motore. Le
capability della strategia selezionata dichiarano `upload_preparation` e
`prepared_protocol_max_bytes`. La discovery descrive il codice, non una prova
di compatibilità con un account o server reale.

Il temporaneo non è un checkpoint riutilizzabile e non abilita resume. Viene
chiuso al termine dell'operazione; la directory temporanea del processo deve
disporre delle protezioni e dello spazio necessari. Il contenuto non viene
inserito nei messaggi pubblici.

## Errori e pubblicazione

Un errore durante la preparazione precede la modifica della destinazione.
Dopo l'inizio della pubblicazione, usare sempre `remote_effect` e `retry`
dell'errore per decidere come recuperare. Una risposta persa può lasciare un
esito sconosciuto; il motore non ripete automaticamente la mutazione.

Local conserva la pubblicazione tramite staging e rename/hard-link; Azure e
GCS mantengono le condizioni di creazione sul server. SMB e WebDAV richiedono
`best_effort`: la preparazione locale non rende atomica una scrittura remota.
La compatibilità WebDAV resta limitata alla fixture e configurazione
[documentate](webdav-compatibility.md).
