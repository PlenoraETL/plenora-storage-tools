# Migrazione alla serie 2.0

La 2.0 mantiene i contratti operativi della 1.0: le sette operazioni, le
connessioni versionate, le firme Rust pubbliche, il protocollo CLI 2 e i
risultati dizionario dello SDK Python. La separazione dei moduli interni non
richiede di cambiare import pubblici, comandi o chiamate sync/async.

Il perimetro resta Rust, CLI e Python, nove provider, Linux/Windows x86_64 e
CPython 3.10–3.14. Le [restrizioni di compatibilità](compatibility-1.0.md)
rimangono applicabili: le fixture cloud non certificano account reali e il
profilo WebDAV richiede la configurazione qualificata. Limiti di buffering e
garanzie di pubblicazione non vengono ampliati dal refactoring.

Per aggiornare, sostituire gli archivi Rust/CLI o la wheel con quelli della
release 2.0 qualificata, verificarne i checksum e ripetere il consumer della
propria applicazione. La distribuzione rimane esclusivamente GitHub Releases.
Non usare una wheel alfa come prova della futura wheel stabile.

Chi contribuisce al progetto trova nuovi controlli di dimensione, commenti,
rustdoc, pin delle dipendenze e scansione degli artefatti: vedere
[Qualità 2.0](quality-2.0.md). I vincoli compatibili sul confine pubblico Rust
sono documentati separatamente dai pin delle dipendenze private.

La qualifica della nuova versione richiede nuove evidenze. Il tag 1.0.0 e la
sua ricevuta rimangono riferimenti immutabili, non attestazioni della 2.0.
