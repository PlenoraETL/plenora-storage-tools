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
oltre metà del budget della mediana, 5% con un minimo di 10 ms
(`stability_allowance` in `scripts/performance-policy.json`), la misura è
**non affidabile**: lo stato è `UNRELIABLE`, mai `PASS`, anche se il
candidato ha anche regredito; `check_performance.py` esce con il codice 3,
distinto dal codice 1 della regressione, e chiede di ripetere la campagna su un
host stabile. Il report registra ogni controllo in `stability`.

Il minimo di 10 ms è lo stesso del budget di confronto ed è motivato dai dati
reali. Sulla campagna 2.1.0 accettata, su un host tranquillo, un minimo di 5 ms
avrebbe dichiarato non affidabile una misura buona (`azure copy`, 5,45 ms tra
le due metà); con 10 ms la campagna accettata non dà allarmi. Restano rilevate
le derive di almeno 10 ms sulle operazioni brevi (nelle misure della 3.0.0
`smb copy` −10,6 ms e `webdav copy` −10,3 ms) e quelle oltre il 5% sulle
operazioni lunghe (ftp e ftps tra −5% e −7% nella stessa misura, ftp da +6% a
+26% nella campagna VM fallita della 2.1.0). Limite dichiarato: uno
spostamento sotto i 10 ms su un'operazione sotto i 200 ms non viene rilevato,
anche se in percentuale è grande, come il +25/+32% (5–7 ms) di azure e smb in
quella campagna fallita; lì il confronto resta coperto dall'alternanza ABBA e
dal budget di 10 ms del confronto stesso.

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

### Ammissione ed epoca sulla VM

Una sola campagna alla volta lavora su una radice VM (`vm_root`), qualunque sia
la revisione qualificata. Il protocollo (`scripts/campaign_fence.py`) usa due
file in `<realpath(vm_root)>/.campaign/`, gli stessi per ogni checkout remoto
della stessa radice:

- `lock`, un `flock`. Il controller viene **ammesso** solo prendendolo in modo
  esclusivo, cosa che il kernel concede solo se nessun processo lo tiene in
  alcun modo; poi lo converte in condiviso e lo tiene finché resta aperto il
  suo canale SSH. Lo tengono condiviso, per tutta la loro vita, anche tutte le
  attività che toccano input, fixture o misure: ogni preparazione delle
  fixture (il wrapper apre il descrittore e i figli lo ereditano, anche se il
  controller muore) e il runner, che apre lo stesso file nel suo container
  attraverso un mount in sola lettura della directory;
- `epoch`, un contatore che l'ammissione incrementa sotto il lock esclusivo.
  Ogni attività riceve l'epoca della propria ammissione, prende il lock
  condiviso e la verifica prima di iniziare, prima di ogni scrittura (stato e
  ricevuta delle fixture, report selezionato) e alla fine di ogni fase
  misurata, prima di registrarla come superata. Il controller la verifica
  sulla VM prima di ogni caricamento, prima di avviare il runner, dopo averne
  scaricato le evidenze e alla fine della qualifica Windows. Un'epoca cambiata
  è un errore esplicito (`CampaignFenced`, codice 77 negli script) e il
  risultato non viene registrato: la fase in corso resta `FAIL`.

L'ammissione rifiuta anche finché esiste un container runner di questa radice
(etichetta Docker `plenora.campaign`) che non ha ancora preso il lock. Un
rifiuto, dell'ammissione o di una preparazione, non tocca niente ed esce con il
codice 75 (`CampaignBusy`); ogni altro errore del lock, compreso un `flock` che
fallisce per un motivo diverso dalla contesa, viene riportato come tale. Sulla
VM non si termina nessun processo: un'attività superstite non viene uccisa,
l'ammissione si rifiuta finché vive.

La qualifica Windows gira sul controller, ma solo finché l'ammissione vive: il
controller controlla il canale ogni secondo e, se cade, termina il
sottoprocesso (lo uccide dopo 10 secondi) e fallisce con `CampaignLost`, senza
registrare il risultato; finita la qualifica, verifica l'epoca sulla VM.

**Impedito per costruzione**: due ammissioni contemporanee; un'ammissione
mentre vive un'attività di un'ammissione precedente (controller, preparazione
staccata con `nohup`, runner), anche se il suo controller è morto o qualifica
un'altra revisione in un'altra `remote_root`; un'ammissione mentre un runner
etichettato è stato creato ma non ha ancora preso il lock.

