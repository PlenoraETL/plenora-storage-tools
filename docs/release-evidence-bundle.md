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
| `sdk/<target>/<python>/sdk-tests.json`, `sdk-tests.log`, `coverage.json`, `sdk-typing.json`, `sdk-typing.log` | `check_installed_sdk.py --coverage --typing`: due combinazioni di target e CPython 3.14, stessa wheel qualificata per target, test senza skip, coverage e consumer statico |
| `fuzz/report.json`, `<parser>/run.log`, `<parser>/replay-<seed>.log` | `fuzz_parsers.py`: otto target, ASan, replay di tutti i seed e 10.000 esecuzioni per target, contatori dei log e digest dei seed originali; conservare anche il corpus evoluto nell'archivio della campagna |
| `coverage/coverage-summary.json`, `coverage/rust-coverage.json` | `coverage.sh`: commit pulito, contatori ricalcolati dal report LLVM e soglie per crate correnti |
| `transfers/large.json` | `qualify_transfers.py`: 1 GiB, un worker, due round, nove provider con distinzione tra streaming riuscito e rifiuto previsto |
| `transfers/workers4.json`, `transfers/workers16.json` | Stesso script: 1 MiB, rispettivamente quattro worker per almeno due round e sedici worker per almeno un round |
| `soak/report.json`, `soak/calibration.json` | `stress_python.py`: stessa wheel Linux, campagna terminata, nove provider, almeno quattro worker e budget delle risorse rispettati |

I target sono `x86_64-unknown-linux-gnu` e `x86_64-pc-windows-msvc`; i nomi Python
sono `3.14`. La policy `storage-sdk-cycles-v2` richiede 5 cicli di warm-up
separati e 40 cicli misurati. Lo schema 2 conserva campioni, matrice completa,
quartili e risorse dopo close/cancel. Nessuna durata minima. Non sommare
campagne parziali o riassegnare report a un'altra wheel.

Il validatore rilegge log, contatori e hash; non accetta il solo `PASS` di un
riepilogo. I report devono comunque provenire da esecuzioni fidate: un digest
non è una firma del runner. I soli file consumati vengono registrati nella
ricevuta e copiati sotto `dist/<version>/evidence/gates/`.

## Prestazioni del candidato

4.0.0 non impone una baseline di una release precedente né un SLO assoluto
non calibrato. Restano tempi osservati, checksum e tetti RSS dei trasferimenti.
Gli strumenti A/B sono solo diagnostici; nessun loro file è richiesto dal seal.
Le ricevute storiche conservano la propria policy.

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

La provenienza di build (SLSA v1) e la SBOM CycloneDX di CLI, wheel e sorgenti
sono attestate dai job `linux` e `windows` di release-candidate, cioè dai runner
che compilano quei byte, subito dopo `build_release.py`. Prima di pubblicare, il
workflow di release verifica con `gh attestation verify` che ogni file distribuito
abbia entrambe le attestazioni, firmate da release-candidate di questo repository
su un runner ospitato da GitHub, al commit del tag; un artefatto senza di esse
blocca la pubblicazione. Per ciascun target la release pubblica anche la SBOM
(`plenora-storage-<versione>-<target>.sbom.cdx.json`) e il manifest di adozione
dei contratti (`plenora-storage-<versione>-<target>.adoption-manifest-v4.json`),
copiati dagli artefatti qualificati con lo stesso digest.

Questa procedura è implementata e sottoposta a controlli negativi in CI; la sua
presenza non significa che sia già stata completata una pubblicazione 1.0.
Le prove su account cloud reali rimangono rinviate secondo la
[matrice di compatibilità](compatibility-1.0.md).
