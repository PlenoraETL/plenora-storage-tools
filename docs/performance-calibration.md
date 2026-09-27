# Calibrazione iniziale del confronto delle prestazioni

La policy 2 mantiene i budget relativi del 10% sulla mediana, 20% sul p95
e 10% sul massimo RSS. Per i tempi usa un margine minimo assoluto di **10 ms
sulla mediana e 20 ms sul p95**. Il massimo ammesso è la baseline più il maggiore
tra margine relativo e assoluto; i due margini non si sommano. La memoria
non ha una tolleranza assoluta aggiuntiva. Questo budget di regressione non
è uno SLO di throughput o latenza del prodotto.

## Misura che ha motivato la revisione

Il 27 settembre 2026 sono state eseguite due campagne indipendenti sulla VM
dedicata: nove provider, payload da 1 MiB, quattro client e cinque round.
Entrambe hanno usato gli stessi byte del binario del commit `be65ae4`
(`2ff3ad9d47f71a08bb173af6cb8103f1f324e7cc88294303b8323370b95bee20`).
La misura comprende l'avvio di ogni processo CLI. Era attiva anche la campagna
SDK RC.1: queste misure non descrivono una VM altrimenti inattiva.

| Operazione | Mediana baseline | Mediana seconda campagna | Differenza |
| --- | --- | --- | --- |
| local stat | 2,95 ms | 5,80 ms | 2,85 ms |
| local put | 7,45 ms | 14,15 ms | 6,70 ms |
| local get | 6,85 ms | 8,05 ms | 1,20 ms |

Il p95 di S3 delete è passato da 18,6 a 26,9 ms e quello di SMB copy da 38,7
a 47,8 ms. La prima policy, esclusivamente percentuale, segnalava anche questi
scarti fra esecuzioni dello stesso binario. I margini assoluti sono una scelta
iniziale di budget basata su questa variabilità, non una stima statistica
universale né una prova che qualunque aumento sia rumore.

La stessa campagna ha rilevato un difetto concreto del laboratorio WebDAV a un
worker: mediana copy da 28,15 ms a 5,03155 secondi e p95 get da 33,6 ms a
10,0295 secondi. **Il confronto resta FAIL anche con la policy 2**, su tre
misure WebDAV. La configurazione del server è stata corretta separatamente;
il nuovo candidato deve superare due nuove campagne complete.

## Evidenze conservate

- [Baseline originale compressa](evidence/performance-2026-09-27/baseline.json.gz)
- [Seconda campagna originale compressa](evidence/performance-2026-09-27/candidate.json.gz)
- [Confronto originale, policy 1](evidence/performance-2026-09-27/original-comparison.json)
- [Stessi campioni valutati con la policy 2](evidence/performance-2026-09-27/calibrated-comparison.json)

I gzip preservano esattamente i JSON originali; i loro SHA-256 non compressi
sono registrati nel confronto originale. Per ripetere il calcolo, decomprimere
i due file e passarli a `scripts/check_performance.py`. I report sono storici:
non vanno inseriti nel bundle di qualifica del nuovo candidato. Le regressioni
negative del comparatore verificano che aumenti sostanziali di latenza e memoria
continuino a fallire. La CI esegue questi test nel job `product-quality`.
