# Piano verso Plenora Storage Tools 1.0.0

Stato: esecuzione autorizzata, non attestazione di release. Riferimento iniziale:
`main`, commit `78fc26f0d9c40de9642368af0f67f9cff1f0fa11`, versione sorgente 0.2.2.
Verifica iniziale: 19 settembre 2026. Distribuzione scelta: **solo GitHub Releases**.

## Obiettivo e perimetro

Pubblicare una 1.0.0 installabile e supportabile con API stabili Rust, CLI e SDK
Python, sette operazioni su tutti i nove provider: local, S3, SFTP, FTP, FTPS,
Azure Blob, GCS, SMB e WebDAV. Il Runtime Binding rimane incluso nel core.

Target iniziali: Windows x86_64 MSVC e Linux x86_64 con baseline Debian Bookworm.
Rust minimo 1.92; la matrice CPython va qualificata dalla versione minima 3.10
alle versioni effettivamente dichiarate. ABI3 non sostituisce i test di installazione.
macOS, ARM64 e altri ambienti richiedono una qualifica successiva.

Artefatti GitHub previsti:

- CLI Windows in ZIP e Linux in tar.gz, con licenze e istruzioni di installazione.
- Wheel Python dei due target, installabili tramite `pip install <wheel>`.
- Archivio sorgente Rust completo di workspace, crate interni, Cargo.lock,
  contratti e licenze; esempio consumer con dipendenza Git fissata al tag.
- Documentazione, changelog, matrice di compatibilità, SBOM, SHA-256,
  provenienza della build e ricevuta di qualifica collegata ai file distribuiti.

Non sono previsti upload a crates.io o PyPI. Gli archivi `.crate` oggi prodotti
rimangono utili alla verifica: da soli non risolvono l'installazione dei crate
interni non pubblicati. Il consumer da Git/tag e il bundle sorgente devono essere
testati senza patch manuali nascoste e senza dipendenze interne su registry.

## Punto di partenza verificato

- Nessun tag locale e nessuna GitHub Release al momento della verifica.
- Esistono build, test, SBOM e qualifica locale. La 0.2.2 ha superato 1.139 test
  Rust Windows e 1.149 Linux; CLI e SDK sono stati provati sui nove provider.
  Vedere il [resoconto delle dipendenze](dependency-update-0.2.2.md).
- Il workflow [release-candidate](../.github/workflows/release-candidate.yml)
  carica artefatti Actions, ma non pubblica una GitHub Release e non riunisce
  automaticamente tutta la qualifica richiesta per i due target.
- La CI di `78fc26f` risultava ancora in corso durante l'analisi. La precedente
  esecuzione di `b4cbc04` era fallita nei job Windows/wheel e Docker/conformance.
  I test locali non sostituiscono il risultato dei runner GitHub.
- Gli script `verify_release.py` e `qualify_release.py` convertono la versione
  con `int()` sui segmenti: non gestiscono `1.0.0-rc.1`. Anche confronto versioni,
  nomi wheel e report devono gestire la corrispondenza SemVer/PEP 440.
- Profilo Python, coverage quantitativa, benchmark con obiettivi misurabili e
  campagne fuzz sono ancora aperti nell'[allineamento a Database Tools](database-alignment.md).
- Azure/GCS sono provati su emulatori, S3 su MinIO. Le prove non qualificano
  automaticamente AWS, account cloud reali, Windows Server o Nextcloud.
- Local, Azure, GCS, SMB e WebDAV bufferizzano put/copy con limite predefinito
  di 64 MiB per operazione; SFTP offre attualmente autenticazione password.
- Il changelog non contiene ancora la 0.2.2; alcuni documenti riportano versioni
  precedenti delle fixture e criteri riferiti alla 0.2.1.

## Milestone e criteri di uscita

| ID | Priorità | Lavoro | Completata quando | Dipende da |
| --- | --- | --- | --- | --- |
| M0 | P0 | Rendere affidabili CI e preparazione release | CI verde sul commit scelto; build pulita dei due target; evidenze salvate; nessun passaggio necessario vive solo in script locali ignorati | — |
| M1 | P0 | Definire la superficie stabile e il supporto | Specifica Rust/CLI/Python, garanzie per provider, versioni supportate e compatibilità approvate e coperte da test | M0 |
| M2 | P0 | Chiudere i limiti operativi scelti per la 1.0 | Trasferimenti grandi, autenticazione e recovery superano i criteri sotto; nessuna mutazione dichiarata riuscita senza prova | M1 |
| M3 | P0 | Misurare affidabilità e compatibilità reale | Matrice dei nove provider completa, stress/fault/fuzz/coverage/benchmark riproducibili; nessun difetto bloccante aperto | M1, M2 |
| M4 | P0 | Pubblicare una release candidate GitHub | `v1.0.0-rc.1` installabile dai suoi asset; qualifica completa su quegli esatti file; release note con limiti | M0–M3 |
| M5 | P0 | Pubblicare e supportare la 1.0.0 | Nuova build finale qualificata, tag e release coerenti, installazione da GitHub verificata, procedura 1.0.1 pronta | M4 |

