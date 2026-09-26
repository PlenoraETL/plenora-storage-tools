# Prove di affidabilità della serie 1.0

Il workflow [storage-reliability](../.github/workflows/reliability.yml) esegue
prove su fixture isolate. I report identificano il binario tramite SHA-256;
non qualificano account cloud reali e non costituiscono una ricevuta di release.

## Trasferimenti e concorrenza

Dentro il container collegato alle fixture:

```sh
cargo build --release --locked -p plenora-storage-cli
python3 scripts/qualify_transfers.py --bytes 1073741824
python3 scripts/qualify_transfers.py --bytes 1048576 --workers 4 --rounds 2
python3 scripts/qualify_transfers.py --bytes 1048576 --workers 16
```

Il test verifica upload, copia, download, checksum e cleanup di nomi univoci.
Le operazioni concorrenti condividono una directory nuova, così la creazione
dei parent viene esercitata anche quando le fixture sono già state utilizzate.
Ogni processo CLI ha una deadline; un timeout forzato non viene scambiato per
rollback. Le cancellazioni dopo effetti ambigui richiedono riconciliazione.

Il gate predefinito impone 256 MiB di RSS per processo, misurati dal kernel Linux
in un wrapper dedicato; comprende il costo di avvio del processo. Non è un SLO
di throughput e non misura la RAM dei server. Durata e CPU sono registrate per
operazione. Il consumer deve limitare anche la concorrenza e la memoria totale.

S3 con overwrite, SFTP, FTP e FTPS vengono esercitati in streaming a 1 GiB.
Local, Azure, GCS, SMB e WebDAV conservano il limite di upload/copy di 64 MiB:
il test verifica il rifiuto senza alterare un oggetto esistente. Anche il PUT
S3 condizionale conserva il limite di buffer. Questi casi non vengono contati
come trasferimenti grandi riusciti. I download grandi dei provider limitati
richiedono ancora una qualifica separata.

Le fixture FTP consentono 64 sessioni e 64 porte passive: una copia usa due
sessioni. SFTP consente le aperture simultanee necessarie alla matrice 1/4/16.
Questa capacità del laboratorio non modifica le policy dei server degli utenti.

## Coverage

```sh
bash scripts/coverage.sh
```

Il comando usa cargo-llvm-cov 0.9.1 e LLVM della toolchain bloccata. Esegue i test
Rust e le prove pubbliche CLI/Python sul codice strumentato. Il report completo
è `rust-coverage.json`, quello per crate è `coverage-summary.json`, sotto
`.fixtures/evidence/`. Il fork SMB è escluso dal totale del prodotto e riportato
separatamente. I test della wheel non misurano automaticamente le righe Python:
il report LLVM riguarda il codice Rust, incluso il bridge nativo.
Le righe dei moduli di test inline nei file Rust rientrano nella misura LLVM:
la percentuale non va presentata come coverage del solo codice di produzione.

Le percentuali iniziali sono misure, non soglie già soddisfatte. Prima della RC
vanno fissate soglie per modulo e censiti i percorsi di sicurezza/commit non
coperti. Una baseline ottenuta dai soli test Rust non va confusa con quella
estesa alle superfici pubbliche.

## Mutazione degli input

```sh
python3 scripts/fuzz_cli_inputs.py --cases 2000 --seed 7319
```

La campagna muta JSON di connessione, chiavi e cursor con seed riproducibile.
Usa operazioni di sola lettura su una fixture locale e controlla envelope,
assenza di credenziali nei messaggi ed effetti. Il report conserva seed e indice
del caso fallito. È separata dalla campagna guidata dalla coverage descritta
sotto e dalle prove di durata.

## Fuzzing dei parser XML e FTP

