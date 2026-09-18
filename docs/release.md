# Qualifica e distribuzione

La versione 0.2.0 riguarda Rust e CLI con nove provider; la [matrice](provider-expansion.md) descrive i sei aggiunti. Gli
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
8. Riunire i due target sotto `dist/0.2.0/` e chiudere con
   `python scripts/qualify_release.py dist/0.2.0 --evidence <directory-log>`.
   Il gate richiede `audit.json`, `deny.log` e i log Rust dei due target
   denominati `<target>-tests.log`. Nei target devono essere presenti
   `qualification.json`, `cli-regressions.json`, `extended-qualification.json`,
   `extended-regressions.json` e, per Linux, `commit-faults.json`.
   Generare i due report extended con `qualify_extended.py` e
   `qualify_extended_faults.py` sul binario finale. I log comprendono anche
   `smb-upstream-audit.json`.

Il runner Docker deve eseguire i comandi relativi alla fiducia della CA come
root: la CA è effimera ed è installata solo nel container di test. Le chiavi in
`.fixtures` non sono artefatti di release. `COMPOSE_PROJECT_NAME` e `COMPOSE_FILE`
permettono di isolare una qualifica sulla VM. Python 3.11 o successivo è richiesto
dagli script. I test contrattuali che leggono fixture del workspace sono esclusi
dal pacchetto core; i contratti sono distribuiti in un archivio separato.

La fase di packaging usa `--no-verify` perché Cargo 1.92 su Windows può
fallire nel registro temporaneo dei crate interni non pubblicati con
`no hash listed`. La verifica successiva estrae i sette archivi in una
directory temporanea, compila ed esegue un consumer dei crate Rust,
poi compila la CLI estratta con patch locali per quei medesimi archivi.
La verifica non usa i sorgenti dei crate nel checkout.

`release-manifest.json` registra anche un digest dei sorgenti Rust, manifest
Cargo e contratti, per confrontare il contenuto qualificato tra piattaforme.
Non è una firma digitale né una promessa di build identiche bit per bit.

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
- MinIO/OpenSSH/Pure-FTPd sono le implementazioni testate, con immagini bloccate
  per digest nel compose. Non estendere il claim ad AWS o ad altri server senza
  eseguire la matrice del documento release-readiness.
- FTP è in chiaro e richiede autorizzazione esplicita. FTPS e autenticazione SSH
  tramite chiave privata non sono implementati; SFTP usa password e pin SHA-256.
- Il root remoto è un namespace applicativo, non una sandbox contro symlink o
  hardlink ostili. Il server deve applicare isolamento/chroot e permessi corretti.
- Le deadline sono cooperative. `unknown` richiede verifica dello stato remoto,
  non retry automatico. In particolare, perdita della risposta al commit e
  terminazione forzata del processo non consentono di provare il rollback.
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