Le milestone descrivono esiti, non versioni intermedie obbligatorie. Percorso
proposto: sorgente 0.2.2 -> eventuali iterazioni di sviluppo -> 1.0.0-rc.N -> 1.0.0.

### M0 — Pipeline riproducibile

1. Diagnosticare i fallimenti Actions e verificare la CI del commit corrente.
   Installare esplicitamente interpreti/toolchain e dipendenze necessarie sui
   runner puliti; evitare di dipendere dalle cache presenti sulla workstation.
2. Riportare nel repository l'orchestrazione Windows/VM necessaria alla qualifica.
   La CI pubblica non può presumere accesso a `192.168.2.134`: usare fixture sul
   runner dove possibile e un job di qualifica con accesso alla VM per la matrice
   completa. Il job deve ricevere commit e digest, eseguire i file candidati e
   produrre report verificabili. I job con accessi riservati eseguono codice fidato.
3. Gestire versioni stabili e prerelease in tutti gli script, con test sui casi
   `0.2.2`, `1.0.0-rc.1`, `1.0.0-rc.2` e `1.0.0`. Per Python la rappresentazione
   di packaging è `1.0.0rc1`; mantenere esplicita la relazione con il componente
   Rust, senza confronti o glob che trattino le due stringhe come identiche.
4. Aggregare log, audit, test di feature e report dei due target; rendere il gate
   finale sensibile a report mancanti, incompleti, alterati o riferiti ad altri
   artefatti. Ogni nuovo controllo deve avere un job eseguibile e un test negativo.

### M1 — Contratti stabili

Inventariare i simboli Rust pubblici, i comandi e gli exit code CLI, le firme
Python, gli schemi JSON e le categorie di errore. Definire cosa una versione
1.x può aggiungere e cosa richiede una major, includendo MSRV e versioni Python.
Separare versione del prodotto, versione del protocollo CLI e contratti v1.

Chiudere la matrice di adozione Python con le prove richieste dal profilo
applicabile; se occorre un'estensione storage-Python in `plenora-contracts`,
tracciarla come dipendenza prima di dichiarare conformità. Testare esempi e tipi
pubblici dalla wheel installata, compresi resolver, close, cancellazione e asyncio.

Congelare autenticazione, limiti, atomicità, create-if-absent e trattamento dei
metadata per ciascun provider. Introdurre test di compatibilità verso il primo
baseline 1.0, guida di migrazione dalla 0.2.2 e politica di deprecazione.

### M2 — Comportamento operativo

Proposta di requisiti da fissare in M1 prima del congelamento API:

- Trasferimenti oltre 64 MiB e prova da almeno 1 GiB sui percorsi dichiarati
  idonei a file grandi, con checksum verificato. Usare streaming o staging su
  disco con quote e cleanup espliciti; la memoria non deve crescere con l'intero
  payload. Se un'operazione mantiene un limite, esporlo e rifiutarla senza effetti
  prima possibile. Documentare RAM, disco e concorrenza per provider.
- Autenticazione SFTP con chiave privata tramite resolver dell'host, eventuale
  passphrase e pin della host key; segreti assenti da configurazioni e log.
- Test di credenziali scadute/rifiutate per cloud e rinnovo tramite il consumer,
  mantenendo esplicita la responsabilità di ottenere e rinnovare i token.
- Interruzione rete, timeout, cancellazione, disco pieno, permission denied,
  risposta al commit persa, dati parziali e restart. Verificare contenuto e
  stato remoto, classificazione degli effetti e istruzioni di riconciliazione.
- Nessun retry automatico di mutazioni con esito ambiguo; staging e multipart
  residui gestiti con ownership verificabile e procedure documentate.

Le prove devono distinguere garanzie reali da operazioni non supportate: FTP,
FTPS, SMB e WebDAV non diventano atomici per il solo passaggio alla 1.0.

### M3 — Evidenze sufficienti

