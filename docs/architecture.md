# Architettura

`plenora-storage-tools` separa il contratto pubblico dai protocolli concreti.
Il chiamante costruisce una connessione versionata, sceglie un'operazione e
passa eventuali byte come stream. L'Engine seleziona un provider registrato e
restituisce risultati o errori Plenora tipizzati.

```text
Rust / CLI / runtime
        |
        v
plenora-storage-core  ->  Provider registry
                              |
                              +-> S3-compatible -> MinIO / AWS / altro S3
                              +-> SFTP           -> server SSH/SFTP
                              +-> FTP            -> server FTP (opt-in)
                              +-> FTPS           (roadmap)
                              +-> SharePoint     (roadmap)
```

## Confine pubblico

La connessione esterna contiene:

- `provider`: identità stabile del provider;
- `config_contract`: contratto versionato della configurazione;
- `config`: dati non segreti validati dall'adapter;
- `credential_ref`: riferimento opaco risolto dall'host.

I segreti risolti non vengono serializzati né inclusi negli errori. I byte di
`get` e `put` attraversano stream; il JSON contiene soltanto metadati.

## Semantica comune

Il nucleo comune standardizza soltanto ciò che può essere osservato in modo
coerente: test della connessione, enumerazione, metadati, trasferimento,
copia ed eliminazione. Le capacità non universali sono dichiarate dal provider
e un'operazione non supportata fallisce chiuso.

S3 è object storage; SFTP/FTPS sono filesystem remoti; SharePoint espone
documenti e cartelle. Il core non promette directory, rename atomico, ETag
universali o versioning equivalente.

## Lifecycle ed effetti

L'Engine è persistente e riutilizzabile. `close` è idempotente e le operazioni
dopo la chiusura falliscono localmente. Deadline e cancellazione sono
cooperative. Per una mutazione interrotta dopo l'invio, l'effetto remoto è
conservativamente `unknown` e il retry richiede recovery.

## MinIO

MinIO non è un provider pubblico distinto. È un'implementazione S3-compatible
usata per testare lo stesso adapter selezionato con `provider: "s3"`.
