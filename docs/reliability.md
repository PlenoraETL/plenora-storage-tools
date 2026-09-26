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
del caso fallito. Non sostituisce il fuzzing guidato dalla coverage dei parser
XML/FTP, che resta aperto, né la campagna di durata di 24 ore prevista dal piano.

## Compatibilità Python

La CI installa la medesima wheel ABI3 su CPython 3.10–3.14, Linux e Windows,
poi esegue i test da una directory esterna in modalità isolata. Un interprete
è qualificato solo quando il relativo job ha superato import, identità, typing,
lifecycle e operazioni; il solo tag ABI3 non costituisce quella prova.

Gli artifact Linux passano dal volume Docker a `.fixtures/evidence`, montato
dal checkout. Vengono esportati solo report pubblici: chiavi e certificati
privati delle fixture non fanno parte delle evidenze distribuite.
