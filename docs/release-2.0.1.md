# Chiusura della release 2.0.1

Pubblicata il 29 settembre 2026 alle 13:12 UTC su
[GitHub Releases](https://github.com/PlenoraETL/plenora-storage-tools/releases/tag/v2.0.1),
senza distribuzione su crates.io o PyPI. Il tag identifica
`ce864ce16add3a26b27461da8ad43b29cd30d78a`.
Questo resoconto viene aggiunto dopo la pubblicazione e non modifica il tag
o le evidenze delle release precedenti.

## Prove degli artefatti finali

La [CI](https://github.com/PlenoraETL/plenora-storage-tools/actions/runs/36509967500),
il [candidato](https://github.com/PlenoraETL/plenora-storage-tools/actions/runs/36510085846)
e la [pubblicazione](https://github.com/PlenoraETL/plenora-storage-tools/actions/runs/36573199743)
sono passati. La pubblicazione ha rivalidato il bundle ed eseguito le prove
degli asset scaricati su Linux e Windows. Il controller ha poi verificato
nuovamente ricevuta e checksum pubblici.

- Rust: 1.171 test passati e nessuno ignorato su Linux; 1.158 passati e
  12 ignorati su Windows. Le prove con fixture ignorate su Windows non sono
  conteggiate come successi; CLI e SDK Windows hanno qualifiche separate.
- Nove provider sulle fixture dichiarate, CLI e SDK installato sui due target,
  matrice Python 3.10–3.14, API, typing, coverage, fuzz, audit e inventari nativi.
- Confronto prestazionale: trenta round per baseline 2.0.0 e candidato 2.0.1,
  nello stesso laboratorio; budget rispettati.
- Due round da 1 GiB e campagne con 4 e 16 worker. Sui cinque percorsi buffered
  il caso grande prova il rifiuto prima della mutazione: non certifica un
  trasferimento oltre il limite. La nuova modalità su disco appartiene alla 2.1.
- Soak finale: **7.216,090 secondi, 154 cicli, nove provider e quattro worker**.
  Picco RSS misurato 54.341.632 byte, 21 thread e 11 descrittori; budget rispettati.

## Ripresa dopo il riavvio

Il primo soak è stato interrotto dal riavvio della VM. L'ultimo report conserva
lo stato `RUNNING`, 4.860,490 secondi e 101 cicli: non è un test superato.
Dopo il cambio di indirizzo è stata verificata la stessa chiave SSH della VM.
Le fasi già passate sono state riutilizzate solo dopo il confronto dei digest;
il secondo tentativo ha eseguito due ore complete, senza sommare il tempo
del primo. Entrambi i report sono conservati:
[interrotto](evidence/2.0.1/soak-interrupted.json.gz) e
[completato](evidence/2.0.1/soak-completed.json.gz).

La [pressione disco](evidence/2.0.1/disk-pressure.json.gz) è stata riprodotta
con il binario Linux finale su MinIO e tmpfs isolato da 256 MiB. La risposta
507 conserva gli assi pubblici `execution / commit / unknown / requires_recovery`;
la destinazione precedente rimane integra e il recupero verifica il checksum.
Questa prova non ricostruisce la causa del tentativo storico della 2.0.0.

## Identità e perimetro

La [ricevuta](evidence/2.0.1/release-qualification.json.gz) associa tutte le
prove agli artefatti; il suo SHA-256 non compresso è
`b2cdc4e177d7f002bb36723f0135519de6fd5042e05a48150d216e5825ba4524`.

| Artefatto | SHA-256 |
| --- | --- |
| CLI Linux | `8911205d7dfdc5b2a0295a05e9e20a4800e040121675c3502edcce6021c8081d` |
| CLI Windows | `12942072f8eeff92e8f85a45003ecbfb3f88bd7be8e5506e82113b914d87be06` |
| Wheel Linux | `cffed81d4f4037a41196c37186f312444910402a5511b07991621199bf4f5478` |
| Wheel Windows | `df0aae9c7d62ab983c8387505de2add9d01365cb742ca5c6b5b474e6fa9000c0` |

Il perimetro resta quello delle [fixture dichiarate](compatibility-1.0.md).
AWS S3, Azure Blob e GCS su account reali rimangono non qualificati; WebDAV
mantiene la [configurazione verificata](webdav-compatibility.md).
