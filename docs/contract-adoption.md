# Adozione del profilo pubblico storage v1

Il riferimento immutabile è `plenora-contracts` alla revisione
`1e902dfaab5819c1d9ce785878d5b26dbeae48b3`, profilo
`plenora-storage-tools-profile-v1`. Gli schemi comuni copiati in
`contracts/upstream` mantengono gli identificatori originali.

Il profilo storage adottato comprende le sette operazioni su Rust, CLI, Python SDK 1.0
e Runtime Binding 1.0.
Il binding runtime è incluso nel crate core: non richiede un servizio runtime
distribuito separatamente né un adapter di trasporto posseduto da questa libreria.
Il composition root del consumer mantiene autorizzazione, risoluzione di
segreti e artifact, trasporto e lifecycle.

| Contratto | Regole coperte | Evidenze eseguibili |
| --- | --- | --- |
| Public Surfaces v1 | Identità, sette operazioni, input/output versionati, effetti, controlli e corrispondenza dei binding | `core/tests/contracts.rs`; catalogo e descriptor runtime; `preflight.rs` |
| Capabilities v2 | Identità artefatto, catalogo per superficie, provider presenti, attributi tipizzati, stato available | Test capability core e `cli/tests/protocol.rs` con schema comune; discovery del binario estratto |
| Errors v1 | Categoria/fase/effetto/retry, assenza di retry sicuro per esito ambiguo, redazione | Schema comune sugli envelope CLI, suite runtime; fault injection multipart e SFTP |
| Public Security v1 | Identità remota, opt-in separati, riferimenti ai segreti, policy di rete, artifact opachi e preflight | Test provider HTTPS/pin corretto e errato, test network/credential/connection, suite runtime e regressioni CLI |
| CLI v2 | JSON unico, stderr vuoto, exit code, version/capabilities, input fail-closed, deadline/cancellazione | `cli/tests/protocol.rs`, `audit_release_readiness.py`, `qualify_cli.py`, `qualify_commit_faults.py` |
| Runtime Binding v1 | RT-001–RT-015: route, versioni, content type, UUID canonici, controlli, artifact, risultati ed errori completi; vettori runtime storage | `core/tests/runtime_binding.rs`, eseguito anche dall'archivio Cargo estratto; `core/tests/runtime_vectors.rs` |
| Python SDK v1 | Identità wheel, typing, sync/async, lifecycle, errori e discovery Python | `python/tests/test_sdk.py`, eseguito dalla wheel installata in modalità isolata fuori dal checkout |

Dalla revisione adottata il profilo storage richiede anche lo SDK Python
(decisione 0006 di `plenora-contracts`): `Engine` e `AsyncEngine` legano le
stesse sette operazioni, con `plenora_storage.version`, `Engine.capabilities` e
`AsyncEngine.capabilities` per versione e discovery. La mappa comune
`bindings/python-sdk-v1.json` è copiata byte per byte in
`contracts/upstream/python-sdk-v1.json`, fissata per SHA-256 da
`core/tests/runtime_vectors.rs`, e `python/tests/test_sdk.py` verifica dalla
wheel installata che ogni entrypoint e ogni simbolo di discovery della voce
storage esista, sincrono in `Engine` e coroutine in `AsyncEngine`. Il testo
normativo è copiato dalla medesima revisione in
`contracts/upstream/PYTHON-SDK-1.0.md`.
Trasporto runtime del consumer, compatibilità AWS e SLO prestazionali non sono
garanzie adottate. La qualifica operativa resta distinta dai test contrattuali.
FTP non offre pubblicazione atomica o create-if-absent: discovery e rifiuto
anticipato esplicitano queste limitazioni, come previsto dal profilo.

## Identità runtime

`plenora.message.id`, `plenora.trace.correlation_id` e l'eventuale
`plenora.message.causation_id` accettano solo UUID minuscoli con trattini.
Un'identità invalida produce `protocol`/`validate`/`none`/`never` prima di
qualunque resolver o provider. Il risultato, di successo o d'errore, è un
messaggio nuovo (RT-012): `plenora.message.id` è un UUID versione 4 tratto
dalla sorgente casuale del sistema operativo, mai derivato dalla richiesta, `plenora.message.causation_id` è
l'identità della richiesta se canonica, e la correlazione è quella della
richiesta. Un'identità non canonica non viene né riflessa né sostituita: la
chiave corrispondente del risultato è omessa. L'host è responsabile della
generazione di message ID unici.

