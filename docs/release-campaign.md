# Campagna di qualifica riprendibile

Dalla 2.0.1 il coordinamento della VM è versionato, insieme ai gate, e non
richiede script con commit o identificativi Actions incorporati. La durata
del soak resta 7200 secondi. Account cloud reali rimangono fuori dal perimetro.

## Prerequisiti

Il coordinatore viene eseguito su Windows, dove prova la distribuzione Windows;
la VM Linux dedicata esegue fixture, trasferimenti, prestazioni e soak.
Servono Git, GitHub CLI autenticato, Python 3.11 e:

```powershell
python -m pip install -r scripts/requirements-campaign.txt
```

La VM deve avere Docker Compose, Git, OpenSSL, un'immagine runner costruita dal
Dockerfile del progetto e una cache Cargo popolata. I gate ricevono i binari
canonici di Actions: il runner non li ricompila. L'accesso SSH verifica le
host key già approvate; la password viene richiesta interattivamente e non
viene salvata. In alternativa specificare una chiave con `ssh_key`.

## Configurazione e avvio

Salvare la configurazione sotto `target/`, escluso da Git. Esempio di struttura:

```json
{
  "repository": "PlenoraETL/plenora-storage-tools",
  "host": "storage-vm.example.invalid",
  "user": "storage",
  "vm_root": "/home/storage/qualification",
  "runner_image": "plenora-storage-runner:qualified",
  "registry_volume": "storage-rust-cargo-registry",
  "candidate_run": "123456789",
  "ci_run": "123456790",
  "baseline_binary": "dist/2.0.0/x86_64-unknown-linux-gnu/plenora-storage"
}
```

I valori identificano risorse reali dell'operatore; gli ID di esempio non sono
evidenze. Il checkout deve essere pulito e corrispondere ai due workflow riusciti.
La CLI di baseline deve provenire da una precedente distribuzione verificata.
Il suo digest entra nell'identità della campagna.

```powershell
python scripts/release_campaign.py --config target/campaign.json --output target/campaign-2.0.1
```

Il comando scarica e verifica i materiali Actions, crea un checkout dedicato
sulla VM, prepara le fixture, verifica Windows e avvia il runner Linux.
La directory VM include versione e commit; non riutilizza altri progetti Compose.
Le porte delle fixture devono essere disponibili. Al termine produce un bundle
`qualification-input.tar.gz` solo dopo la validazione completa delle evidenze.

Il certificato FTPS contiene l'host della configurazione come SAN, senza IP
del laboratorio incorporati nello script. Per preparazioni manuali impostare
`PLENORA_FIXTURE_HOST`; il default aggiunge `127.0.0.1` al nome Docker `ftps`.

`scripts/run_vm_campaign.py` è il runner Linux interno. Usa gli artefatti
immutabili come input e scrive i report in tentativi distinti. Qualifica della
CLI, installazione SDK, misura prestazionale, confronto, trasferimenti e soak
hanno fasi separate. Le prove Windows e Linux coprono gli stessi nove provider
nel perimetro delle fixture.

### Prestazioni: misura alternata

La fase `performance-ab` misura baseline e candidato nella stessa esecuzione,
32 round con 4 worker. Per ogni round e provider girano entrambi i binari, uno
dopo l'altro; chi parte per primo segue lo schema ABBA sull'indice di round di
quel provider (baseline, candidato, candidato, baseline, poi di nuovo). Ogni
provider, e quindi ogni sua operazione, parte per primo lo stesso numero di
volte con ciascun binario; per questo i round devono essere un multiplo di
quattro, altrimenti la misura viene rifiutata. Lo scopo è ridurre l'effetto di
una deriva lenta dell'ambiente, come un fixture che rallenta, che con due fasi
consecutive ricadrebbe tutta sul secondo binario.

Limiti dichiarati:

- i due binari non girano nello stesso istante. Per una deriva lineare la
  differenza residua tra i ruoli, in mediana e p95, è al più la deriva
  accumulata in una singola esecuzione (un provider, quattro worker). Con una
  deriva del 50% sull'intera misura, il caso di riferimento dei test, il
  residuo è circa 0,07% sulla mediana e 0,06% sul p95; una deriva così grande
  viene comunque respinta dalla guardia di stabilità;
- l'alternanza non protegge da un salto dell'ambiente tra le due esecuzioni di
  una stessa coppia, né da cambiamenti non lineari: per questi c'è la guardia.

**Guardia di stabilità.** Per ogni provider e operazione il confronto calcola,
separatamente per baseline e candidato, la mediana del tempo nella prima e
nella seconda metà dei round. Se uno dei due binari cambia rispetto a sé stesso
oltre metà del budget della mediana (5%, con soglia minima di 5 ms), la misura
è **non affidabile**: lo stato è `UNRELIABLE`, mai `PASS`, anche se il
candidato ha anche regredito; `check_performance.py` esce con il codice 3,
distinto dal codice 1 della regressione, e chiede di ripetere la campagna su un
host stabile. Il report registra ogni controllo in `stability`.

