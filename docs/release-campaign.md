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
CLI, installazione SDK, baseline/candidato prestazionale, confronto, trasferimenti
e soak hanno fasi separate. Le prove Windows e Linux coprono gli stessi nove
provider nel perimetro delle fixture.

## Ripresa, tentativi e spazio

Ripetere lo stesso comando riprende le fasi riuscite dopo averne verificato
tutti i digest. Cambiare sorgenti, configurazione o artefatti richiede una nuova
campagna. Un lock del sistema operativo impedisce due coordinatori contemporanei
sulla stessa directory e viene rilasciato anche quando il processo termina.

Una fase fallita o interrotta richiede una scelta esplicita, con motivazione:

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