Le chiavi opzionali `plenora.execution.deadline`, `plenora.execution.idempotency_key` e
`plenora.message.causation_id` si omettono quando assenti: un valore `null`
non è una stringa del trasporto ed è rifiutato dalla deserializzazione di
`RuntimeInvocation` e `RuntimeResultEnvelope`, invece di essere letto come
assente (una deadline `null` farebbe partire l'operazione senza scadenza).
Deserializzando `RuntimeInvocation` il rifiuto avviene nel DTO, e l'host lo
riceve come errore di deserializzazione. `RuntimeBinding::invoke_json` riceve
invece la richiesta serializzata così come il trasporto l'ha ricevuta e
restituisce sempre un envelope: una chiave riservata assente, un valore che non
è una stringa JSON o un controllo `null` producono
`protocol`/`validate`/`none`/`never` (RT-016, RT-017). Le chiavi di metadati
che il Binding 1.0 non riserva sono ignorate (§9): una grafia diversa di una
chiave riservata non è un suo alias.

## Vettori runtime

RUNTIME-VECTORS-1.0 chiede di esercitare ogni fixture delle operazioni
pubblicate. Le dodici fixture `storage-*` di `vectors/runtime-v1` (le richieste
delle sette operazioni, i successi di get, list e put, gli errori di get e put),
lo schema `runtime-vector-v1` e il testo normativo sono copiati byte per byte
dalla revisione adottata in `contracts/upstream/runtime-v1` e
`contracts/upstream`; `core/tests/runtime_vectors.rs` ne verifica lo SHA-256
fissato e la revisione di `source.json`. Il test valida ogni fixture contro lo
schema dei vettori e contro lo schema del componente, poi la esegue tramite
`RuntimeBinding` con un provider `s3` scriptato che restituisce gli esiti
descritti dalle fixture: get e put producono gli envelope di successo e,
quando il provider fallisce, quelli d'errore; test, stat, copy e delete, che non
hanno fixture di risultato, producono un risultato valido contro lo schema
d'uscita del componente con l'identità della richiesta. Per ogni richiesta, capability,
versione, operazione, versione d'operazione e input contract mancanti o
invalidi vengono rifiutati prima di segreti, artifact e provider.

Due differenze sono verificate. Il cursore della fixture list
appartiene a un altro engine: i cursori sono locali all'engine, quindi la
richiesta è rifiutata con `LIST_CURSOR_INVALID_OR_EXPIRED`, e la stessa richiesta
senza cursore produce il payload di successo con un cursore emesso da questo
engine. `execution_id`, facoltativo in `plenora-error-v1`, non è prodotto da
questo componente e non compare nell'envelope d'errore; `StorageError` lo
accetta in lettura (stringa di 1–128 caratteri o `null`, come nello schema) e
lo riserializza solo se presente, così le fixture d'errore si deserializzano
senza modifiche.

Un get che fallisce dopo che parte del trasferimento ha raggiunto il sink
dell'host riporta `partial`/`never`, come la fixture `storage-get-partial-error`:
il binding conta i byte accettati dal sink, e un get non modifica l'oggetto
remoto. Se il sink è stato aperto ma non ha ricevuto byte, l'apertura può averlo
creato o troncato: l'esito resta `unknown`/`requires_recovery`.

## Matrice runtime comune

Il binding segue la matrice canonica del Runtime Binding 1.0 condivisa con
Database, IO e REST Tools. Le celle **N** (normate) e **D** (derivate) seguono
il testo adottato di `plenora-contracts`; le celle **P** sono scelte comuni che
la pull request 21 di `plenora-contracts` (RT-016–RT-023, ERR-014, ERR-015)
propone di ratificare e che non sono ancora normative. Il codice le segnala.

| Caso | Comportamento | Stato |
| --- | --- | --- |
| Ordine dei rifiuti | `protocol` per qualunque chiave riservata malformata, poi `unsupported`, poi `timeout`; tutti prima dell'invocazione, con `validate`/`none`/`never` | P (RT-016, RT-018) |
| Capability, versione, operazione, versione d'operazione o input contract ben formati ma non annunciati, content type non annunciato | `unsupported`, codice `RUNTIME_ROUTE_UNSUPPORTED` | N (ERR-002, RT-004, RT-011) per operazioni e versioni; P per gli altri |
| Chiave riservata assente, non stringa o fuori grammatica (`01`, `+1`, `1` numerico, maiuscole, UUID tra graffe) | `protocol`, codice `RUNTIME_ROUTE_INVALID`, `RUNTIME_IDENTITY_INVALID` o, per l'envelope, `RUNTIME_ENVELOPE_INVALID`; mai normalizzata | D (rifiuto, RT-012); P (categoria, RT-017) |
| Metadati del risultato di rifiuto | operazione, versione d'operazione e correlazione copiate byte per byte solo se ben formate, altrimenti omesse (mai `"0"`, `storage.unknown` o l'UUID nil) | P (RT-019) |
| Identità del risultato | `message.id` nuovo (UUID versione 4 casuale; sorgente non disponibile: `RUNTIME_RESULT_IDENTITY_UNAVAILABLE` prima dell'invocazione, mai un panic); correlazione della richiesta | N (RT-012) |
| Causazione del risultato | `message.id` della richiesta, se canonico; mai la causazione della richiesta | P (RT-020) |
| Deadline | ogni grafia RFC 3339 di UTC (`Z`/`z`, `+00:00`, `T`/`t`, frazioni); offset diverso da zero e `-00:00` rifiutati con `protocol`; lo spazio al posto di `T`, fuori dalla grammatica RFC 3339, rifiutato | N (UTC); D (`-00:00`); P (grafie accettate, RT-021) |
| Deadline già scaduta (`deadline <= now`) | `timeout` (N), `validate` e `none` (D), `never` (P) | N, D, P |
| Deadline anche nel payload | i contratti di input storage non hanno una deadline: `invalid_configuration`/`validate`/`none`/`never` anche a valori uguali | P (RT-023) |
| Chiave di idempotenza | solo `plenora.execution.idempotency_key` (N); presente: `unsupported`, codice `RUNTIME_CONTROL_UNSUPPORTED` (N rifiuto, D categoria, RT-006); vuota o `null`: `protocol` (P, RT-022) | N, D, P |
| Chiavi `plenora.*` non riservate | ignorate | N (§9) |
| Get fallito dopo byte consegnati al sink | `partial`/`never`; se è il sink a fallire dopo un prefisso, codice `STORAGE_GET_SINK_PARTIAL` per ogni provider (`Engine::get`) | D (fixture `storage-get-partial-error`) |
| Get fallito con sink aperto e nessun byte consegnato | `unknown`/`requires_recovery` | N (ERR-004) per `unknown`; P (ERR-014) per `requires_recovery` |
| Pubblicazione provata, metadati non rileggibili (S3, SFTP, FTP) | `committed`/`cleanup`/`never`: nessun residuo, un nuovo tentativo ripubblicherebbe | P (ERR-015) |
| Pubblicazione FTP con dimensione diversa dai byte trasferiti | `committed`/`cleanup`/`requires_recovery`, codice `FTP_COMMITTED_SIZE_MISMATCH` | P (ERR-015) |

`core/tests/runtime_vectors.rs` esegue anche le 21 sonde di rifiuto
`vectors/runtime-probes-v1` e i due vettori d'errore di pulizia storage della
pull request 21. I file sono copiati byte per byte dal suo commit
`4890d27c120b3819bbadf6560ba9e730dfcb57aa` in `contracts/upstream/proposed`,
con SHA-256 fissati e la dicitura «proposed, not yet normative». Le tre sonde
storage si eseguono così come sono. Le altre si eseguono sulla richiesta
`storage-get-request.json` con la stessa mutazione, sostituendo nei metadati
attesi i valori della richiesta base con quelli storage.

Lo stesso `retry: never` per una deadline già scaduta in fase `validate` vale
per Rust, CLI e SDK Python, che condividono `StorageError::timeout` (RT-007).
La grammatica della deadline della CLI (`--deadline`) segue CLI 2.0 e non
cambia.

## Deviazioni dichiarate

Nessuna: il manifest v4 generato da `scripts/build_release.py` ha
`deviations: []`.

## Manifest ed evidenze immutabili

`scripts/build_release.py` crea `adoption-manifest-v4.json`, lo valida contro
lo schema bloccato e applica i controlli semantici upstream. I digest identificano
i crate Rust distribuiti, il binario e il binding runtime contenuto nel core; non un
branch o un percorso di sorgenti. Lo script compila solo gli archivi estratti
per la verifica del consumer ed esegue la suite runtime dal core distribuito.

Il manifest dichiara l'adozione contrattuale; la distinta
`release-qualification.json` attesta il superamento dei gate operativi per
Linux e Windows sullo stesso commit. `scripts/qualify_release.py` rifiuta
checkout modificati, digest discordanti, provider mancanti, test falliti o
saltati nel gate Linux e audit delle dipendenze con segnalazioni.

Le prove di guasto coprono commit S3 ritardato e risposta persa, sia con
deadline sia con SIGTERM, e commit SFTP interrotto prima/dopo pubblicazione.
Non costituiscono una dimostrazione contro ogni guasto del server o filesystem;
gli esiti non dimostrabili restano `unknown` e richiedono recovery.

Lo SDK Python è distribuito nella wheel; il manifest ne identifica il digest e
le modalità `sync` e `async`. Le ricevute delle release precedenti non cambiano:
queste nuove evidenze riguardano soltanto gli artefatti che le includono.
Vedere [allineamento](database-alignment.md).