L'ordine dipende solo dalla posizione: è deterministico e non usa semi casuali.
Entrambi i report lo registrano in `paired_measurement`, con ruolo, schema,
ordine completo e `campaign_id` dell'altro report; lo schema entra
nell'identità della campagna. Il confronto `performance-compare`, il budget di
`scripts/performance-policy.json` e i criteri di regressione restano quelli di
prima. Il confronto valida i metadati accoppiati, tipi compresi: un campo
presente ma vuoto, nullo, incompleto, con tipi errati o con un ordine diverso
dallo schema ABBA dei suoi round e provider viene rifiutato, mai letto come
report storico. Dalla 3.0.0 il bundle di evidenze richiede una misura
accoppiata.

Il fixture GCS usa il backend in memoria di fake-gcs: con il backend su file
le directory dei prefissi degli oggetti cancellati restano, il listing le
percorre e la sua latenza cresceva a ogni round (da 1 ms a 68 ms dopo 300 cicli
di creazione e cancellazione), una deriva della fixture che nella campagna
3.0.0 ricadeva su `gcs test`.

Fino alla 3.0.0 baseline e candidato erano due fasi consecutive: nella prima
campagna della 3.0.0 un rallentamento dell'host durante la misura, e poi un
fixture GCS che rallentava nel tempo, sono ricaduti sul candidato e hanno
prodotto confronti rossi che un A/B alternato smentiva.

### Fixture ricreate prima del runner

All'inizio di ogni tentativo `qualify-vm` il coordinatore riesegue la
preparazione delle fixture con `PLENORA_FIXTURE_RECREATE=1`. Ogni preparazione,
anche quella di `prepare-vm`, ha un nonce unico, registrato nel tentativo
locale (`<etichetta>-nonce.json`) e usato nei nomi dei file di segnale sulla
VM: un segnale lasciato da un'esecuzione precedente non viene mai letto come
quello corrente. Il wrapper registra sempre il codice d'uscita reale ed esce
con quel codice. In ordine:

1. lo script gira sotto il lock del runner VM (`.fixtures/campaign/campaign.lock`):
   se un runner o un'altra preparazione lo tiene, si ferma prima di toccare
   qualcosa (codice 75) e la campagna termina con un errore esplicito;
2. se un container runner di questa campagna è ancora in esecuzione, si ferma
   con un errore esplicito (codice 76). Non viene terminato nessun processo;
3. archivia stato dei container, log delle fixture, certificati e fingerprint
   SFTP, che il tentativo conserva in `pre-reset.tar.gz`. Se la raccolta
   fallisce il reset non avviene. Il tentativo precedente resta immutabile,
   perché le sue evidenze sono già inventariate;
4. ricrea tutti i container delle fixture: i server in memoria (Azurite, fake
   GCS, WebDAV, SMB, FTPS) ripartono vuoti, gli altri ripartono sui loro volumi
   di dati, che la qualifica ripulisce man mano. Certificati, CA e fingerprint
   vengono rigenerati e il runner li legge al suo avvio;
