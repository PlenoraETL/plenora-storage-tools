# Chiusura della release 2.1.0

Pubblicata il 30 settembre 2026 alle 07:33 UTC su
[GitHub Releases](https://github.com/PlenoraETL/plenora-storage-tools/releases/tag/v2.1.0),
senza distribuzione su crates.io o PyPI. Il tag identifica
`9df3d27d1356cb779aab7c85ed15adba50b71e0c`.
Questo resoconto viene aggiunto dopo la pubblicazione e non modifica il tag
o le evidenze delle release precedenti.

La release completa il [piano 2.1](roadmap-2.1.0.md): preparazione privata su
disco per put e copy su local, Azure, GCS, SMB e WebDAV, disponibile tramite
factory Rust, `--spool-uploads` nella CLI e `spool_uploads=True` nello SDK.
Le chiamate esistenti conservano i limiti buffered. La
[guida](large-transfers.md) descrive spazio temporaneo, limiti e garanzie;
non vengono aggiunti resume, quote aggregate o nuove garanzie di atomicità.

## Prove degli artefatti finali

La [CI](https://github.com/PlenoraETL/plenora-storage-tools/actions/runs/36663825301),
il [candidato](https://github.com/PlenoraETL/plenora-storage-tools/actions/runs/36664558153)
e la [pubblicazione](https://github.com/PlenoraETL/plenora-storage-tools/actions/runs/36684204248)
sono passati. La pubblicazione ha rivalidato il bundle ed eseguito le prove
degli asset scaricati su Linux e Windows. Il controller ha poi verificato
nuovamente ricevuta, inventario e checksum pubblici.

- Rust: 1.183 test passati e nessuno ignorato su Linux; 1.169 passati e
  13 ignorati su Windows. I test ignorati non sono conteggiati come successi;
  CLI e SDK Windows hanno qualifiche separate contro le fixture.
- Nove provider, CLI e SDK installato sui due target, matrice Python 3.10–3.14,
  API, typing, coverage, fuzz, audit, inventari nativi e
  [pressione disco](evidence/2.1.0/disk-pressure.json.gz).
- Report distinti per entrambe le modalità. I fault HTTP verificano gli assi
  pubblici degli errori; Linux esercita anche ENOSPC ed EACCES nella preparazione
  su disco, con destinazione preservata e assenza di residui temporanei.
- Due round da 1 GiB su tutti e nove i provider nella modalità preparata:
  [report completo](evidence/2.1.0/transfers-large-private-file.json.gz).
  Picco RSS per processo 32.559.104 byte; tra i cinque nuovi percorsi,
  21.110.784 byte. Entrambi sono sotto il limite di 256 MiB.
- La [prova grande predefinita](evidence/2.1.0/transfers-large-default.json.gz)
  verifica il rifiuto prima della mutazione sui cinque provider buffered:
  non certifica un upload da 1 GiB su quei percorsi. Le campagne con quattro
  e sedici worker completano rispettivamente 72 e 144 casi per modalità.
- [Soak finale](evidence/2.1.0/soak-completed.json.gz): **7.213,771 secondi,
  111 cicli per ciascuna modalità, nove provider e quattro worker**.
  Picchi osservati: 58.437.632 byte RSS, 24 thread e 15 descrittori;
  budget rispettati. Le due modalità sono esercitate nello stesso intervallo
  continuo; non vengono sommati test parziali.

## Confronto delle prestazioni

La [campagna isolata](https://github.com/PlenoraETL/plenora-storage-tools/actions/runs/36675818665)
ha usato i byte pubblicati della 2.0.1 e quelli finali della 2.1.0, senza
ricompilarli. Entrambi i binari sono stati verificati prima delle misure.
Sul medesimo runner GitHub, con quattro CPU logiche AMD EPYC 7763 e kernel
`6.17.0-1022-azure`, ciascuna campagna ha eseguito trenta round, quattro worker
e nove provider: 1.080 casi per versione con payload da 1 MiB.

La [baseline](evidence/2.1.0/performance-baseline.json.gz), il
[candidato](evidence/2.1.0/performance-candidate.json.gz) e il
[confronto](evidence/2.1.0/performance-comparison.json.gz) conservano le misure.
Tutte le 162 metriche sono passate: 54 mediane, 54 p95 e 54 massimi RSS.
La policy è invariata: +10% sulle mediane, +20% sul p95, +10% sul massimo RSS,
con margini assoluti minimi di 10 e 20 ms per i tempi. Il validatore finale
ha ricalcolato gli esiti dai campioni originali. Non è una promessa di throughput.

Il workflow [reliability](../.github/workflows/reliability.yml) espone ora
`candidate_run`, `baseline_tag` e `baseline_sha256` per riprodurre questa
modalità sui binari esatti. L'aggiornamento operativo ha superato la
[propria CI](https://github.com/PlenoraETL/plenora-storage-tools/actions/runs/36675809877)
ed è stato integrato dopo la pubblicazione, senza cambiare il sorgente del tag.

## Tentativi conservati

La prima qualifica Linux del candidato finale si è fermata durante `ensurepip`.
La causa non è stata accertata. Due prove isolate di creazione dell'ambiente
sono passate, poi la qualifica completa è stata ripetuta sugli stessi byte
con esito positivo; il primo tentativo rimane fallito.

Il primo confronto del candidato finale sulla VM conserva
[baseline](evidence/2.1.0/performance-vm-baseline.json.gz),
[candidato](evidence/2.1.0/performance-vm-candidate.json.gz) e
[risultato fallito](evidence/2.1.0/performance-vm-failed.json.gz):
sedici p95 oltre budget, con mediane e RSS entro i limiti.
La [diagnostica successiva](evidence/2.1.0/tail-diagnostic.json.gz) ha rilevato
superamenti anche tra due misure dello stesso binario 2.0.1. Comprende solo
cinque provider e dieci round: non è una qualifica della release.
L'[osservazione dell'host](evidence/2.1.0/host-load.jsonl.gz), successiva al
primo confronto, registra 175 campioni e 18 letture di CPU almeno al 95%.
Questi dati motivano il cambio di laboratorio, ma non provano da soli la
causa di ogni superamento. Nessuna soglia è stata allentata.

La [prima esecuzione del nuovo workflow](https://github.com/PlenoraETL/plenora-storage-tools/actions/runs/36675464738)
si è fermata prima delle misure per un'opzione Compose non supportata.
La correzione riguarda il comando operativo; la campagna successiva ha
eseguito tutte le misure senza cambiare i binari.

Il coordinatore iniziale conserva il proprio esito fallito. La raccolta finale
riunisce solo qualifiche completate e vincolate agli stessi artefatti:
Windows, Linux, sei campagne di trasferimento, soak e confronto isolato.
Il validatore completo ha ricontrollato questa raccolta prima di creare il
bundle e nuovamente durante la pubblicazione. Non sono state riutilizzate
prove di sviluppo o di candidati precedenti come evidenze del rilascio.

## Identità e perimetro

La [ricevuta pubblicata](evidence/2.1.0/release-qualification.json.gz) lega
artefatti, prove per piattaforma e 114 file dei gate aggiuntivi. Il suo SHA-256
non compresso è
`41e7206e96308efc0ac41ad784f573548a3f907d9eb94611daeaa97a78330084`.

| Artefatto | SHA-256 |
| --- | --- |
| CLI Linux | `efd184ef112504a29c5fba402bcd51334373c9601ce2f39808855bc70530b084` |
| CLI Windows | `b395094df64792c93fe19226558eb46fcf682b9e089e5f0bec9005f2ba8c3142` |
| Wheel Linux | `270af6131b1b9b06d8864feccba45b9e36e0617758fc2e362e578bfb0828990e` |
| Wheel Windows | `2721e6e04184c52f09f600252bd9ad6e6fd1b8ef25c7ecaf55f21c64ed1fa773` |

Il perimetro resta quello delle [fixture dichiarate](compatibility-1.0.md).
AWS S3, Azure Blob e GCS su account reali rimangono non qualificati; WebDAV
mantiene la [configurazione verificata](webdav-compatibility.md).
