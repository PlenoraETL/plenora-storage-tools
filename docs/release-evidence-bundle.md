# Evidenze finali e pubblicazione GitHub

La ricevuta della serie 1.0 usa lo schema 2. Il gate finale controlla anche le
prove elencate sotto: una qualifica incompleta non può produrre
`qualified_for_publication`. Un tentativo fallito invalida la precedente ricevuta
e il relativo `SHA256SUMS` nella directory candidata.

## Preparazione delle evidenze

Le prove di base restano quelle di [release.md](release.md), incluso
`webdav-fixture.json` accanto agli artefatti di ciascun target. Sotto la directory
passata a `qualify_release.py --evidence` aggiungere `gates/` con questa struttura:

| Percorso relativo a gates | Origine e controllo |
| --- | --- |
| `api/<target>/report.json` e i sette `.txt` | `check_rust_api.py`: Linux e Windows, commit pulito, confronto delle API con la baseline e digest dei testi |
| `sdk/<target>/<python>/sdk-tests.json`, `sdk-tests.log`, `coverage.json`, `sdk-typing.json`, `sdk-typing.log` | `check_installed_sdk.py --coverage --typing`: dieci combinazioni di target e CPython 3.10–3.14, stessa wheel qualificata per target, test senza skip, coverage e consumer statico |
| `fuzz/report.json`, `<parser>/run.log`, `<parser>/corpus/<seed>` | `fuzz_parsers.py`: quattro parser, ASan, almeno 60 secondi per parser, contatori dei log e digest dei seed originali; conservare anche il corpus evoluto nell'archivio della campagna |
| `coverage/coverage-summary.json`, `coverage/rust-coverage.json` | `coverage.sh`: commit pulito, contatori ricalcolati dal report LLVM e soglie per crate correnti |
| `transfers/large.json` | `qualify_transfers.py`: 1 GiB, un worker, nove provider con distinzione tra streaming riuscito e rifiuto previsto |
| `transfers/workers4.json`, `transfers/workers16.json` | Stesso script: 1 MiB, rispettivamente quattro worker per almeno due round e sedici worker per almeno un round |
| `soak/report.json` | `stress_python.py`: stessa wheel Linux, campagna terminata, nove provider, almeno quattro worker e budget delle risorse rispettati |
| `performance/baseline.json`, `performance/candidate.json`, `performance/report.json` | Due campagne indipendenti e confronto descritto sotto |

I target sono `x86_64-unknown-linux-gnu` e `x86_64-pc-windows-msvc`; i nomi Python
sono `3.10`, `3.11`, `3.12`, `3.13`, `3.14`. La soglia del soak resta un'ora per
le alfa e 24 ore per RC/stabile. Non sommare campagne parziali e non riassegnare
un report a un'altra wheel, anche se deriva dallo stesso commit.

Il validatore rilegge log, contatori e hash; non accetta il solo `PASS` di un
riepilogo. I report devono comunque provenire da esecuzioni fidate: un digest
non è una firma del runner. I soli file consumati vengono registrati nella
ricevuta e copiati sotto `dist/<version>/evidence/gates/`.

## Confronto delle prestazioni

Sul runner dedicato, con le fixture preparate e `PLENORA_CLI_BIN` riferito al
binario scelto, eseguire una campagna baseline e una campagna candidata separate:

```sh
python3 scripts/qualify_transfers.py --bytes 1048576 --workers 4 --rounds 5 --output .fixtures/evidence/performance/baseline.json
python3 scripts/qualify_transfers.py --bytes 1048576 --workers 4 --rounds 5 --output .fixtures/evidence/performance/candidate.json
python3 scripts/check_performance.py .fixtures/evidence/performance/baseline.json .fixtures/evidence/performance/candidate.json --output .fixtures/evidence/performance/report.json
```

La prima baseline può essere misurata sullo stesso binario del candidato, in
un'esecuzione indipendente. Per gli aggiornamenti si conserva la baseline del
binario di riferimento: non sostituirla con una nuova misura del candidato per
nascondere una regressione. Cambiamenti di hardware, kernel o definizioni delle
fixture richiedono una nuova baseline dichiarata. Il report registra questi
dati senza nomi host, endpoint o credenziali.

La policy 2 consente +10% sulla mediana dei tempi, +20% sul p95 e +10%
sul massimo RSS per coppia provider/operazione, con almeno cinque round e lo
stesso numero di campioni. Per i tempi il margine è il maggiore tra quello
percentuale e 10 ms sulla mediana / 20 ms sul p95. La
[calibrazione](performance-calibration.md) conserva i campioni che hanno
motivato il margine assoluto e il fallimento WebDAV che resta rilevato.
È un budget di regressione, non uno SLO di throughput già qualificato.
Un fallimento va indagato e le revisioni della policy richiedono dati conservati.
Il workflow [reliability](../.github/workflows/reliability.yml) espone l'opzione
`performance` per eseguire due campagne e il confronto sullo stesso runner.

## Pubblicazione

Il workflow [release](../.github/workflows/release.yml) parte da un tag già
creato sul commit finale e da una **bozza** GitHub Release. Non modifica release
già pubblicate. La bozza contiene `qualification-input.tar.gz`, preparato da:

```sh
python scripts/release_publication.py bundle --directory dist/1.0.0 --evidence target/release-evidence --archive target/qualification-input.tar.gz
```

Adattare la versione al candidato scelto. Il comando riesegue tutta la qualifica
e archivia solo file elencati da manifest e ricevuta; non include file estranei
della workstation. Non sostituisce un archivio di input già esistente.
Caricare l'archivio nella bozza con le release note e avviare il workflow con
il tag corrispondente, ad esempio `v1.0.0`. Non inserire segreti o certificati
privati nel materiale di qualifica.

Il workflow ricontrolla commit, artefatti e tutte le evidenze, prepara gli asset
diretti CLI/wheel/sorgenti e un archivio della qualifica, poi li carica nella
bozza. Due runner scaricano da GitHub ed eseguono CLI, SDK, esempio e typing su
Linux e Windows. Solo dopo il successo viene attestata la ricevuta e pubblicata
la bozza. Checksum e identità vengono ricontrollati dopo il download; asset
estranei impediscono la pubblicazione. L'archivio di input viene rimosso mentre
la release è ancora una bozza, lasciando il bundle di qualifica verificato.

Il job di download dispone di `contents: write` per vedere gli asset ancora in
bozza: GitHub limita la visibilità delle bozze a chi ha accesso push, come
descritto nella [documentazione delle release](https://docs.github.com/en/rest/releases/releases#list-releases).
La pubblicazione rimane affidata al job finale dopo i controlli sui due target.

L'[attestazione personalizzata](https://github.com/actions/attest#attestation-modes)
descrive la qualifica dei digest distribuiti. Non dichiara che i runner di
pubblicazione abbiano compilato gli artefatti prodotti sulla VM.

Questa procedura è implementata e sottoposta a controlli negativi in CI; la sua
presenza non significa che sia già stata completata una pubblicazione 1.0.
Le prove su account cloud reali rimangono rinviate secondo la
[matrice di compatibilità](compatibility-1.0.md).