5. verifica con `scripts/check_fixtures.py` che ogni fixture sia in esecuzione
   e sana, e che completi uno scambio applicativo con le identità di test:
   HEAD firmato SigV4 del bucket MinIO, anche in HTTPS verificato con la CA
   (dall'interno della rete delle fixture); login con chiave e listing SFTP con
   la host key confrontata con il pin; login e listing FTP; TLS esplicito
   verificato con la CA, login e listing FTPS; listing firmato SharedKey del
   container Azurite; metadati del bucket GCS; PROPFIND autenticato WebDAV con
   207; sessione SMB3 cifrata, tree connect e listing della share con
   smbclient. Un banner o uno stato d'errore non bastano. Gli healthcheck di
   FTPS, WebDAV e SMB fanno lo stesso scambio;
6. registra il proprio nonce in `.fixtures/campaign/fixture-state.json`.

Solo se tutte le fixture rispondono il tentativo registra `fixture-reset.json`
con `recreated: true` e il nonce, insieme a `fixture-check.json` e
`fixture-reset.log`; altrimenti il tentativo fallisce prima di avviare il
runner. Il runner riceve il nonce (`--fixture-nonce`) e, appena acquisito il
lock, verifica che `fixture-state.json` riporti proprio quel reset: se è stata
eseguita un'altra preparazione dopo, si ferma senza misurare. Si è scelta la
verifica del nonce invece di tenere il lock dalla preparazione all'avvio del
runner: il lock è di un processo, e il runner parte in un container separato
che non può ereditarlo.

## Ripresa, tentativi e spazio

Ripetere lo stesso comando riprende le fasi riuscite dopo averne verificato
tutti i digest. Cambiare sorgenti, configurazione o artefatti richiede una nuova
campagna. Un lock del sistema operativo impedisce due coordinatori contemporanei
sulla stessa directory e viene rilasciato anche quando il processo termina.

Una fase fallita o interrotta richiede una scelta esplicita, con motivazione.
Un retry di una fase già superata, o di una fase sconosciuta, viene rifiutato
con un errore prima di eseguire qualsiasi fase, sia in `--retry-phase` sia in
`--vm-retry-phase`: le evidenze superate non si rimisurano, perché ripetere
una misura riuscita finché il risultato conviene non è una qualifica. Per
misurare di nuovo si crea una campagna nuova. `--vm-retry-phase` richiede
anche `--retry-phase qualify-vm`, perché senza un nuovo tentativo VM il runner
non lo vedrebbe; viene rifiutata una fase che il runner non esegue per la
versione qualificata, come le fasi `spooled-*` prima della 2.1.0.

```powershell
python scripts/release_campaign.py --config target/campaign.json --output target/campaign-2.0.1 --retry-phase qualify-vm --vm-retry-phase transfers-large --retry-reason "Spazio del laboratorio ripristinato"
```

I tentativi precedenti restano presenti. Il retry crea una nuova directory,
non modifica i report falliti. Se il controller si interrompe mentre il runner
remoto è ancora attivo, attendere e controllare quel runner prima di avviare
un retry: il lock Linux impedisce campagne sovrapposte, ma non interrompe da
solo un processo remoto. I report selezionati per il bundle devono passare
nuovamente i validatori finali.

Dalla 2.1, se cambia soltanto l'indirizzo della stessa VM dopo un riavvio,
`--connect-host <nuovo-indirizzo>` consente la ripresa senza modificare la
configurazione già registrata. Richiede `--retry-reason` e le fasi precedenti
alla VM già superate, compresa la qualifica Windows. Il collegamento continua
a verificare la chiave SSH dell'host originale; una macchina con chiave diversa
viene respinta. Il nuovo tentativo registra indirizzo di trasporto e digest
della chiave. Se occorre preparare nuove fixture o rifare la qualifica Windows,
creare invece una nuova campagna con la configurazione corretta.

Prima di ogni nuova fase di trasferimento si controllano workspace, directory
temporanea e filesystem dei dati delle fixture. La riserva è il maggiore tra
8 GiB e il 10% del filesystem, più quattro payload per worker, oppure cinque
quando si qualifica la preparazione privata su disco della 2.1. È una policy
del laboratorio, non una soglia dichiarata del protocollo. Lo spazio viene
ricontrollato anche nei retry. Nessun comando cancella automaticamente dati,
evidenze o cache per aggirare il controllo.

## Pubblicazione

Usare il percorso del bundle restituito dal coordinatore:

```powershell
python scripts/publish_qualified.py --bundle target/campaign-2.0.1/seal/1/qualification-input.tar.gz --output target/publication-2.0.1 --repository PlenoraETL/plenora-storage-tools --notes docs/release-notes-2.0.1.md
```

La pubblicazione rivalida il bundle prima di creare tag e bozza, conserva il
checkpoint del workflow e non sostituisce tag o input di qualifica differenti.
Una bozza della stessa campagna può essere ripresa. Il workflow verifica gli
asset scaricati su entrambi i target prima della pubblicazione; il comando
controlla poi nuovamente i checksum e la ricevuta pubblica. Un workflow fallito
richiede `--retry-failed-workflow` dopo l'ispezione della causa.

## Riproduzione della pressione disco S3

`scripts/qualify_s3_disk_pressure.py` usa un MinIO usa e getta, con un tmpfs
da 256 MiB. Crea sorgente e destinazione, riduce lo spazio della sola fixture,
osserva `507 / XMinioStorageFull`, verifica l'errore pubblico e la conservazione
della destinazione, poi libera il riempimento e verifica il recupero con checksum.
Non riempie il filesystem host e non esporta messaggi o payload del server.

Il job `disk-pressure` di `release-candidate.yml` applica questa prova al binario
Linux finale. Dalla 2.0.1 il report è obbligatorio nel bundle dei gate sotto
`disk-pressure/report.json`. I test negativi del validatore e del coordinatore
sono eseguiti dalla suite tooling della CI.

La riproduzione su una fixture isolata conferma che lo spazio insufficiente
può produrre gli stessi campi d'errore della copia fallita nella 2.0.0.
Non ricostruisce una risposta server che quella campagna non aveva conservato:
il [resoconto storico](release-2.0.0.md#tentativo-da-1-gib-ripetuto) resta invariato.
