# Qualifica e distribuzione

La prima 1.0 segue il [perimetro approvato](compatibility-1.0.md): tutti i nove
provider sulle fixture dichiarate, con i cloud reali esplicitamente non
qualificati. Le prove su account cloud sono rinviate e non bloccano questa
release; il manifest e la ricevuta riportano tale limite.

WebDAV richiede la [fixture con serializzazione applicativa](webdav-compatibility.md).
Ogni target finale deve conservare `webdav-fixture.json`: trenta prove HTTP
con otto scrittori, una sola creazione riuscita per chiave e dati preservati.
Il gate finale verifica il report e ne registra il digest nella ricevuta.

Per la serie 1.0 la ricevuta richiede anche il [bundle completo delle evidenze](release-evidence-bundle.md):
API, parser fuzz, coverage, matrice Python con typing, trasferimenti, soak e
confronto delle prestazioni. Lo stesso documento descrive la pubblicazione da
bozza GitHub con prove degli asset scaricati su Linux e Windows.

Gli inventari delle dipendenze devono essere verificabili nello stesso checkout
per entrambi i target. `.gitattributes` impone LF anche ai file
`scripts/requirements-*.txt`, dei quali la SBOM di qualifica registra i digest
dei byte. Il test `test_sboms_match_across_git_checkout_line_endings`, eseguito
dal job CI `product-quality`, confronta gli inventari dopo due checkout Git
con conversione dei fine riga disattivata e attivata. Correggere gli attributi
non corregge gli inventari già prodotti: gli artefatti precedenti conservano
le proprie evidenze e devono superare separatamente il gate finale.

La release corrente comprende Rust, CLI e SDK Python; la [matrice](provider-expansion.md) descrive i sei aggiunti. Gli
artefatti preparati diventano distribuibili quando `release-qualification.json`
registra `qualified_for_publication` per i loro digest. Non pubblicare un
artefatto prodotto da un checkout modificato o privo delle evidenze richieste.

## Gate

1. `cargo fmt --all -- --check` e Clippy con tutti i target e feature.
2. Test locali; i test di integrazione devono risultare esplicitamente ignored
   quando le fixture non sono disponibili.
3. `bash scripts/prepare-fixtures.sh`, quindi
   `bash scripts/prepare-extended-fixtures.sh`, poi
   `docker compose -f docker-compose.yml -f compose.extended.yml run --rm --no-deps storage-rust`: tutti i test, senza skip,
   contro MinIO HTTP/HTTPS, OpenSSH con fingerprint e Pure-FTPd.
4. `python scripts/audit_release_readiness.py`: regressioni pubbliche CLI con
   server locali. `PLENORA_CLI_BIN` seleziona il binario release da verificare.
5. `cargo audit --deny warnings` sul lockfile definitivo; usare cargo-audit
   0.22.2 e registrare la revisione del database insieme all'esito.
   Eseguire anche `cargo deny --locked check` con cargo-deny 0.20.2 e
   `python scripts/audit_smb_upstream.py` per il nome originale della dipendenza SMB.
6. `cargo fetch --locked`, poi `python scripts/build_release.py`: compila offline la CLI release, verifica tutti i
   pacchetti Cargo, esegue un consumer Rust dagli archivi estratti fuori dal
   checkout e genera archive e SHA-256 in `dist/<version>/<target>`.
7. Ripetere le verifiche CLI sul binario finale, mantenendone il digest.
   `qualify_cli.py` copre i tre provider; `qualify_commit_faults.py` esercita
   timeout e SIGTERM durante il completamento multipart nella fixture Linux.
8. Riunire i due target sotto `dist/<version>/` e chiudere con
   `python scripts/qualify_release.py dist/<version> --evidence <directory-log>`.
   Il gate richiede `audit.json`, `deny.log` e i log Rust dei due target
   denominati `<target>-tests.log`. Nei target devono essere presenti
   `qualification.json`, `cli-regressions.json`, `extended-qualification.json`,
   `extended-regressions.json` e, per Linux, `commit-faults.json`.
   Generare i due report extended con `qualify_extended.py` e
   `qualify_extended_faults.py` sul binario finale. I log comprendono anche
   `smb-upstream-audit.json`.

