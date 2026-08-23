# Contratti pubblici storage v1

Questa directory possiede i contratti specifici di `plenora-storage-tools`.
Adotta i contratti comuni di `plenora-contracts` per superfici pubbliche,
capability discovery, errori, sicurezza e CLI.

## Identità

- componente: `plenora-storage-tools`;
- capability runtime futura: `plenora.storage-tools`;
- provider iniziali: `sftp`, `ftp`, `s3`;
- configurazioni provider: `plenora-storage-sftp-connection-v1`,
  `plenora-storage-ftp-connection-v1`, `plenora-storage-s3-connection-v1`.

Le operazioni v1 sono `storage.test`, `storage.list`, `storage.stat`,
`storage.get`, `storage.put`, `storage.copy` e `storage.delete`.

## Regole

- la connessione contiene solo configurazione non segreta e `credential_ref`;
- `get` e `put` trasferiscono byte fuori dal JSON;
- `put` e `copy` richiedono overwrite esplicito;
- deadline e cancellazione sono supportate;
- idempotency key non è ancora supportata;
- un errore di mutazione ambiguo usa `remote_effect: unknown` e
  `retry.kind: requires_recovery`.

`schemas/` contiene gli schemi Draft 2020-12. `bindings/rust-v1.json` collega
le operazioni agli export Rust documentati.
