# Allineamento al modello Database Tools

Database Tools è il riferimento per separazione delle responsabilità, utilizzo
applicativo e qualità del rilascio. Storage mantiene i propri contratti e la
licenza MIT/Apache-2.0. Il profilo database non è un profilo storage.

Il [confronto del 27 settembre 2026](database-reference-review.md) identifica
le revisioni esaminate, le differenze ancora aperte e i criteri di completamento.
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
sistema operativo o strumenti di build. Lo schema di riferimento è quello
[ufficiale CycloneDX 1.6](https://github.com/CycloneDX/specification/blob/1.6/schema/bom-1.6.schema.json).

Lo SDK Python espone le sette operazioni tramite file, configurazioni tipizzate
e risultati ancora espressi come `dict[str, Any]`. Il marker PEP 561 e lo stub
nativo non sostituiscono un contratto statico preciso dei risultati pubblici.
Non introduce pooling, transazioni storage, sincronizzazione di
directory, resume o copia tra provider. `close` impedisce nuove chiamate; quelle
già in corso usano i propri token di cancellazione. La cancellazione asyncio
attende l'esito della chiamata Rust e lo rende disponibile sull'eccezione.

Il contratto comune Python è incluso nel manifest della wheel, con test della
distribuzione installata. Esistono gate distinti per parser fuzz, coverage Rust
per crate e coverage Python sulla wheel installata, descritti nelle
[prove di affidabilità](reliability.md). Restano da completare il confronto
delle prestazioni con una baseline, la misura Rust del solo codice di prodotto
e l'aggregazione delle evidenze richieste nella ricevuta finale. L'esistenza
di un gate non certifica ogni candidato. Le prove su emulatori cloud, Samba e
WsgiDAV non certificano automaticamente account cloud reali, Windows Server,
Nextcloud, ACL/DFS ADLS o altri server.

Le ricevute già presenti in `dist/0.2.0` restano valide per i loro artefatti.
Le release successive richiedono i propri artefatti e le proprie evidenze;
una build riuscita da sola non equivale a una qualifica di produzione.
