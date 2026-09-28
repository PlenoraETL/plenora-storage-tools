# Documentazione

Le guide seguenti descrivono i contratti e le procedure correnti. Gli inventari
generati descrivono il codice; solo le ricevute riferite ai digest degli
artefatti attestano una qualifica.

| Documento | Contenuto |
| --- | --- |
| [STATO](STATO.md) | Inventario generato di crate, provider e operazioni |
| [Architettura](architecture.md) | Responsabilità e confini dei componenti |
| [API 1.0](api-1.0.md) e [inventario API](API-INVENTORY.md) | Contratti pubblici e baseline di compatibilità |
| [SDK Python](../crates/plenora-storage-py/README.md) | Utilizzo sync/async e lifecycle |
| [Compatibilità 1.0](compatibility-1.0.md) | Sistemi e limiti del supporto dichiarato |
| [Provider](provider-expansion.md) | Configurazione e garanzie dei sei adapter aggiunti dalla 0.2.0 |
| [Distribuzione](release.md) | Packaging e procedura di qualifica |
| [Evidenze finali](release-evidence-bundle.md) | Bundle dei gate 1.0, benchmark e workflow di pubblicazione |
| [Affidabilità](reliability.md) | Trasferimenti, coverage, fuzz e stress; identità delle misure riportate |
| [Adozione dei contratti](contract-adoption.md) | Profilo e prove di conformità |
| [Migrazione](migration-1.0.md) | Passaggio alla serie 1.0 |
| [Allineamento](database-alignment.md) | Principi del riferimento Database Tools |
| [Confronto con Database Tools](database-reference-review.md) | Analisi al 27 settembre 2026, differenze e criteri di completamento |
| [Consolidamento](quality-alignment-progress.md) | Interventi dopo il confronto, chiusura 1.0 e rimando alle attività successive |
| [Piano 2.0](roadmap-2.0.0.md) | Sei ambiti di allineamento, milestone e criteri verificabili della prossima major |
| [Piano 1.0](roadmap-1.0.0.md) | Piano storico: milestone, perimetro e criteri di uscita della prima release |

## Resoconti e baseline storiche

[Release 0.1](release-readiness-0.1.md),
[criteri 0.2.1](release-readiness.md),
[aggiornamento 0.2.2](dependency-update-0.2.2.md),
[baseline di audit](release-audit-baseline.md),
[evidenze del 18 settembre](release-evidence-20260918.json),
[avanzamento alfa](progress-1.0.0-alpha.1.md) e
[resoconti di avanzamento](progress-1.0.0.md) conservano le osservazioni dei
commit e dei momenti indicati. Le sezioni iniziali possono descrivere attività
poi completate o un perimetro successivamente aggiornato. Non attestano gli
artefatti di una release nuova; per il perimetro cloud corrente fa fede la
[matrice 1.0](compatibility-1.0.md).

`STATO.md` e `API-INVENTORY.md` sono generati: non modificarli manualmente.
