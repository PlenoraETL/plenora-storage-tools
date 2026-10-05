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
I test dei crate Storage sono ora in file figli privati, referenziati sotto
`cfg(test)`. `check_test_layout.py` impedisce di reintrodurli nei file di
prodotto; il riepilogo esclude questi file usando le dichiarazioni effettive,
non il solo nome. Il fork SMB conserva i test upstream ed è riportato a parte.
La baseline storica seguente includeva ancora i test inline: i suoi numeri e
le relative soglie non costituiscono una misura del nuovo denominatore.

### Baseline storica con test inline

La campagna sul commit `ddf4a3f05cbfc6bb90fe8972124a07b2666123d0`, con test
Rust e prove CLI/Python su tutte le fixture, misura 6.504/7.583 righe (85,77%)
escludendo il fork SMB. Da questa misura derivavano le soglie storiche riportate
sotto: circa 2–3 punti percentuali di margine, per
bloccare regressioni senza trattare la percentuale come prova di correttezza.

| Crate | Baseline (%) | Minimo (%) |
| --- | ---: | ---: |
| core | 88,33 | 86 |
| engine | 91,67 | 89 |
| CLI | 83,43 | 81 |
| FTP/FTPS | 85,58 | 83 |
| SFTP | 84,69 | 82 |
| S3 | 75,41 | 73 |
| providers | 88,15 | 86 |
| bridge Python Rust | 90,43 | 88 |
| fork SMB, separato | 88,46 | 86 |

### Baseline del codice di produzione

La [misura del commit d8827bb](coverage-production-baseline.json) registra
5.369/6.431 righe di prodotto coperte (83,49%), dopo la separazione dei quindici
moduli di test. La campagna ha eseguito tutti i test Rust senza skip e le prove
CLI/Python sulle fixture. Il report LLVM è identificato dal digest nella baseline;
la misura non qualifica automaticamente un candidato successivo.

| Crate | Baseline prodotto (%) | Minimo corrente (%) |
| --- | ---: | ---: |
| core | 86,97 | 84 |
| engine | 90,43 | 88 |
| CLI | 81,06 | 79 |
| FTP/FTPS | 81,33 | 79 |
| SFTP | 79,58 | 77 |
| S3 | 73,80 | 71 |
| providers | 87,54 | 85 |
| bridge Python Rust | 90,43 | 88 |
| fork SMB, separato e con test upstream | 88,47 | 86 |

Le soglie correnti in `scripts/coverage-policy.json` mantengono almeno due punti
di margine rispetto alla baseline, arrotondando verso il basso. Il denominatore
è cambiato: queste soglie sostituiscono quelle che includevano il codice dei test,
senza presentare il cambiamento della percentuale come una regressione funzionale.

`coverage.sh`, eseguito dalla [CI ordinaria](../.github/workflows/coverage.yml)
e dal workflow di affidabilità con `coverage=true`, impone le soglie correnti
e rifiuta crate mancanti o non ancora censiti nella policy. Il
report conserva commit, stato del checkout e digest del report LLVM originale.
Il confronto usa i conteggi esatti; arrotondare la percentuale non permette di
superare una soglia. Le soglie non vengono ridotte senza una misura conservata.
Restano da censire e chiudere i percorsi di sicurezza/commit non coperti.

Il wrapper Python ha un gate distinto nella matrice CPython 3.10–3.14 su Linux
e Windows. `scripts/check_installed_sdk.py --coverage` usa coverage.py 7.16.1
sulla wheel installata, da una directory esterna al checkout, misurando righe e
rami. Confronta i file misurati con i byte nell'archivio wheel e rifiuta moduli
mancanti, test omessi o suite vuote. I report legano la misura al digest della
wheel; non misurano il codice Rust o le compatibilità remote.

La prima misura Windows/Python 3.11 dopo l'estensione a 25 test raggiunge
199/199 righe e 36/36 rami del wrapper: include un ciclo asincrono completo,
cancellazioni ripetute e redazione degli errori nativi non strutturati. Le
soglie per modulo in `scripts/coverage-policy.json` sono 98% righe e 95% rami;
lasciano un margine limitato rispetto alla baseline, impedendo regressioni
ampie. Le eccezioni richiedono revisione della policy; un totale alto di un
altro modulo non compensa quello sotto soglia. I rami riportati da coverage.py
non rappresentano ogni possibile eccezione o interleaving dei thread.

## Mutazione degli input

```sh
python3 scripts/fuzz_cli_inputs.py --cases 2000 --seed 7319
```

