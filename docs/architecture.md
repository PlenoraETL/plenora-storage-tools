# Architettura

`plenora-storage-tools` separa il contratto pubblico dai protocolli concreti.
Il chiamante costruisce una connessione versionata, sceglie un'operazione e
passa eventuali byte come stream. L'Engine seleziona un provider registrato e
restituisce risultati o errori Plenora tipizzati.

```text
Rust / CLI / Python
        |
plenora-storage-engine -> provider selezionati dalle feature Cargo
        |
plenora-storage-core <- Runtime Binding del consumer
```

Il catalogo aggiornato dei provider e dei contratti è generato in [STATO.md](STATO.md).
I trasferimenti su file di CLI e Python condividono staging e pubblicazione.


## Confine pubblico

La connessione esterna contiene:

- `provider`: identità stabile del provider;
- `config_contract`: contratto versionato della configurazione;
- `config`: mappa piatta di valori scalari non segreti, validata dall'adapter;
- `credential_ref`: riferimento opaco risolto dall'host.

I segreti risolti non vengono serializzati né inclusi negli errori. I byte di
`get` e `put` attraversano stream; il JSON contiene soltanto metadati.

Il Runtime Binding 1.0 transport-neutral risolve `credential_ref` e i
riferimenti `artifact://` tramite trait applicativi, valida
capability/selector/versione/contratto/content type, collega deadline e
cancellazione e invoca realmente l'Engine. Restituisce il risultato completo o
un `plenora-error-v1`, preservando identità contrattuale e correlation ID. Il
core non dipende da `plenora-runtime-tools` e non implementa l'handler del
trasporto: ownership, autorizzazione e lifecycle restano nel composition root
dell'applicazione.

## Semantica comune

Il nucleo comune standardizza soltanto ciò che può essere osservato in modo
coerente: test della connessione, enumerazione, metadati, trasferimento,
copia ed eliminazione. Le capacità non universali sono dichiarate dal provider
e un'operazione non supportata fallisce chiuso.

S3, Azure e GCS sono object storage; gli altri adapter espongono filesystem
locali/remoti o risorse WebDAV. Il core non promette directory, rename atomico,
ETag universali o versioning equivalente.

I cursori di `storage.list` sono token opachi di massimo 512 byte, mantenuti in
memoria per 15 minuti e limitati a 1024 token attivi per Engine. Sono vincolati
a provider, fingerprint della connessione, prefix e `max_items`; un cambio di
scope fallisce chiuso. Scadono al timeout, all'eviction o alla chiusura/riavvio
dell'Engine e non promettono snapshot isolation durante mutazioni concorrenti.

La CLI esaurisce i cursori nello stesso processo tramite `list --all`, con
un budget totale sui risultati. Non emette cursori inutilizzabili dopo l'uscita.

I provider filesystem enumerano le directory e potano i rami estranei al
prefisso. I limiti di scansione e dei buffer sono specifici del provider;
`max_list_items` non rappresenta un limite uniforme sulle entry visitate per tutti
gli adapter. La pagina resta limitata a `max_items`. Un nome remoto non
rappresentabile come chiave pubblica fa fallire la list.

Le chiavi e i prefissi hanno la stessa semantica su tutti i provider: percorsi
relativi normalizzati, senza segmenti vuoti, `.` o `..`. Il prefix può essere
vuoto, e significa l'intero namespace. Un prefix seleziona segmenti interi: con
`prefix: "incoming"` la chiave `incoming/a.bin` corrisponde, `incomingother/a.bin`
no. È l'unica semantica che tutti i provider possono garantire, perché il livello
object store elenca per segmento e non per confronto letterale di stringhe.

L'adapter S3 valida le chiavi nella risposta XML prima della normalizzazione
del livello object store. Una chiave non rappresentabile, anche isolata nella
pagina, fa fallire la list: per esempio `folder/` non viene mai esposto come
`folder`. Ogni risposta XML di listing ha un limite di 32 MiB. Resta inoltre
il controllo sull'ordinamento strettamente crescente delle chiavi.

`etag`, version ID del provider e SHA-256 sono metadati distinti e opzionali.
La libreria non sintetizza i primi due e non li tratta come digest. Il campo
SHA-256 viene valorizzato soltanto quando i byte sono realmente attraversati e
calcolati dalla superficie.

## Lifecycle ed effetti

L'Engine è persistente e riutilizzabile. `close` è idempotente e le operazioni
dopo la chiusura falliscono localmente. Deadline e cancellazione sono
cooperative e coprono anche la fase di connessione e la risoluzione degli
artifact runtime. Per una mutazione interrotta dopo l'invio, l'effetto remoto è
conservativamente `unknown` e il retry richiede recovery.

L'effetto remoto dichiarato segue la fase realmente raggiunta. Una pulizia
confermata riporta `rolled_back`; una pulizia non confermata mantiene la causa
originale, aggiunge `details.cleanup` e riporta `unknown`; un errore successivo
al commit riporta `committed`, mai `none`. Dopo un rollback verificato solo una
causa transitoria resta ritentabile: una configurazione invalida o un limite
superato fallirebbero di nuovo in modo deterministico. Le pulizie hanno un
budget di tempo proprio, perché la deadline del chiamante può essere già scaduta.

Se la preparazione può avere creato directory, un rollback del solo file non
è un rollback completo: l'errore conserva `unknown`/`requires_recovery` e
`details.preparation=directories_may_remain`. La pubblicazione atomica SFTP
di sostituzione negozia `posix-rename@openssh.com` v1 prima di mutare lo storage;
un server privo dell'estensione viene rifiutato. La creazione esclusiva usa
invece il flag SFTP `EXCL`, senza probe seguito da scrittura.

Una pulizia rimuove soltanto ciò di cui l'operazione può dimostrare la
proprietà. Se una creazione esclusiva fallisce, il percorso non viene toccato:
potrebbe appartenere a un'altra operazione, e cancellarlo sarebbe una scelta
distruttiva basata su una supposizione. L'esito ambiguo viene riportato invece di
essere risolto arbitrariamente.

Il runtime esegue le verifiche locali — connessione, provider, contratto, opt-in
e chiave — prima di aprire un artifact sink, e confronta i metadati dichiarati
prima di finalizzarlo, così un input invalido non può produrre un effetto
esterno. L'apertura del sink è classificata come mutazione: se viene cancellata
dopo che il resolver ha già agito, l'esito è `unknown`, non `none`.
Anche un errore del provider prima della scrittura, come un oggetto inesistente,
mantiene l'effetto `unknown` e richiede recovery se il sink è già stato aperto:
il resolver potrebbe aver creato o troncato la destinazione.

## MinIO

MinIO non è un provider pubblico distinto. È un'implementazione S3-compatible
usata per testare lo stesso adapter selezionato con `provider: "s3"`.
