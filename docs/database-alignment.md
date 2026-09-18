# Allineamento al modello Database Tools

Database Tools è il riferimento per separazione delle responsabilità, utilizzo
applicativo e qualità del rilascio. Storage mantiene i propri contratti e la
licenza MIT/Apache-2.0. Il profilo database non è un profilo storage.

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

Lo SDK Python espone le sette operazioni tramite file e risultati tipizzati nel
codice Python. Non introduce pooling, transazioni storage, sincronizzazione di
directory, resume o copia tra provider. `close` impedisce nuove chiamate; quelle
già in corso usano i propri token di cancellazione. La cancellazione asyncio
attende l'esito della chiamata Rust e lo rende disponibile sull'eccezione.

Restano qualifiche distinte: adozione formale del profilo Python upstream,
benchmark con SLO, campagne fuzz e copertura quantitativa. Non vengono dichiarate
completate per analogia con Database Tools. Le prove su emulatori cloud, Samba e
WsgiDAV non certificano automaticamente account cloud reali, Windows Server,
Nextcloud, ACL/DFS ADLS o altri server.

Le ricevute già presenti in `dist/0.2.0` restano valide per i loro artefatti.
L'allineamento e lo SDK Python richiedono nuovi artefatti e nuove evidenze;
una build riuscita da sola non equivale a una qualifica di produzione.