La campagna muta JSON di connessione, chiavi e cursor con seed riproducibile.
Usa operazioni di sola lettura su una fixture locale e controlla envelope,
assenza di credenziali nei messaggi ed effetti. Il report conserva seed e indice
del caso fallito. È separata dalla campagna guidata dalla coverage descritta
sotto e dalle prove di durata.

## Fuzzing dei parser XML, FTP e SMB

Il workflow [parser-fuzz](../.github/workflows/parser-fuzz.yml), richiamato da CI
e release-candidate, compila i parser effettivi di S3, Azure, WebDAV e FTP/FTPS con
cargo-fuzz 0.13.2, libFuzzer 0.4.13 e AddressSanitizer. La toolchain nightly è
fissata al 20 settembre 2026; non modifica la toolchain del prodotto.
Gli ingressi `cfg(fuzzing)` esistono solo nelle build strumentate, senza feature
o simboli aggiunti alla distribuzione.

Quattro target coprono i decoder di `plenora-smb2` che leggono byte dal server:
`smb2_messages` (transform header, split dei compound, header e corpo di ogni
comando, contesti d'errore), `spnego_der` (TLV DER, `negTokenResp` SPNEGO,
wrapper GSS-API), `ntlm_challenge` (CHALLENGE_MESSAGE e AV pair tramite
l'autenticatore pubblico, con e senza NEGOTIATE) e `kerberos_messages` (risposte
KDC, KRB-ERROR, AP-REP, parti decifrate, ticket e credential cache). Gli ingressi
stanno in `smb2::fuzzing`, dietro la feature `fuzzing` che la distribuzione non
abilita. Vedere la [guida Rust Fuzz](https://rust-fuzz.github.io/book/cargo-fuzz/guide.html).

```sh
docker build -t storage-parser-fuzz -f fuzz/Dockerfile .
docker run --rm -v "$PWD:/workspace" -v storage-fuzz-registry:/usr/local/cargo/registry \
  storage-parser-fuzz cargo +nightly-2026-09-20 fetch --manifest-path fuzz/Cargo.toml --locked
docker run --rm --network none -v "$PWD:/workspace" -v storage-fuzz-registry:/usr/local/cargo/registry \
  storage-parser-fuzz python3 scripts/fuzz_parsers.py --seconds 300 --output target/parser-fuzz-manual
```

Il budget CI è 60 secondi per parser, esclusa la compilazione. Il workflow
[scheduled](../.github/workflows/scheduled.yml) ripete ogni lunedì la campagna con
300 secondi per parser e l'audit delle dipendenze (cargo audit sui tre lockfile,
cargo deny, audit del nome upstream SMB), così un advisory o un crate ritirato
dopo l'ultimo push emerge entro una settimana. Ogni input ha
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
  --wheel .fixtures/soak-wheel/*.whl --duration-seconds 7200 \
  --output .fixtures/evidence/soak-python.json
```

La prova mantiene lo stesso engine Python sincrono, con quattro worker e tutti
i nove provider. Ogni ciclo verifica put/copy/get, checksum e cleanup; dopo il
primo ciclo impone crescita massima di 128 MiB RSS, 16 descrittori e 16 thread.
Il report identifica la wheel effettivamente installata e registra risorse per
provider, picchi e avanzamento. `RUNNING` non equivale a una prova superata.
La durata provvisoria richiesta dal 28 settembre 2026 è **due ore per tutte
le versioni** (alfa, beta, RC e stabili), anche senza specificare
`--duration-seconds`. Il gate e il runner condividono `scripts/soak_policy.py`.
La durata sarà rivalutata prima del rilascio definitivo. Il workflow propone
120 minuti; le opzioni da 10 o 60 minuti restano diagnostiche e non soddisfano
il gate. Il timeout del job include anche build, trasferimenti e raccolta
delle evidenze. Questa campagna non misura un engine asyncio persistente.

I report storici conservano durata ed esito originali. Una campagna da 24 ore
interrotta non diventa automaticamente una campagna completata da due ore:
le nuove prove usano un report distinto e terminano tutti i cicli e il cleanup.

## Compatibilità Python

La CI installa la medesima wheel ABI3 su CPython 3.10–3.14, Linux e Windows,
poi esegue i test da una directory esterna in modalità isolata. Un interprete
è qualificato solo quando il relativo job ha superato import, identità, typing,
lifecycle e operazioni; il solo tag ABI3 non costituisce quella prova.

Gli artifact Linux passano dal volume Docker a `.fixtures/evidence`, montato
dal checkout. Vengono esportati solo report pubblici: chiavi e certificati
privati delle fixture non fanno parte delle evidenze distribuite.