**Soltanto rilevato**: il lavoro di un controller che ha perso l'ammissione
senza saperlo, per esempio in una partizione di rete dopo la quale il server
SSH ha chiuso il canale e un altro controller è stato ammesso. Le sue attività
che partono dopo la nuova ammissione trovano un'epoca diversa e si fermano
prima di toccare qualcosa; quelle già in corso falliscono al controllo
successivo (prossima scrittura o fine fase) e il loro risultato non viene
registrato. Fra il controllo dell'epoca e la scrittura che segue resta una
finestra: un caricamento o una scrittura già partiti possono completarsi dopo
la nuova ammissione. Se le due campagne qualificano revisioni diverse quella
scrittura finisce nella `remote_root` della vecchia (`<vm_root>/<versione>-<revisione>`),
che la nuova non legge. Se qualificano la stessa revisione la `remote_root` è
la stessa e la finestra non è chiusa dal protocollo: il runner verifica solo
che i binari corrispondano al manifest caricato con loro. Per questo due
controller della stessa revisione non si avviano sulla stessa VM. La
conversione del lock da esclusivo a condiviso non è atomica in `flock`: se
un'altra ammissione lo prende in quell'istante, la prima esce con 75 e vale
l'epoca più recente.

Limite dichiarato: il lock del controller si libera quando sulla VM il canale
SSH si chiude. Se il controller termina, la connessione si chiude subito; in
una partizione di rete il server SSH della VM se ne accorge solo con i propri
keepalive (`TCPKeepAlive`, e `ClientAliveInterval` se configurato): fino ad
allora il lock resta tenuto e un altro controller viene rifiutato, il verso
sicuro. Il controller, dal suo lato, smette di lavorare appena vede il canale
chiuso.

### Fixture ricreate prima del runner

All'inizio di ogni tentativo `qualify-vm` il coordinatore riesegue la
preparazione delle fixture con `PLENORA_FIXTURE_RECREATE=1`. Ogni preparazione,
anche quella di `prepare-vm`, ha un nonce unico, registrato nel tentativo
locale (`<etichetta>-nonce.json`) e usato nei nomi dei file di segnale sulla
VM: un segnale lasciato da un'esecuzione precedente non viene mai letto come
quello corrente. Il wrapper registra sempre il codice d'uscita reale ed esce
con quel codice. In ordine:

1. il wrapper prende in modo condiviso il lock di ammissione e poi, in modo
   esclusivo, `.fixtures/campaign/campaign.lock` del checkout: se un lock è
   occupato si ferma prima di toccare qualcosa (codice 75). Lo script verifica
   l'epoca all'avvio, prima del marcatore `in-progress`, prima di ricreare e
   prima di registrare la ricevuta: un'epoca cambiata lo ferma con il codice
   77. In entrambi i casi la campagna termina con un errore esplicito;
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

Prima di modificare qualsiasi fixture, ogni preparazione scrive in
`fixture-state.json` il proprio nonce con stato `in-progress`: una preparazione
che si ferma dopo questo punto invalida la ricevuta precedente, e nessun runner
la accetta. Le uscite precedenti al marcatore (lock occupato, epoca cambiata, runner
ancora in esecuzione) non toccano le fixture e lasciano valida la ricevuta precedente. Prima di ricreare, il reset controlla anche la
memoria disponibile (`scripts/check_memory.py`): il fixture GCS in memoria
tiene sorgente e copia del trasferimento spooled da 1 GiB, un picco di circa
2 GiB, e serve una riserva di altri 2 GiB per le altre fixture e il runner. Lo
stesso controllo precede la fase `spooled-large`. Se la memoria non basta, si
ferma con un errore esplicito e non libera niente da solo.

Le sonde girano sulla VM e raggiungono le fixture in loopback. Il TLS di FTPS
viene verificato per il nome dell'host della configurazione, che è nel
certificato: dopo una ripresa con `--connect-host` cambia solo l'indirizzo con
cui il controller raggiunge la VM, non l'identità verificata, e la verifica non
viene mai disattivata.

Solo se tutte le fixture rispondono il tentativo registra `fixture-reset.json`
con `recreated: true`, il nonce e l'epoca, insieme a `fixture-check.json` e
`fixture-reset.log`; altrimenti il tentativo fallisce prima di avviare il
runner. Il runner riceve il nonce (`--fixture-nonce`) e l'epoca (`--epoch`) e,
appena acquisito il lock condiviso e verificata l'epoca, verifica che `fixture-state.json` riporti proprio quel reset: se è stata
eseguita un'altra preparazione dopo, si ferma senza misurare. Fra la fine della
preparazione e l'avvio del runner il lock di ammissione resta tenuto dal
controller, quindi nessun'altra campagna può preparare nel frattempo; la
verifica del nonce copre una preparazione successiva della stessa campagna.

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
un retry: l'ammissione si rifiuta finché quel runner vive, ma non lo
interrompe. I report selezionati per il bundle devono passare
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
