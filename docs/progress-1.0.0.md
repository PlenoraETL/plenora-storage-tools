# Avanzamento verso 1.0.0

Il [piano](roadmap-1.0.0.md) è in esecuzione. Distribuzione: solo GitHub Releases.
Le fasi non sono complete finché mancano le relative prove.

| Fase | Stato | Evidenza / lavoro aperto |
| --- | --- | --- |
| M0 | In corso | Correzione packaging e versioni prerelease su main: CI 35408111325 verde in tutti i job. Orchestrazione qualifica del candidato in completamento. |
| M1 | In corso | Sviluppo 1.0.0-alpha.1, discovery Python, PlenoraError, version(), aclose(): 17 test della wheel Windows superati. Restano matrice finale e congelamento API. |
| M2 | In corso | SFTP: chiavi Ed25519 semplici/cifrate, pin e rifiuto passphrase errata verificati sulla VM. Trasferimenti grandi e qualifica operativa restano aperti. |
| M3 | In corso | Prima baseline Rust misurata e gate per file grandi/concorrenza aggiunti. Raccolta estesa CLI/Python e matrice CPython in verifica; stress 24 ore, fuzz dei parser e cloud reali restano aperti. |
| M4 | Da completare | Asset installabili e prerelease GitHub qualificata. |
| M5 | Da completare | Qualifica finale, pubblicazione 1.0.0 e supporto. |

Dipendenza esterna aperta: configurazione degli account e namespace cloud di test
richiesta al proprietario. Le prove su emulatori proseguono sulla VM dedicata.

Nessuna release 1.0 pubblicata. Le prove indicate sono quelle effettivamente
eseguite; la CI verde riguarda il commit `1727d1f`, non certifica automaticamente
le modifiche successive. I test Python sono registrati in
`target/python-wheels/python-tests.log` e legati alla wheel dal relativo JSON.

Il 26 settembre l'alpha `3361a21` ha superato sulla VM 1.152 test Rust senza
skip e le operazioni CLI/SDK sui nove provider. La prova iniziale da 1 GiB ha
verificato S3/SFTP/FTP/FTPS con checksum; gli altri cinque hanno rispettato il
limite senza sovrascrittura. La concorrenza ha poi evidenziato race nella
creazione dei parent: correzioni e regressioni sono in verifica. Queste prove
iniziali non certificano automaticamente il binario modificato successivamente.

Procedura e limiti dei nuovi gate: [affidabilità](reliability.md).