Per la serie 1.0 la qualifica Linux richiede anche `local-faults.json`, legato
al digest del binario finale. `scripts/qualify_local_faults.py` esercita ENOSPC
su un tmpfs isolato da 4 MiB ed EACCES con processi senza privilegi: upload e
download devono preservare i file esistenti, eliminare gli staging e restituire
categoria, fase, effetto e retry corretti senza esporre percorsi o contenuti.
Il mount è configurato nel servizio Compose `storage-rust`; il gate viene
eseguito sia da `verify.sh` nella CI sia da `qualify_target.py` sui candidati.
Questa prova riguarda il filesystem locale Linux e non certifica quote o
permessi dei server remoti.

Il runner Docker deve eseguire i comandi relativi alla fiducia della CA come
root: la CA è effimera ed è installata solo nel container di test. Le chiavi in
`.fixtures` non sono artefatti di release. `COMPOSE_PROJECT_NAME` e `COMPOSE_FILE`
permettono di isolare una qualifica sulla VM. Python 3.11 o successivo è richiesto
dagli script. I test contrattuali che leggono fixture del workspace sono esclusi
dal pacchetto core; i contratti sono distribuiti in un archivio separato.

Nella serie 1.0 il pacchetto CLI Windows è ZIP; Linux usa tar.gz. Entrambi
includono licenze e documentazione. `verify_release.py` controlla anche che
l'eseguibile estratto abbia gli stessi byte del binario qualificato: il solo
checksum dell'archivio non dimostra questa corrispondenza.

Il bundle `plenora-storage-<version>-source.tar.gz` contiene l'intero workspace
committato, Cargo.lock, contratti e licenze. `scripts/package_source.py` lo
estrae fuori dal checkout e compila/esegue un consumer con dipendenze path da
core ed engine, senza patch del registry. Il report `source-consumer.json`
lega esito, commit, archivio e log tramite digest; la CI esegue la prova su
Linux e Windows. La build completa include questi file negli asset e nel gate
finale. `--allow-dirty` produce solo un candidato locale incompleto: non può
attestare un bundle sorgente committato né superare la qualifica 1.0.

La fase di packaging usa `--no-verify` perché Cargo 1.92 su Windows può
fallire nel registro temporaneo dei crate interni non pubblicati con
`no hash listed`. La verifica successiva estrae gli archivi Cargo in una
directory temporanea, compila ed esegue un consumer dei crate Rust,
poi compila la CLI estratta con patch locali per quei medesimi archivi.
La verifica non usa i sorgenti dei crate nel checkout.

`release-manifest.json` registra anche un digest dei sorgenti Rust, manifest
Cargo e contratti, per confrontare il contenuto qualificato tra piattaforme.
Non è una firma digitale né una promessa di build identiche bit per bit.

I gate rifiutano manifest la cui versione o piattaforma differiscono dalla
directory candidata: spostare gli asset alfa in `dist/1.0.0` non li promuove.
`verify_release.py` e `qualify_release.py` rifiutano inoltre Python con `-O` o
`PYTHONOPTIMIZE`, perché la modalità ottimizzata disabiliterebbe le asserzioni
su cui si basano alcuni controlli. Queste condizioni hanno regressioni negative
nel job `product-quality` della CI.

## Ambito e limiti operativi

- Rust richiede Tokio; toolchain minima dichiarata: 1.92. Non è dichiarata
  compatibilità con versioni Rust più vecchie.
- Linux x86_64 viene qualificato nel container Debian Bookworm. Windows x86_64
  supera build/test nativi e la matrice CLI contro le fixture della VM (S3 HTTP
  con opt-in, SFTP con pin, FTP). HTTPS positivo/negativo è esercitato nel gate
  Linux; non si installa la CA temporanea nello store Windows dell'utente.
  Altri target richiedono qualifica.
- Il binario Windows MSVC dipende da Universal CRT e dal runtime x64 che
  fornisce `VCRUNTIME140.dll`, rilevati nella tabella degli import del binario.
  Questi componenti non sono inclusi nell'archivio: il controllo `--version`
  sulla macchina di destinazione è parte del gate di installazione.
