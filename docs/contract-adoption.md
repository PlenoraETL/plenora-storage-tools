# Adozione del profilo pubblico storage v1

Il riferimento immutabile è `plenora-contracts` alla revisione
`f811f21f072b34896efdb6e110bee34d756153df`, profilo
`plenora-storage-tools-profile-v1`. Gli schemi comuni copiati in
`contracts/upstream` mantengono gli identificatori originali.

Il profilo storage adottato comprende le sette operazioni su Rust, CLI e Runtime Binding 1.0.
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

Il contratto comune Python si applica alla wheel distribuita; è aggiunto al
manifest v4 senza modificare il profilo storage upstream. Il testo normativo è
copiato dalla medesima revisione in `contracts/upstream/PYTHON-SDK-1.0.md`.
Trasporto runtime del consumer, compatibilità AWS e SLO prestazionali non sono
garanzie adottate. La qualifica operativa resta distinta dai test contrattuali.
FTP non offre pubblicazione atomica o create-if-absent: discovery e rifiuto
anticipato esplicitano queste limitazioni, come previsto dal profilo.

## Identità runtime

`plenora.message.id`, `plenora.trace.correlation_id` e l'eventuale
`plenora.message.causation_id` accettano solo UUID minuscoli con trattini.
Un'identità invalida produce `protocol`/`validate`/`none` prima di qualunque
resolver o provider. La risposta di rifiuto sostituisce identità invalide
richieste con UUID nil e omette una causation invalida, evitando di riflettere
testo arbitrario. Identità valide e causation vengono preservate. L'host è
responsabile della generazione di message ID unici.

Le chiavi opzionali `plenora.execution.deadline`, `plenora.idempotency.key` e
`plenora.message.causation_id` si omettono quando assenti: un valore `null`
non è una stringa del trasporto ed è rifiutato dalla deserializzazione di
`RuntimeInvocation` e `RuntimeResultEnvelope`, invece di essere letto come
assente (una deadline `null` farebbe partire l'operazione senza scadenza).
Il rifiuto avviene nel DTO, prima del binding, che riceve un tipo già
deserializzato: l'host lo riceve come errore di deserializzazione, non come
envelope `protocol`/`validate`/`none`.

## Vettori runtime

RUNTIME-VECTORS-1.0 chiede di esercitare ogni fixture delle operazioni
pubblicate. Le sei fixture `storage-*` di `vectors/runtime-v1` (get, list e put),
lo schema `runtime-vector-v1` e il testo normativo sono copiati byte per byte
dalla revisione adottata in `contracts/upstream/runtime-v1` e
`contracts/upstream`; `core/tests/runtime_vectors.rs` ne verifica lo SHA-256
fissato e la revisione di `source.json`. Il test valida ogni fixture contro lo
schema dei vettori e contro lo schema del componente, poi la esegue tramite
`RuntimeBinding` con un provider `s3` scriptato che restituisce gli esiti
descritti dalle fixture: get produce l'envelope di successo, put l'envelope
d'errore `unknown`/`requires_recovery`. Per ogni richiesta, capability,
versione, operazione, versione d'operazione e input contract mancanti o
invalidi vengono rifiutati prima di segreti, artifact e provider.

Due differenze sono intenzionali e verificate. Il cursore della fixture list
appartiene a un altro engine: i cursori sono locali all'engine, quindi la
richiesta è rifiutata con `LIST_CURSOR_INVALID_OR_EXPIRED`, e la stessa richiesta
senza cursore produce il payload di successo con un cursore emesso da questo
engine. `execution_id`, facoltativo in `plenora-error-v1`, non è prodotto da
questo componente e non compare nell'envelope d'errore.

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
