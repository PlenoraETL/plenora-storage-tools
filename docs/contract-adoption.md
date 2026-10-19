# Adozione del profilo pubblico storage v1

Il riferimento immutabile è `plenora-contracts` alla revisione
`3aec9f724f5a0ad21366f293895b4d0dbcd0aad1`, profilo
`plenora-storage-tools-profile-v1`. Gli schemi comuni copiati in
`contracts/upstream` mantengono gli identificatori originali.

L'implementazione del profilo storage comprende le sette operazioni su Rust, CLI,
Python SDK 1.0 e Runtime Binding 2.0, compatibile con il wire 1.0 del profilo.
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
| Runtime Binding v2 | RT-001–RT-015: route, versioni, content type, UUID canonici, controlli, artifact, risultati ed errori completi; vettori runtime storage | `core/tests/runtime_binding.rs`, eseguito anche dall'archivio Cargo estratto; `core/tests/runtime_vectors.rs` |
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

Il binding adotta RT-016 a RT-023, ERR-014 ed ERR-015 come norme della
revisione fissata. N indica una regola normativa, D una conseguenza derivata.

| Caso | Comportamento | Stato |
| --- | --- | --- |
| Ordine dei rifiuti | `protocol` per qualunque chiave riservata malformata, poi `unsupported`, poi `timeout`; tutti prima dell'invocazione, con `validate`/`none`/`never` | N (RT-016, RT-018) |
| Capability, versione, operazione, versione d'operazione o input contract ben formati ma non annunciati, content type non annunciato | `unsupported`, codice `RUNTIME_ROUTE_UNSUPPORTED` | N (ERR-002, RT-004, RT-011) per operazioni e versioni; N per gli altri |
| Chiave riservata assente, non stringa o fuori grammatica (`01`, `+1`, `1` numerico, maiuscole, UUID tra graffe) | `protocol`, codice `RUNTIME_ROUTE_INVALID`, `RUNTIME_IDENTITY_INVALID` o, per l'envelope, `RUNTIME_ENVELOPE_INVALID`; mai normalizzata | D (rifiuto, RT-012); N (categoria, RT-017) |
| Metadati del risultato di rifiuto | operazione, versione d'operazione e correlazione copiate byte per byte solo se ben formate, altrimenti omesse (mai `"0"`, `storage.unknown` o l'UUID nil) | N (RT-019) |
| Identità del risultato | `message.id` nuovo (UUID versione 4 casuale; sorgente non disponibile: `RUNTIME_RESULT_IDENTITY_UNAVAILABLE` prima dell'invocazione, mai un panic); correlazione della richiesta | N (RT-012) |
| Causazione del risultato | `message.id` della richiesta, se canonico; mai la causazione della richiesta | N (RT-020) |
| Deadline | ogni grafia RFC 3339 di UTC (`Z`/`z`, `+00:00`, `T`/`t`, frazioni); offset diverso da zero e `-00:00` rifiutati con `protocol`; lo spazio al posto di `T`, fuori dalla grammatica RFC 3339, rifiutato | N (UTC); D (`-00:00`); N (grafie accettate, RT-021) |
| Deadline già scaduta (`deadline <= now`) | `timeout` (N), `validate` e `none` (D), `never` (N) | N, D |
| Deadline anche nel payload | i contratti di input storage non hanno una deadline: `invalid_configuration`/`validate`/`none`/`never` anche a valori uguali | N (RT-023) |
| Chiave di idempotenza | solo `plenora.execution.idempotency_key` (N); presente: `unsupported`, codice `RUNTIME_CONTROL_UNSUPPORTED` (N rifiuto, D categoria, RT-006); vuota o `null`: `protocol` (N, RT-022) | N, D |
| Chiavi `plenora.*` non riservate | ignorate | N (§9) |
| Get fallito dopo byte consegnati al sink | `partial`/`never`; se è il sink a fallire dopo un prefisso, codice `STORAGE_GET_SINK_PARTIAL` per ogni provider (`Engine::get`), con categoria e fase del guasto del sink | D (fixture `storage-get-partial-error`) |
| Get fallito con sink aperto e nessun byte consegnato | `unknown`/`requires_recovery` | N (ERR-004) per `unknown`; N (ERR-014) per `requires_recovery` |
| Pubblicazione provata, metadati non rileggibili (S3, SFTP, FTP) | `committed`/`cleanup`/`never`: nessun residuo, un nuovo tentativo ripubblicherebbe | N (ERR-015) |
| Pubblicazione FTP con dimensione diversa dai byte trasferiti | `committed`/`cleanup`/`requires_recovery`, codice `FTP_COMMITTED_SIZE_MISMATCH` | N (ERR-015) |

`core/tests/runtime_vectors.rs` esegue anche le 21 sonde di rifiuto e i
due vettori di cleanup ratificati, copiati in `contracts/upstream/ratified`
dalla revisione adottata e verificati per SHA-256. Le copie in `proposed`
restano storiche e non sono evidenza del pin corrente. Le sonde di altri
componenti applicano la stessa mutazione alla richiesta storage get.

Lo stesso `retry: never` per una deadline già scaduta in fase `validate` vale
per Rust, CLI e SDK Python, che condividono `StorageError::timeout` (RT-007).
La grammatica della deadline della CLI (`--deadline`) segue CLI 2.0 e non
cambia.

## Deviazioni dichiarate

Il manifest v4 incorpora `contracts/adoption-policy.json`: RT-024 e ERR-016
hanno le deviazioni STOR-LIMIT-001 e STOR-LIMIT-002, descritte nel
[registro dei limiti](contract-limits.md). Non si dichiara conformita senza queste riserve.

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

## Adozione 3aec9f7: stato dei requisiti

Il catalogo `storage-tools-v1` e il profilo `plenora-storage-tools-profile-v1`
restano quelli upstream: non esiste un catalogo storage v2. Public Catalogs
2.0 si applica al catalogo esistente senza riscriverne identita o byte.
`contracts/upstream/vendor-lock.json` registra origine e SHA-256 delle copie.
Il manifesto di artefatto resta generato al packaging; la policy sorgente
non e una ricevuta di qualifica.

| Regola / modifica | Stato | Ambito, evidenza e ragione |
| --- | --- | --- |
| CAT-001 | implementato | Discovery per superficie e provider compilato; test catalogo e CLI. Storage non seleziona destinazioni. |
| CAT-002 | implementato | Nessuna superficie rimossa; tutte e quattro implementate, nessun marker planned. |
| CAT-003 | non applicabile | Il profilo storage v1 non dichiara assenze intenzionali. |
| RT-001, RT-003 a RT-023 | implementato | Suite runtime_binding e runtime_vectors, sonde ratificate. RT-002 sostituito da RT-025. |
| RT-024 | deviazione | Limite opzionale del payload completo; errore resource_limit senza troncamento e con effetti preservati. STOR-LIMIT-001 delimita allocazioni e trasporto. |
| RT-025 | implementato | Discovery runtime v2/versione 2; wire, capability e operazioni versione 1. Test dedicato. |
| RT-026, RT-027 | non applicabile | Solo destinazione: nessun sender, router, coda o registro di accettazione. Il consumer deve negoziare e conservare il contesto fuori envelope. |
| ERR-014, ERR-015 | implementato | Effetto unknown e recovery; cleanup committed con retry distinto per residuo, vettori ratificati e test provider. |
| ERR-016 | deviazione | Il binding preserva io/timeout/internal e gli assi; fallback dei provider S3/SFTP/FTP possono ancora restituire execution. STOR-LIMIT-002 vale su tutte le superfici. |
| SB-001 | implementato | Nessun effetto aggiuntivo piu forte del catalogo: get/put/copy/delete sono gia remote; test/list/stat non materializzano file. Chiave opzionale omessa. |
| INTEROP-001 a INTEROP-005 (#34, decisione 0018) | non applicabile | Storage non adotta Arrow Geometry Semantics e non produce tabelle Arrow; gli artifact sono flussi opachi. |
| Decisione 0017, adapter REST/Arrow | non applicabile | Nessun adapter REST/Arrow o campo geometrico interpretato da Storage. |
| #39 / decisione 0021 | implementato nel gate di adozione | Diagnostica fissa, nessuna istanza o eccezione interpolata; helper upstream copiato con dipendenza json_values. Suite sentinelle upstream distinta dalla qualifica del prodotto. |
| #40, cache fuzz e confronti JSON esatti | non applicabile al runtime | Non si vendoriza il fuzz harness contracts; eseguito dalla copia locale. json_values upstream e adottato per il gate semantico. Il fuzz parser Rust resta distinto. |

Gate offline: `python scripts/check_contract_adoption.py`, con confronto byte
opzionale `--upstream C:/pg/contracts-main`; collegato alla CI Linux e Windows.
Il gate verifica policy/schema/semantica e hash, non inventa digest di release.
Le prove operative con fixture, wheel installata e account reali restano separate.

La revisione non contiene una decisione 0016: il registro passa da 0015 a 0017.

Il vincolo di un solo avvio per richiesta mutante senza chiave (issue #35)
e del profilo runtime-tools: Storage non e un trasporto, non effettua retry
automatici e non possiede il registro durevole delle consegne del consumer.

I due nuovi vettori `runtime-v2` riguardano `database.query`, non un'operazione
storage. La prova RT-024 storage usa invocazioni reali del binding con provider
scriptato, confine esatto in byte, payload sovradimensionato e mutazioni gia
completate; non attribuisce ai vettori database una copertura storage.
