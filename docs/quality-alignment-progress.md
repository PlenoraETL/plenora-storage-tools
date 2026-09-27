# Consolidamento rispetto a Database Tools

Interventi successivi al [confronto del 27 settembre](database-reference-review.md).
Il confronto conserva le osservazioni dei commit esaminati; questo documento
descrive gli interventi, senza attribuire loro qualifiche dei candidati precedenti.

| Area | Implementato | Verifica ancora necessaria per la release |
| --- | --- | --- |
| Ricevuta finale | Controllo API, fuzz, coverage, dieci combinazioni Python, typing, trasferimenti, soak e performance; verifica dei file originali; invalidazione della ricevuta precedente su un tentativo fallito | Raccogliere il bundle completo del nuovo candidato, con gli stessi digest che saranno pubblicati |
| Distribuzione | Workflow da bozza GitHub, qualifica ripetuta, smoke dopo download su entrambi i target, attestazione della qualifica, checksum e pubblicazione solo dopo successo | Eseguire il workflow su una bozza qualificata; non è ancora una release pubblicata |
| SDK Python | Tipi pubblici dei risultati, policy Literal, controlli nominati negli stub, baseline dei tipi, consumer mypy strict ed esempio sync/async | Confermare la matrice completa CPython 3.10–3.14 sui due target per la wheel finale |
| Documenti | Controllo README dello SDK, link locali, ancore, script citati ed esempi Python; procedure correnti indicizzate | Eseguire e verificare le istruzioni con gli asset finali |
| Layout e coverage Rust | Quindici moduli di test spostati in file figli privati; misura del prodotto 83,49%, soglie ricalibrate e workflow nella CI ordinaria | Confermare il gate sul candidato finale e censire i percorsi critici non coperti |
| Commenti e API Rust | Controllo dei riferimenti al debito tecnico nei commenti reali; rustdoc di engine, policy, lifecycle, effetti e limiti dei trasferimenti degli adapter | Completare il dettaglio delle configurazioni pubbliche dei provider |
| Percorsi di sicurezza | Test isolati del resolver di credenziali e test delle classi di indirizzi speciali, IPv4 mappati e DNS locale | Misurare la coverage aggiornata e proseguire la revisione dei percorsi di commit |
| Errori degli stream | Corretto il flush dei download SFTP; prove di scrittura parziale, flush fallito/cancellato e sorgente interrotta sui nove adapter | Conservare gli esiti della suite sul commit RC.2 e sugli artefatti finali |
| CLI | Parsing degli argomenti e costruzione degli envelope JSON separati dal dispatch; snapshot dei comandi e test del protocollo invariati | Verificare gli eseguibili finali sui due target |
| Performance | Identità delle campagne, ambiente misurato, confronto mediana/p95/RSS e workflow eseguibile | Misurare sul laboratorio dedicato e valutare la stabilità delle soglie iniziali; nessun SLO già attestato |
| Inventario dipendenze | SBOM del prodotto e inventario separato dei lockfile fuzz/API e dei pin Python; confronto completo nella qualifica | Gli ambienti installati e le librerie native del sistema rimangono fuori dal perimetro dichiarato |

La verifica locale Windows del primo sviluppo ha eseguito 26 test sulla wheel
installata con Python 3.11.15, consumer mypy 2.3.1 e coverage Python del 100%
(232 righe e 36 rami). È una prova di sviluppo locale, non la qualifica delle
dieci combinazioni né degli artefatti RC costruiti in precedenza.

Restano interventi distinti: ulteriore suddivisione degli adapter per responsabilità,
revisione della documentazione degli adapter e inventario completo degli
ambienti di build installati. Non si dichiarano completati per la sola
presenza dei nuovi gate di distribuzione.
