# Analisi della preparazione alla release

Data: 18 settembre 2026. Baseline: `9b7188e7d83e65da30ba18724a6b000efae5bc70`.
Perimetro concordato: libreria Rust e CLI, provider S3-compatible, SFTP e FTP.
Esito: **non pronta per una release di produzione**. Questa analisi non modifica
il comportamento del prodotto e non promuove i contratti da experimental.

## Evidenze raccolte

| Controllo | Risultato |
| --- | --- |
| `cargo fmt --all -- --check` | Passa |
| `cargo clippy --workspace --all-targets --all-features --locked -- -D warnings` | Passa |
| `cargo test --workspace --all-targets --locked` su Windows, Rust 1.92.0 | 51 test riportati come passati; 7 escono immediatamente senza eseguire lo scenario |
| CI Docker Linux sul commit analizzato | Successo, run del 12 settembre 2026 |
| Packaging S3 offline | Fallisce: dipendenza interna senza requisito di versione |
| CLI, pagina successiva in un nuovo processo | Difetto riprodotto: `LIST_CURSOR_INVALID_OR_EXPIRED`, exit 2 |
| FTP, server fermo sulla verifica della directory padre | Difetto riprodotto: processo ancora attivo dopo 4 secondi con deadline a 2 secondi |
| Schemi comuni CLI/capability | Passano sui campioni version, capabilities, errore di parsing e due risposte di paginazione |
| Issue/PR aperte e release GitHub | Nessuna restituita dalle API al momento dell'analisi |
| Audit delle dipendenze | Non eseguito: `cargo-audit` e `cargo-deny` non installati; nessun gate equivalente in CI |

