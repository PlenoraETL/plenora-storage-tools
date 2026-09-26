# Migrazione dalla 0.2.2 alla serie 1.0

La serie `1.0.0-alpha.N` è in sviluppo; non equivale a una release pronta per
la produzione. Il congelamento delle API e la qualifica finale sono tracciati
nel [piano](roadmap-1.0.0.md).

## Python

- `version()` e `__version__` usano la versione Python della wheel: per esempio
  `1.0.0a1`. Discovery conserva l'identità SemVer nativa `1.0.0-alpha.1`.
- `StorageError` deriva da `PlenoraError`. Anche gli input esplicitamente
  validati dal wrapper (configurazione, resolver, percorso e controlli) producono
  errori strutturati e redatti. Aggiornare i catch che dipendevano da `ValueError`
  o `RuntimeError` per questi casi. Errori di sintassi delle chiamate Python,
  come argomenti obbligatori mancanti, rimangono errori del linguaggio.
- `capabilities()` identifica la superficie `python_sdk`, con trasferimenti
  tramite file locali. I consumer non devono più aspettarsi il catalogo `rust`.
- Usare `await engine.aclose()` oppure `async with`; `await engine.close()`
  rimane disponibile come alias. La chiusura impedisce nuove operazioni e non
  cancella quelle in corso: attendere il loro esito prima di scartare l'engine.
- Dopo una cancellazione asyncio, usare `cancellation_outcome(error)` per leggere
  il risultato definitivo o l'errore storage. Il helper segue anche le eccezioni
  create da Python 3.10 e da `wait_for`; leggere direttamente gli attributi
  dell'eccezione esterna non è portabile. Un risultato `None` indica assenza di
  evidenza, non assenza di effetti remoti.

## Rust e SFTP

`Surface` include `PythonSdk`: aggiornare eventuali match esaustivi. Il resolver
SFTP può restituire `username` e `private_key`, con `passphrase` opzionale;
la chiave è il contenuto OpenSSH, non un percorso. L'autenticazione a password
rimane disponibile, ma le due modalità non si possono mescolare. Il pin della
host key resta obbligatorio salvo l'opt-in esplicito già previsto.

Limiti di trasferimento, atomicità e compatibilità server continuano a dipendere
dal provider e dalle evidenze pubblicate. Il cambio di versione non amplia da
solo queste garanzie.
