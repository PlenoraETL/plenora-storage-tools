# Avanzamento verso 1.0.0

Il [piano](roadmap-1.0.0.md) è in esecuzione. Distribuzione: solo GitHub Releases.
Le fasi non sono complete finché mancano le relative prove.

| Fase | Stato | Evidenza / lavoro aperto |
| --- | --- | --- |
| M0 | In corso | Causa dei fallimenti CI identificata: cache Cargo incompleta per metadata offline Maturin. Correzione fetch e gestione prerelease in verifica. |
| M1 | Da completare | Congelamento superficie e profilo Python. |
| M2 | Da completare | Autenticazione SSH a chiave e trasferimenti grandi. |
| M3 | Da completare | Coverage, fuzz, benchmark, stress, test cloud reali. |
| M4 | Da completare | Asset installabili e prerelease GitHub qualificata. |
| M5 | Da completare | Qualifica finale, pubblicazione 1.0.0 e supporto. |

Dipendenza esterna aperta: configurazione degli account e namespace cloud di test
richiesta al proprietario. Le prove su emulatori proseguono sulla VM dedicata.
