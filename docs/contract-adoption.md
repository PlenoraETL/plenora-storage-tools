# Adozione del profilo pubblico storage v1

Il riferimento immutabile è `plenora-contracts` alla revisione
`f811f21f072b34896efdb6e110bee34d756153df`, profilo
`plenora-storage-tools-profile-v1`. Gli schemi comuni copiati in
`contracts/upstream` mantengono gli identificatori originali.

La release 0.1.0 adotta le sette operazioni su Rust, CLI e Runtime Binding 1.0.
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
| Runtime Binding v1 | RT-001–RT-015: route, versioni, content type, UUID canonici, controlli, artifact, risultati ed errori completi | `core/tests/runtime_binding.rs`, eseguito anche dall'archivio Cargo estratto |

Non sono dichiarate deviazioni dai sei contratti nel perimetro descritto.
Python SDK, trasporto runtime del consumer, FTPS, autenticazione SSH a chiave,
compatibilità AWS e SLO prestazionali non sono superfici o garanzie adottate.
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

## Manifest ed evidenze immutabili

`scripts/build_release.py` crea `adoption-manifest-v4.json`, lo valida contro
lo schema bloccato e applica i controlli semantici upstream. I digest identificano
i cinque crate, il binario e il binding runtime contenuto nel core; non un
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