La [CI verificata](https://github.com/PlenoraETL/plenora-storage-tools/actions/runs/34723019431)
esegue il gate Docker con le tre variabili di integrazione abilitate. Localmente
Docker non è disponibile nel PATH e le tre variabili `PLENORA_*_TEST` non sono
impostate a `1`: i sette ritorni anticipati non sono evidenza di integrazione.
La CI verde non copre i difetti riprodotti dai nuovi probe.

Il confronto con gli schemi comuni ha usato il checkout locale di
`plenora-contracts`, revisione `f811f21f072b34896efdb6e110bee34d756153df`.
È una verifica campionaria, non una dichiarazione di conformità completa.

## Difetti da chiudere

P1 indica un blocco funzionale o di affidabilità per il perimetro concordato.
P2 indica un intervento di robustezza da risolvere o qualificare esplicitamente.

### STOR-REL-001 — P1: paginazione CLI inutilizzabile tra invocazioni

Evidenza dinamica e statica. `main.rs:233` costruisce un Engine per ogni processo
e `main.rs:270` lo chiude. `engine.rs` conserva i cursori esclusivamente nella
propria mappa in memoria. La CLI espone comunque `list --cursor`.

Con due oggetti e `--max-items 1`, la prima chiamata produce `truncated=true`
e un token; la seconda chiamata con quel token fallisce immediatamente.
Il comportamento interessa tutti i provider perché il cursore appartiene al core.

Chiusura: rendere disponibile un percorso CLI che completi la paginazione
conservando lo scope e i limiti. Se si mantiene `--cursor` tra processi, serve una
strategia di persistenza/sessione; un nuovo modello di token deve essere
allineato ai contratti, che oggi documentano cursori process-local.
Test richiesti: più processi o sessione pubblica realmente supportata, tre pagine,
scope errato, scadenza e limite delle risorse. Non basta il test sul medesimo Engine.

### STOR-REL-002 — P1: deadline e cancellazione non coprono un'attesa FTP

Evidenza dinamica e statica. In `plenora-storage-ftp/src/lib.rs:712`,
`ensure_parent_directories` attende `ftp_directory_exists` direttamente;
quest'ultima attende `MLST` senza `ExecutionControl::run`.
La funzione è chiamata sia da `put` sia da `copy`.

Il probe effettua login e poi non risponde a `MLST`: la CLI rimane bloccata oltre
la deadline. La stessa attesa non consulta il token di cancellazione.

Chiusura: coprire anche questi probe con i controlli di esecuzione e preservare
l'effetto di eventuali directory già create. Testare server silenzioso, deadline
e cancellazione separatamente, sia in put sia in copy.

### STOR-REL-003 — P1: SFTP atomic-required non sostituisce un file OpenSSH esistente

Evidenza statica nel codice applicativo e nella dipendenza bloccata:
`plenora-storage-sftp/src/lib.rs:529` e `:703` usano `SftpSession::rename`.
In `russh-sftp 2.4.0` questa funzione invia `SSH_FXP_RENAME`, non
`posix-rename@openssh.com`.

OpenSSH distingue esplicitamente le due primitive; il rename standard non
sostituisce un file già esistente. Si vedano il
[protocollo OpenSSH](https://github.com/openssh/openssh-portable/blob/master/PROTOCOL)
e l'[implementazione server](https://github.com/openssh/openssh-portable/blob/master/sftp-server.c).
Quindi `overwrite=true` con `atomic-required` fallisce proprio sul caso di
aggiornamento di un file. Il roundtrip corrente crea destinazioni nuove.
Questo caso non è stato rieseguito contro un server OpenSSH in questa sessione.

Chiusura: usare una primitiva di sostituzione atomica qualificata, verificarne
il supporto e rifiutare la richiesta prima delle mutazioni quando manca.
Testare put e copy su destinazioni esistenti, server senza estensione e perdita
della risposta al commit. Eliminare prima la destinazione non sarebbe atomico.

### STOR-REL-004 — P2: limite del file di connessione applicato dopo l'allocazione

Evidenza statica: `plenora-storage-cli/src/main.rs:421` legge tutto il file con
`fs::read`; il limite di 1 MiB viene controllato solo a `:431`.
Un file molto grande consuma memoria prima del rifiuto; un file speciale può
anche trattenere la lettura. Queste operazioni locali non ricevono il controllo
di esecuzione.

Chiusura: lettura limitata a budget + 1 byte, policy esplicita sui file speciali,
deadline/cancellazione dove applicabili. Test con file oltre soglia e input che
non termina; non è necessario creare un file enorme per verificare il limite.

### STOR-REL-005 — P2: le directory create non sono tracciate nell'esito complessivo

Evidenza statica: gli adapter FTP/SFTP creano le directory padre prima di aprire
la sorgente di una copy. Un successivo errore di lettura può essere riportato con
`remote_effect=none`, pur avendo già creato directory. In SFTP il rollback dello
staging può analogamente lasciare directory nuove (`discard_staged_object`).

Chiusura: esplicitare il perimetro degli effetti e conservare l'informazione sulle
mutazioni preparatorie. Non dichiarare rollback completo senza prova e non
rimuovere directory di altre operazioni. Test con parent assente e sorgente copy
inesistente, oltre a errore di trasferimento dopo mkdir.

### STOR-REL-006 — P2: upload FTP/SFTP troppo grande rifiutato dopo l'apertura distruttiva

Evidenza statica: i due adapter non confrontano `content_length` con
`max_transfer_bytes` prima di aprire la destinazione. Con `overwrite=true` e
pubblicazione best-effort, una richiesta già nota come fuori limite può quindi
troncare il file precedente prima di fallire durante la copia. S3 fa invece
questo controllo anticipato (`plenora-storage-s3/src/lib.rs:481`).

Chiusura: preflight coerente per i limiti noti; mantenere anche il limite durante
lo streaming per sorgenti di dimensione sconosciuta o dichiarata erroneamente.
Testare che una dimensione dichiarata eccessiva lasci intatta la destinazione.

### STOR-REL-007 — P2: preflight del binding runtime incompleto

Punto da conservare nel backlog del core, pur non essendo il trasporto runtime
il deliverable prioritario. `Engine::preflight` verifica il contratto comune,
l'identità del provider e le chiavi, ma non la configurazione specifica o le
policy del trasporto. `RuntimeBinding` può aprire/troncare un sink prima che
l'adapter rifiuti una configurazione localmente invalida.

Chiusura: validazione locale del provider prima di `open_sink`; test con un vero
adapter registrato, configurazione specifica invalida e sink strumentato.
Non serve contattare la rete per questa verifica.

## Lavoro necessario per la release

| ID | Gap attuale | Criterio di completamento |
| --- | --- | --- |
| STOR-REL-008 | Crate interni referenziati solo con `path`; mancano descrizioni e file di licenza | Versioni delle dipendenze dichiarate, archive completi, testi MIT/Apache inclusi, consumer Rust compilato da pacchetti fuori dal checkout |
| STOR-REL-009 | Nessuna pipeline release o artefatto di produzione verificato | Build release per le piattaforme dichiarate, archive CLI, checksum, smoke test degli stessi artefatti, installazione e procedura di rollback documentate |
| STOR-REL-010 | Nessun pin di adozione, manifest v4 o digest qualificato | Pin immutabile di plenora-contracts, manifest valido, versioni e SHA-256 degli artefatti testati, evidenze e deviazioni esplicite |
| STOR-REL-011 | Test provider che saltano silenziosamente e copertura prevalentemente roundtrip | Skip visibili; gate di integrazione che fallisce se fixture/configurazione manca; matrice negativa e di concorrenza obbligatoria |
| STOR-REL-012 | Nessun gate dipendenze/advisory/licenze | Scansione del lockfile e delle dipendenze distribuite, gestione documentata delle eccezioni, riesecuzione sul candidato release |
| STOR-REL-013 | Nessuna matrice pubblica di compatibilità né guida operativa | Versioni server e sistemi operativi verificati, limiti, configurazioni sicure, recovery degli esiti unknown, manutenzione degli staging e multipart |

Il packaging S3 fallisce con:

```text
cargo package -p plenora-storage-s3 --offline --locked --no-verify
dependency `plenora-storage-core` does not specify a version
```

Anche FTP, SFTP e CLI usano dipendenze interne solo `path`. Il package core
elenca test che leggono `../../contracts`, ma non include quella directory:
i test dal checkout non equivalgono alla verifica del pacchetto distribuito.
Il Dockerfile attuale è un runner di sviluppo/test, non un'immagine di esecuzione
per produzione. Un'immagine runtime va realizzata solo se scelta come formato
di distribuzione; non è necessaria per consegnare crate e binari CLI.

La proposta locale dichiara già chiuse le decisioni pubbliche v1, ma rinvia
esplicitamente manifest v4, digest e release qualificata al punto 30.
Il profilo comune include anche Runtime Binding 1.0: la priorità Rust/CLI non
autorizza un claim di conformità completa senza le evidenze o deviazioni per
quella superficie. Non occorre aggiungere qui un trasporto posseduto dal consumer.

## Matrice minima di qualifica

- Tutte le sette operazioni sui tre provider, invocate dalla libreria e dalla
  CLI; validazione completa degli envelope comuni e degli output storage.
- S3 con HTTPS e SFTP con fingerprint corretto/errato: il setup attuale usa HTTP
  e SSH non verificato. FTP rimane esplicitamente in chiaro con opt-in;
  FTPS non è implicitamente incluso nel perimetro richiesto.
- Sovrascrittura su oggetto esistente, due create-if-absent concorrenti,
  self-copy, destinazione locale esistente e collisioni dello staging.
- Timeout e cancellazione in connessione, preparazione, lettura, scrittura,
  commit e cleanup; assert degli assi dell'errore e delle risorse rimaste.
- S3 multipart: interruzione anche dentro `WriteMultipart::finish`, esito di
  commit ambiguo e upload residui. Il codice di cleanup esplicito copre il loop,
  ma il writer viene consumato da finish: occorre verificare anche questa fase.
- File vuoto, trasferimenti oltre la dimensione dei buffer/parti, limiti
  dichiarati e reali, mismatch dimensione/integrità, memoria sotto concorrenza.
- Listing con più pagine, directory ampie, nomi non rappresentabili e mutazioni
  concorrenti. Per FTP/SFTP qualificare symlink e alias: la normalizzazione
  lessicale delle chiavi non rende da sola il root una sandbox remota.
- Test del binario release sulle piattaforme supportate; gestione di SIGINT
  e della terminazione del processo nell'ambiente operativo dichiarato.

Le prove prestazionali devono produrre misure di memoria, throughput e latenza
con dimensioni/concorrenza dichiarate; in questo audit non sono stati misurati
né stabiliti SLO. AWS S3 e altri server non vanno dichiarati compatibili sulla
sola base del roundtrip MinIO/OpenSSH/Pure-FTPd.

## Ordine di esecuzione e uscita

1. Correggere STOR-REL-001/002/003 con regressioni e definire la soluzione
   contrattuale per la paginazione CLI.
2. Chiudere i punti P2 applicabili e trasformare i casi di guasto in gate;
   eseguire la matrice di qualifica su tutti e tre i provider.
3. Preparare pacchetti e CLI release, documentazione operativa e controlli delle
   dipendenze; verificare un consumer esterno al repository.
4. Qualificare gli artefatti definitivi per digest, completare il manifest v4
   e allineare capability/opt-in/versione alla release effettivamente garantita.

Il candidato è pronto quando i P1 sono chiusi, i P2 risolti o accompagnati da
limiti/deviations verificabili, le prove obbligatorie sono realmente eseguite,
e le evidenze identificano gli stessi artefatti che verranno distribuiti.
Cambiare soltanto `experimental` o il numero di versione non soddisfa il gate.

## Riproduzione dei probe locali

```text
cargo build --locked -p plenora-storage-cli
python scripts/audit_release_readiness.py
```

Lo script usa solo la libreria standard Python e server effimeri su loopback;
crea input sintetici in `target/release-readiness` e salva `results.json`.
Non usa credenziali reali né contatta storage esterni. Registra osservazioni,
non sostituisce una suite di regressione e non certifica una release.