Il workflow [parser-fuzz](../.github/workflows/parser-fuzz.yml), richiamato da CI
e release-candidate, compila i parser effettivi di S3, Azure, WebDAV e FTP/FTPS con
cargo-fuzz 0.13.2, libFuzzer 0.4.13 e AddressSanitizer. La toolchain nightly è
fissata al 20 settembre 2026; non modifica la toolchain del prodotto.
Gli ingressi `cfg(fuzzing)` esistono solo nelle build strumentate, senza feature
o simboli aggiunti alla distribuzione. Vedere la [guida Rust Fuzz](https://rust-fuzz.github.io/book/cargo-fuzz/guide.html).

```sh
docker build -t storage-parser-fuzz -f fuzz/Dockerfile .
docker run --rm -v "$PWD:/workspace" -v storage-fuzz-registry:/usr/local/cargo/registry \
  storage-parser-fuzz cargo +nightly-2026-09-20 fetch --manifest-path fuzz/Cargo.toml --locked
docker run --rm --network none -v "$PWD:/workspace" -v storage-fuzz-registry:/usr/local/cargo/registry \
  storage-parser-fuzz python3 scripts/fuzz_parsers.py --seconds 300 --output target/parser-fuzz-manual
```

Il budget CI è 60 secondi per parser, esclusa la compilazione. Ogni input ha
limite di 64 KiB, timeout di 10 secondi e budget RSS del processo di 1 GiB.
FTP esercita anche il limite applicativo di 32 KiB per riga. La campagna non
qualifica i limiti delle risposte HTTP complete (8/32 MiB), i server reali o
tutte le combinazioni di input: copre parsing, nomi, metadata e casi malformati.
Gli oracoli controllano inoltre chiavi ed ETag accettati e categorie degli errori
S3/Azure. Un errore di parsing previsto non è un crash. I tre parser XML
rifiutano una radice estranea al protocollo: non la interpretano come elenco vuoto.

I seed sintetici sono versionati in `fuzz/seeds`; il report conserva seed,
toolchain, commit/stato sporco, hash del lockfile e dei binari, esecuzioni e archi
raggiunti. Log, corpus evoluto e crash sono salvati insieme al report. Il seed
non garantisce una sequenza identica fra macchine o budget temporali: un crash
va riprodotto dal suo file, quindi ridotto e trasformato in regressione.
Il runner rifiuta output già esistenti e non considera PASS un'uscita senza
evidenza di esecuzione strumentata. Le dipendenze sono scaricate prima; la
compilazione e la campagna usano il lockfile senza accesso alla rete.

## Durata dello SDK

```sh
python3 scripts/build_python.py --output .fixtures/soak-wheel
target/python-sdk-test/bin/python scripts/stress_python.py \
  --wheel .fixtures/soak-wheel/*.whl --duration-seconds 3600 \
  --output .fixtures/evidence/soak-python.json
```

La prova mantiene lo stesso engine Python sincrono, con quattro worker e tutti
i nove provider. Ogni ciclo verifica put/copy/get, checksum e cleanup; dopo il
primo ciclo impone crescita massima di 128 MiB RSS, 16 descrittori e 16 thread.
Il report identifica la wheel effettivamente installata e registra risorse per
provider, picchi e avanzamento. `RUNNING` non equivale a una prova superata.
Per l'alfa la durata richiesta è un'ora; per la RC si passa esplicitamente
`--duration-seconds 86400` sulla VM dedicata. Il workflow offre prove di 10 o
60 minuti. Questa campagna non misura un engine asyncio persistente.

## Compatibilità Python

La CI installa la medesima wheel ABI3 su CPython 3.10–3.14, Linux e Windows,
poi esegue i test da una directory esterna in modalità isolata. Un interprete
è qualificato solo quando il relativo job ha superato import, identità, typing,
lifecycle e operazioni; il solo tag ABI3 non costituisce quella prova.

Gli artifact Linux passano dal volume Docker a `.fixtures/evidence`, montato
dal checkout. Vengono esportati solo report pubblici: chiavi e certificati
privati delle fixture non fanno parte delle evidenze distribuite.