- MinIO/OpenSSH/Pure-FTPd sono le implementazioni testate. OpenSSH/Pure-FTPd usano
  immagini bloccate per digest nel compose. La fixture MinIO viene costruita dal
  sorgente ufficiale `07c3a429bfed433e49018cb0f78a52145d4bedeb`
  (`RELEASE.2025-09-07T16-13-09Z`), con SHA-256 dell'archivio e immagini base
  fissati in `docker/minio/Dockerfile`: le vecchie immagini Quay non sono più
  recuperabili da un runner pulito. La sua licenza AGPL-3.0 resta nell'immagine
  di test; MinIO non è incluso negli asset Storage Tools. Non estendere il claim ad AWS o ad altri server senza
  eseguire la matrice del documento release-readiness.
- FTP è in chiaro e richiede autorizzazione esplicita. FTPS esplicito verifica
  il certificato TLS; SFTP usa pin SHA-256 con password oppure chiave privata
  OpenSSH, anche cifrata. Il resolver fornisce `username` e una sola modalità:
  `password` oppure `private_key` con `passphrase` opzionale. La chiave contiene
  il testo del file, non il suo percorso; il limite è 64 KiB. Password e chiave
  mescolate vengono rifiutate prima della connessione. Le fixture esercitano
  chiavi Ed25519; altre famiglie richiedono prove dedicate.
- Il root remoto è un namespace applicativo, non una sandbox contro symlink o
  hardlink ostili. Il server deve applicare isolamento/chroot e permessi corretti.
- Le deadline sono cooperative. `unknown` richiede verifica dello stato remoto,
  non retry automatico. In particolare, perdita della risposta al commit e
  terminazione forzata del processo non consentono di provare il rollback.
- I client HTTP S3, Azure, GCS e WebDAV non ripetono automaticamente le richieste.
  Anche una risposta 500/503/429 a una mutazione lascia la riconciliazione al
  consumer; i test contano le richieste ricevute dal server. Questa policy vale
  anche per le letture: il consumer decide i retry ammessi dall'errore pubblico.
- Su S3 configurare una policy server per eliminare multipart incompleti:
  cancellazione durante `finish` o arresto del processo possono lasciare parti.
  Non cancellare un oggetto finale basandosi soltanto su un timeout del client.
- Le directory preparatorie possono rimanere dopo un fallimento; gli errori le
  segnalano in `details.preparation`. Non rimuoverle senza verificarne ownership
  e uso concorrente. Gli staging orfani richiedono la stessa cautela.
- Nessun SLO di throughput è dichiarato. I limiti di trasferimento e buffer sono
  per operazione; il consumer deve limitare anche la concorrenza complessiva.

## Installazione e rollback

Verificare `SHA256SUMS`, estrarre il binario in una directory versionata e
controllare `--format json --version` e `capabilities`. Puntare il launcher alla
nuova directory solo dopo il test di connessione con le credenziali operative
risolte dall'host. Conservare binario, manifest e configurazione precedenti.
Il rollback ripristina quel launcher e quella configurazione; non annulla le
mutazioni storage già eseguite, che vanno riconciliate separatamente.

Il manifest degli artefatti identifica file e digest; il manifest di adozione
v4 è un file distinto, validato e generato dalla stessa build. La
[matrice di adozione](contract-adoption.md) descrive le evidenze contrattuali.
La ricevuta `release-qualification.json` lega anche i gate operativi al commit
finale. Verificare prima `SHA256SUMS` della versione, poi quelli dei singoli
target: i manifest, incluso quello di adozione, sono nella catena dei checksum.
La pubblicazione è un passo separato dalla build e dalla qualifica.

## Gate aggiunti dal modello Database Tools

`scripts/check_features.py` verifica ogni selezione di provider, inclusa la build
senza provider. `scripts/check_docs.py` controlla lo stato generato e le versioni.
`python -m unittest discover -s scripts/tests` verifica l'inventario SBOM.
Il packaging richiede `maturin==1.15.0`, costruisce la wheel con `build_python.py`
e ne esegue i test dopo installazione in un ambiente isolato. Wheel e log hanno
digest nel manifest. La qualifica Python live è eseguita da `qualify_python.py`
sulla wheel finale per ciascun target. Non riutilizzare report di una wheel diversa.

`storage-sbom.cdx.json` comprende il grafo Cargo.lock e i digest degli artefatti;
la sua presenza non sostituisce `cargo audit` o l'audit del nome upstream SMB.
