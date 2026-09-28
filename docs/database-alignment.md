# Allineamento al modello Database Tools

Database Tools è il riferimento per separazione delle responsabilità, utilizzo
applicativo e qualità del rilascio. Storage mantiene i propri contratti e la
licenza MIT/Apache-2.0. Il profilo database non è un profilo storage.

Il [confronto del 27 settembre 2026](database-reference-review.md) identifica
le revisioni esaminate e le differenze osservate in quel momento. Il
[piano 2.0.0](roadmap-2.0.0.md) registra gli interventi dopo la pubblicazione
della 1.0.0, il loro stato e i rispettivi criteri di completamento.
Seguire questi principi non significa avere già la stessa maturità del riferimento.

| Principio | Applicazione in Storage |
| --- | --- |
| Core indipendente dai provider | `plenora-storage-core` conserva modelli, errori, policy, lifecycle e runtime binding |
| Motore applicativo condiviso | `plenora-storage-engine::build_engine` compone i provider; CLI e Python usano questa factory |
| Provider selezionabili | Feature Cargo indipendenti, verificate da `scripts/check_features.py` |
| SDK Python nativo | PyO3, wheel ABI stabile Python >= 3.10, API sync/asyncio, marker PEP 561 e test della wheel installata |
| Discovery dal codice | `docs/STATO.md` deriva dai manifest e dall'esecuzione della CLI completa |
| Documentazione verificata | `scripts/check_docs.py` verifica inventario, versioni Python e link locali |
| Inventario dipendenze | SBOM CycloneDX 1.6 deterministico, grafo completo di Cargo.lock e digest degli artefatti |
| Errori senza payload | Errori Rust preservati in Python; le eccezioni dei resolver vengono sostituite con errori pubblici redatti |
| Qualifica riproducibile | Test offline, fixture dei provider, fault injection, consumer di archivi Cargo e wheel installata |

L'inventario SBOM comprende dipendenze opzionali, di sviluppo e di tutti i target:
non afferma che ciascuna sia collegata a ogni binario. Non include pacchetti del
sistema operativo o strumenti esterni di build. Un secondo inventario,
`qualification-sbom.cdx.json`, comprende i grafi dei lockfile fuzz/API e i pin
Python dichiarati nei file requirements. Questi pin non descrivono un ambiente
installato né tutte le sue dipendenze transitive. Entrambi gli inventari sono
ricostruiti e confrontati dal gate finale; la SBOM di prodotto lega anche gli
artefatti ai loro digest. Lo schema di riferimento è quello
[ufficiale CycloneDX 1.6](https://github.com/CycloneDX/specification/blob/1.6/schema/bom-1.6.schema.json).

La serie 2.0 aggiunge una [scansione separata degli artefatti](quality-2.0.md)
con Syft: CLI e wheel dei due target, import nativi, dati originali e digest.
Non estende retroattivamente l'ambito delle due SBOM dichiarative né promette
di rilevare ogni libreria incorporata o installata nell'ambiente di build.

Lo SDK Python espone le sette operazioni tramite file e conserva risultati
dizionario a runtime. Gli stub pubblici descrivono risultati `TypedDict`, policy
ammesse e controlli delle operazioni. La matrice della wheel installata esegue
un consumer mypy strict con casi validi e invalidi, oltre a un esempio sync/async.
Non introduce pooling, transazioni storage, sincronizzazione di
directory, resume o copia tra provider. `close` impedisce nuove chiamate; quelle
già in corso usano i propri token di cancellazione. La cancellazione asyncio
attende l'esito della chiamata Rust e lo rende disponibile sull'eccezione.

Il contratto comune Python è incluso nel manifest della wheel, con test della
distribuzione installata. Esistono gate distinti per parser fuzz, coverage Rust
per crate e coverage Python sulla wheel installata, descritti nelle
[prove di affidabilità](reliability.md). Il [gate finale](release-evidence-bundle.md)
richiede ora anche il confronto delle prestazioni e l'aggregazione dei report.
La coverage Rust del prodotto è ora distinta dai test e dispone di una baseline
misurata e di un gate nella CI ordinaria. La
[pubblicazione 1.0.0](https://github.com/PlenoraETL/plenora-storage-tools/actions/runs/36396706695)
ha completato la qualifica e le prove dopo download dei suoi artefatti;
la 2.0.0 richiederà nuove evidenze. L'esistenza
di un gate non certifica ogni candidato. Le prove su emulatori cloud, Samba e
WsgiDAV non certificano automaticamente account cloud reali, Windows Server,
Nextcloud, ACL/DFS ADLS o altri server.

Le ricevute già presenti in `dist/0.2.0` restano valide per i loro artefatti.
Le release successive richiedono i propri artefatti e le proprie evidenze;
una build riuscita da sola non equivale a una qualifica di produzione.
