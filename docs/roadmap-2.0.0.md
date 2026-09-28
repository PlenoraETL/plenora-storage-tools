# Piano 2.0.0: allineamento alla qualità di Database Tools

Piano del 28 settembre 2026. Obiettivo approvato: completare i sei ambiti di
allineamento rimasti aperti dopo la 1.0.0, su libreria Rust, CLI e SDK Python,
per tutti i nove provider. Il primo ciclo `2.0.0-alpha.1` implementa separazione,
documentazione e nuovi controlli. Il commit `d6909bf044fa1f99c5e83d8a9007c4b339bada9b`
ha superato la [CI completa](https://github.com/PlenoraETL/plenora-storage-tools/actions/runs/36404688410),
inclusi API sui due target, fixture, fuzz, coverage e dieci ambienti Python.
Si congela ora il candidato stabile 2.0.0: build e qualifica dei suoi byte finali
restano da completare. L'esito alfa non sostituisce queste prove.
Le verifiche locali sono descritte in [Qualità 2.0](quality-2.0.md).
Non è fissata una data di rilascio.

## Baseline e perimetro

- Storage Tools: tag `v1.0.0`, commit
  `67668f47a071e35d64169f3790634692fc5fbd12`.
  La [release pubblicata](https://github.com/PlenoraETL/plenora-storage-tools/releases/tag/v1.0.0)
  e il [workflow di pubblicazione riuscito](https://github.com/PlenoraETL/plenora-storage-tools/actions/runs/36396706695)
  chiudono la distribuzione della 1.0.0. La ricevuta pubblica
  `release-qualification.json` identifica gli artefatti qualificati.
- Riferimento Database Tools: commit
  `850723d86be9d0cb5d8b0643da6cae971c7ec068`, serie 6.0.0.
  Il confronto riguarda codice, documentazione e workflow esaminati; non
  costituisce una nuova esecuzione della suite di Database Tools.
- Provider: local, S3, SFTP, FTP, FTPS, Azure Blob, GCS, SMB e WebDAV.
  Target: Linux e Windows x86_64; SDK CPython 3.10–3.14 sui due target.
- Distribuzione esclusivamente tramite GitHub Releases: sorgenti Rust,
  archivi CLI, wheel Python, checksum, inventari ed evidenze.
- Restano distinti test offline, fixture e sistemi reali. Le prove su account
  AWS/Azure/GCS rimangono rinviate e non bloccanti. Non si estende la
  compatibilità oltre le prove raccolte. Per WebDAV resta il profilo
  [WsgiDAV con serializzazione applicativa](webdav-compatibility.md).
- Soak provvisorio di **7200 secondi**, valido anche per alfa, beta e RC.
  Un'eventuale revisione prima del rilascio richiede una decisione esplicita;
  questo piano non introduce un requisito di 24 ore.

Non sono inclusi nuovi provider, nuove piattaforme, pubblicazione su registry,
sincronizzazione di directory, resume o copia tra provider. I limiti dei
trasferimenti documentati in [Affidabilità](reliability.md) restano dichiarati:
il riordino del codice non rende automaticamente streaming un percorso buffered.

## Contratti e versionamento

La versione obiettivo è 2.0.0. Si preservano, per quanto possibile, firme e
re-export Rust, contratti delle connessioni, comandi e codici di uscita CLI,
envelope JSON, risultati dizionario Python, typing, lifecycle e cancellazione
sync/async. La major non obbliga a introdurre incompatibilità.

Ogni incompatibilità necessaria deve avere una decisione documentata con
motivazione, alternativa valutata, esempio prima/dopo e guida di migrazione.
I controlli API devono distinguere queste modifiche dalle regressioni
accidentali: non basta rigenerare una baseline per rendere verde il gate.
La baseline 1.0 resta consultabile.

Il primo ciclo di implementazione ha aperto la serie `2.0.0-alpha.1`, mantenendo
coerenti versioni Rust, Python e
inventari generati. Tag, ricevute e artefatti 1.0.0 restano immutati.

## Ordine di lavoro

| Milestone | Ambiti | Dipendenze | Criterio di uscita | Stato |
| --- | --- | --- | --- | --- |
| M0 — Baseline | Inventario delle responsabilità, API e misure iniziali | Release 1.0.0 | Elenco dei moduli da intervenire, misura iniziale del prodotto e matrice dei contratti da preservare | Raccolta; inventario e architettura versionati |
| M1 — Struttura e API | A1 e A2 | M0 | Nove adapter revisionati, documentazione pubblica completa e regressioni controllate sulle tre superfici | Implementato; Clippy, rustdoc, feature isolate e API Rust Windows verificati localmente; qualifica candidata da completare |
| M2 — Manutenibilità | A3 e A4 | Baseline M0; soglie consolidate dopo M1 | Controlli di dimensione e commenti attivi in CI, con prove dei casi rifiutati | Implementato e testato localmente; workflow collegato |
| M3 — Dipendenze e inventari | A5 | M0; prima di congelare gli artefatti | Policy applicata, scansioni degli artefatti dei due target e verifica integrata nella qualifica | Implementato; scanner provato sui byte 1.0 come sviluppo, non qualifica 2.0 |
| M4 — Documentazione corrente | A6, aggiornata durante M1–M3 | Chiusura M1–M3 | Guide coerenti con codice, evidenze collegate e migrazione esplicita | Guide aggiornate; esiti finali da collegare |
| M5 — Qualifica e rilascio | Tutti gli ambiti | M1–M4 | Nuovi artefatti 2.0 qualificati, pubblicati e verificati dopo download | Da fare |

M1 procede per adapter, con modifiche revisionabili e prove del comportamento
prima di passare al successivo. Un difetto comune va cercato anche negli altri
provider e nelle altre superfici. M3 può avanzare indipendentemente dal
refactoring una volta censita la baseline.

## A1 — Separazione delle responsabilità

Rivedere connessione/configurazione, operazioni, staging e pubblicazione dei
file, gestione delle risposte e traduzione degli errori. Separare i moduli dove
queste responsabilità sono oggi intrecciate; introdurre helper comuni soltanto
per comportamenti realmente condivisi. Estendere la revisione al dispatch CLI
e al confine Rust/Python quando attraversano gli stessi percorsi.

Completamento: tutti i nove provider hanno una mappa delle responsabilità;
moduli e confini sono descritti in [Architettura](architecture.md). Test e
feature isolate continuano a funzionare. Le prove coprono almeno rifiuto
dell'overwrite, scritture parziali, flush, timeout/cancellazione e commit nei
provider pertinenti, preservando categoria, fase, effetto e retry. Nessun
payload, endpoint, segreto o eccezione dei callback compare negli errori pubblici.
Non basta ridurre il numero di righe spostando codice senza chiarirne i confini.

Gate: [CI](../.github/workflows/ci.yml),
[compatibilità API](../.github/workflows/api-compatibility.yml) e qualifica
degli artefatti tramite [release-candidate](../.github/workflows/release-candidate.yml).

## A2 — Documentazione Rust e disciplina dei lint

Completare rustdoc per configurazioni, campi pubblici, costruttori, metodi ed
errori: default, unità, vincoli, autenticazione/TLS, limiti, effetti delle
operazioni e condizioni di retry. Gli esempi devono usare API reali e rendere
espliciti i requisiti di ambiente.

Rivedere le eccezioni globali `missing_errors_doc`, `too_many_lines`,
`missing_const_for_fn` e `needless_pass_by_value`. Correggere i casi utili;
conservare solo eccezioni circoscritte e motivate. Non cambiare una firma
pubblica soltanto per soddisfare un suggerimento del lint.

Completamento: revisione di tutte le superfici pubbliche proprie, nessuna
eccezione globale priva di motivazione e documentazione dei percorsi di errore.
Clippy, rustdoc con warning trattati come errori e doctest pertinenti devono
essere eseguiti dalla [CI](../.github/workflows/ci.yml). I casi che richiedono
fixture vanno qualificati nel relativo ambiente, non dichiarati superati perché
ignorati offline.

## A3 — Misura e budget del codice di prodotto

Introdurre una misura riproducibile ispirata al controllo `code_size.py` di
Database Tools. Censire Rust, CLI e wrapper Python, specificando metriche e
denominatore; separare test, codice generato, fixture e codice di terze parti.
Il fork SMB deve avere una classificazione esplicita, non sparire dall'inventario.

Completamento: baseline versionata, budget per componenti e punti critici,
report leggibile e gate nel job `product-quality` della
[CI](../.github/workflows/ci.yml). Le soglie si scelgono dopo la misura M0 e
si consolidano dopo M1; non si aumentano automaticamente quando il gate fallisce.
Le prove devono dimostrare il rifiuto di uno sforamento e il corretto trattamento
di test e codice generato. La metrica affianca la revisione delle responsabilità.

## A4 — Qualità dei commenti

Estendere il controllo ai file posseduti dal progetto: Rust, Python e stub,
shell, PowerShell, YAML e TOML, oltre ad altre estensioni effettivamente presenti.
Mantenere i riferimenti verificabili al debito tecnico e rilevare commenti di
cronologia dello sviluppo che non spiegano il comportamento corrente.
Conservare motivazioni tecniche, invarianti, limiti e riferimenti di sicurezza.

Completamento: policy scritta, ambito di scansione dichiarato e casi positivi
e negativi che distinguono commenti reali da stringhe, esempi e fixture.
Il controllo esteso deve essere eseguito dal job `product-quality` della
[CI](../.github/workflows/ci.yml). La qualità della prosa richiede anche revisione
umana; una ricerca di parole chiave non la certifica.

## A5 — Policy delle dipendenze e SBOM degli artefatti

Definire una policy esplicita di pin e aggiornamento prendendo come riferimento
i pin esatti condivisi di Database Tools. Applicarla alle dipendenze dirette e
agli strumenti di build/qualifica, documentando le eccezioni necessarie per i
consumer Rust. Verificare versioni disponibili e advisory al momento
dell'aggiornamento, compatibilità dei tipi pubblici, feature e MSRV. Non copiare
versioni o MSRV del riferimento senza verificarne l'effetto sul prodotto.

Integrare gli inventari attuali con la scansione dei componenti nativi negli
archivi CLI e nelle wheel effettivamente prodotti, per entrambi i target.
Distinguere dipendenze dichiarate, risolte nei lockfile e rilevate nei binari;
registrare digest degli input, versione/configurazione dello scanner e limiti
di rilevamento. La scansione non dimostra l'inventario completo dell'ambiente
di build né la presenza di ogni dipendenza del lockfile in ogni eseguibile.

Completamento: aggiornamenti verificati con audit e consumer degli archivi;
report conservati nel bundle, associati agli artefatti precisi e controllati
da [release-candidate](../.github/workflows/release-candidate.yml) e
[release](../.github/workflows/release.yml). Il gate deve rifiutare report
obbligatori mancanti, riferiti a digest differenti o alterati. Uno scanner non
eseguito non produce un PASS. Conservare la riproducibilità degli inventari
dichiarativi e rendere espliciti gli eventuali dati variabili della scansione.

## A6 — Documentazione corrente ed evidenze

Allineare indice, architettura, guide Rust/CLI/Python, compatibilità, migrazione
e procedura di release al codice finale. Registrare per ciascun ambito stato,
commit e prove che ne motivano la chiusura. Conservare separatamente osservazioni
storiche e attività correnti, evitando checklist ancora aperte per la 1.0
presentate come stato attuale.

Completamento: inventari rigenerati dal codice, esempi verificati sulle
distribuzioni, limiti e sistemi non qualificati espliciti, istruzioni coerenti
con i workflow. Il controllo documentale resta collegato alla
[CI](../.github/workflows/ci.yml). Il rapporto finale deve indicare ogni differenza
residua rispetto al riferimento e la sua motivazione; niente percentuali di
maturità prive di una misura definita.

## Criteri finali di rilascio

La 2.0.0 è pronta quando A1–A6 risultano chiusi con evidenze e il candidato
finale supera la procedura aggiornata di [qualifica](release.md):

1. CI, feature matrix, lint, documentazione, nuovi controlli di manutenibilità,
   API, fuzz e coverage di prodotto superati sul codice del candidato.
2. Archivi Rust, CLI Linux/Windows e wheel finali verificati tramite consumer;
   tutte le dieci combinazioni Python includono typing ed esempi sync/async.
3. Qualifica dei nove provider, fault injection e trasferimenti nel perimetro
   dichiarato; soak di almeno 7200 secondi e confronto delle prestazioni nel
   laboratorio dedicato. Le prove Linux usano la VM dedicata, quelle Windows
   il relativo ambiente di qualifica. Gli skip non equivalgono a successi.
4. Inventari, scansioni native e report originali legati ai medesimi digest,
   aggregati in una nuova ricevuta. Le ricevute 1.0 non qualificano la 2.0.
5. Pubblicazione GitHub tramite il workflow, verifiche degli asset scaricati
   sui due target, checksum ed evidenze pubbliche. Una build riuscita o una
   bozza GitHub non chiudono la milestone.

Per ogni milestone completata si aggiorna questo piano con commit e link alle
prove. Le attività implementate ma non ancora qualificate restano distinte da
quelle completate. Il prossimo controllo è la CI del candidato e la verifica
dei provider sulla VM dedicata, prima di congelare i byte finali.