| Area | Prova richiesta |
| --- | --- |
| Provider | Sette operazioni, vuoti e file grandi, paginazione, conflitti, concorrenza, metadata e credenziali corrette/errate sui sistemi dichiarati |
| Cloud | AWS S3, Azure Blob e GCS reali per dichiararne supporto di produzione; account e namespace dedicati, budget fissato e cleanup limitato agli oggetti della prova |
| Altri server | MinIO, OpenSSH, Pure-FTPd, FTPS TLS, Samba cifrato e WsgiDAV come baseline; Windows Server/Nextcloud soltanto dopo prove dedicate |
| Coverage | Report separati per core, adapter, CLI, Python e fork SMB; baseline e soglie per modulo, con percorsi di sicurezza/commit inventariati; il volume di test upstream SMB non maschera lacune del prodotto |
| Fuzz | Target per configurazioni, nomi/path, risposte XML/FTP e cursor; seed, durata e risultati conservati; crash riproducibili trasformati in regressioni |
| Prestazioni | Throughput, latenza, RSS e uso disco con payload piccoli e grandi, concorrenza 1/4/16; baseline sulla VM e soglie deliberate prima del gate finale |
| Durata | Campagna iniziale proposta di 24 ore su fixture, con controlli di leak, handle, memoria e integrità; nessun errore inspiegato lasciato aperto |
| Dipendenze | Audit, licenze, SBOM, provenienza del fork SMB e stato della dipendenza transitiva prerelease `ssh-key` valutati sul lockfile finale |

Le soglie numeriche di coverage e throughput si fissano dopo la prima misura,
prima della RC; non si inventano risultati o SLO a partire dal conteggio dei test.
L'assenza di account cloud blocca il relativo claim di supporto reale: va risolta
prima della 1.0 proposta, oppure il perimetro va ristretto esplicitamente.

### M4 — GitHub Release candidate

Preparare un workflow che compili da un commit pulito e identificato, crei gli
asset e qualifichi quei file. Un job finale verifica i digest, crea il tag
`v1.0.0-rc.1` sullo stesso commit e pubblica una GitHub prerelease con gli asset
qualificati. Gestire i rerun senza sovrascrivere asset differenti dello stesso tag;
un fallimento lascia un candidato incompleto, non una release promossa.

Provare download e installazione reali dagli asset GitHub in ambienti puliti:
CLI `--version`/`capabilities` e roundtrip; wheel import/sync/asyncio; consumer
Rust da tag e archivio sorgente. Nessun affidamento sul checkout o sui crate
interni pubblicati. Verificare anche licenze, runtime Windows e baseline Linux.

Changelog e documentazione devono descrivere comportamento e limiti attuali,
con esempi eseguiti dai test. Raccogliere i difetti della RC per gravità:
perdita/corruzione dati, esposizione segreti, blocchi non controllabili e errori
di installazione su target supportati impediscono la promozione.

### M5 — Release finale e manutenzione

Correggere i difetti della RC; ogni cambiamento produce una nuova RC e le prove
interessate. Il passaggio a `1.0.0` modifica versione e artefatti: richiede nuova
build e qualifica dei file finali, non la rinomina delle wheel/binari RC.

Pubblicare tag `v1.0.0`, release note e asset con SHA-256 e attestazione della
provenienza prevista dalla pipeline. Eseguire la verifica post-pubblicazione
degli stessi asset scaricati. Definire prima del rilascio referente del supporto,
segnalazione vulnerabilità, aggiornamento dipendenze, priorità incidenti e
procedura patch 1.0.1. Il rollback cambia client/versione: non annulla mutazioni
già eseguite sullo storage.

## Criterio finale di completamento

La 1.0.0 è conclusa quando la GitHub Release è pubblica e installabile sui due
target, Rust/CLI/Python superano le matrici promesse, il commit del tag coincide
con quello qualificato e ogni asset è legato alle sue evidenze. I difetti
bloccanti sono chiusi; limiti e supporto sono documentati; il percorso per
produrre una patch è ripetibile da un checkout pulito.

## Ordine operativo e dipendenze esterne

Primo intervento: M0, cominciando dai log Actions e dalla gestione prerelease.
M1 determina i cambiamenti ammessi; M2 e M3 procedono per provider; soltanto
dopo si passa a RC e finale. Non serve pubblicare retroattivamente le versioni 0.x.

Servono accessi agli account cloud di test, disponibilità della VM, permessi di
pubblicazione GitHub e un referente per il supporto. Non servono account di
pubblicazione crates.io/PyPI. Stime e date si fissano dopo M0/M1 e la prima
misura sui trasferimenti: i rischi principali sono autenticazione, file grandi,
interoperabilità cloud e chiusura delle differenze tra runner locali e CI.

Nuovi protocolli, sync di directory, resume, copy tra provider, ADLS DFS/ACL,
SMB Kerberos/DFS e FTPS implicito restano nel backlog successivo, salvo necessità
emersa nei requisiti della 1.0. Database Tools guida il metodo di verifica;
la sua maturità non viene attribuita automaticamente a Storage.

## Riferimenti

- [Procedura di qualifica](release.md) e [adozione contratti](contract-adoption.md).
- [GitHub Releases del progetto](https://github.com/PlenoraETL/plenora-storage-tools/releases).
- [CI sul commit iniziale](https://github.com/PlenoraETL/plenora-storage-tools/actions/runs/35407535528).
- [CI precedente fallita](https://github.com/PlenoraETL/plenora-storage-tools/actions/runs/35401779141).
- [Versioning Python e release candidate](https://packaging.python.org/en/latest/discussions/versioning/).
