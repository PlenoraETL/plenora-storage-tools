# Avanzamento verso la 1.0 — 26 settembre 2026

Questo resoconto registra prove di sviluppo. Non è una ricevuta di qualifica
della 1.0.0 e non modifica le evidenze storiche della serie 0.2.

## Difetti chiusi

- FTP/FTPS, SFTP e WebDAV riconciliano la creazione concorrente dei parent solo
  dopo aver verificato che il percorso sia una directory. Le prove a 4 e 16
  worker sono passate sui nove provider; i casi file/assente restano errori.
- Il ricevitore SMB non mantiene più una connessione inattiva tramite un
  riferimento forte. La prova continuativa esponeva un socket trattenuto per
  operazione SMB. La regressione TCP verifica il rilascio dopo l'ultimo clone;
  la campagna breve successiva mantiene 10 descrittori, senza crescita per ciclo.
- `cancellation_outcome()` recupera il risultato definitivo anche attraverso
  le eccezioni create da Python 3.10 e `asyncio.wait_for`. I test verificano sia
  l'errore storage sia un'operazione completata mentre arriva la cancellazione.
- La fixture MinIO si costruisce da sorgente ufficiale bloccato e verificato;
  non dipende dalle immagini Quay diventate indisponibili sui runner puliti.

## Copia verificata sulla VM

Il codice della campagna è il commit
`9a583c8f6ae0e093f3ca3929093329d47a9837dc`, estratto da un archivio Git con SHA-256
`5e9e20edadac7f1763be7d21b25ae9bfe821b4f64402ee06e75e9a984e5276e8`.
La directory dedicata è `/home/marco/plenora-storage-soak-9a583c8`; le fixture
rimangono isolate nel progetto Compose `storage-release`.

| Prova | Esito verificato |
| --- | --- |
| `scripts/verify.sh` | 1.155 test Rust passati, zero falliti e zero omessi; fmt e Clippy passati |
| Wheel installata | 19 test SDK passati; sette operazioni su tutti i nove provider |
| CLI sulle fixture | Sette operazioni sui nove provider, HTTPS e SSH con pin; gate fault passati |
| Payload di 1 GiB | S3 overwrite, SFTP, FTP e FTPS: trasferimento/copia/checksum passati sotto il limite RSS di 256 MiB |
| Provider con buffer | Local, Azure, GCS, SMB e WebDAV: rifiuto del payload oltre il limite con destinazione preesistente preservata; non sono trasferimenti da 1 GiB riusciti |
| Durata SDK | Campagna di 24 ore avviata il 26 settembre alle 04:56:55 UTC; esito ancora da acquisire |

Binario release usato per la misura da 1 GiB:
`12566cb1f471d5b1875db2ee1afa68521dcb5e3a3d74484e540944a4b8a86ce2`.
Wheel installata nella campagna di durata:
`1d8a303b9c1b67b372a7f8bbc0f4e4c9f5c16de9112345595a26864c16a88a10`.
I report e il log completo sono sotto `.fixtures/evidence` nella copia dedicata.
Il JSON della campagna deve terminare con `PASS`; la presenza del processo o di
un checkpoint `RUNNING` non soddisfa il gate.

## Distribuzione e prove ancora aperte

Il packaging CLI produce ZIP per Windows e tar.gz per Linux e confronta i byte
dell'eseguibile archiviato con quelli del binario qualificato. Test negativi
rifiutano eseguibili differenti e pacchetti senza licenze. L'avvio di una CLI
Windows estratta dallo ZIP è stato verificato fuori dal checkout.

Il [workflow di affidabilità](../.github/workflows/reliability.yml) contiene
misure di memoria/concorrenza, coverage, 2.000 mutazioni CLI riproducibili e
prove SDK continuative. La coverage rimane una baseline: include i test inline
Rust ed esclude il fork SMB dal totale del prodotto. Non è ancora un insieme
di soglie per modulo né una misura delle righe del wrapper Python.

Restano da chiudere nel [piano](roadmap-1.0.0.md): esito delle 24 ore, soglie e
lacune di coverage, fuzz dei parser XML/FTP, benchmark con soglie deliberate,
congelamento API, bundle sorgente Rust installabile, aggregazione e qualifica
degli asset finali e pubblicazione GitHub. Le prove AWS/Azure/GCS reali richiedono
account e namespace dedicati con budget: le fixture non sostituiscono tali prove.
Nessuna release stabile o release candidate è pubblicata da questo avanzamento.
