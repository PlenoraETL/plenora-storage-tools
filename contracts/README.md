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
le operazioni agli export Rust documentati. Gli esempi in `examples/valid` e
`examples/invalid` sono verificati dai test; la matrice completa e le decisioni
ancora aperte sono in `STORAGE-OPERATIONS-1.0-PROPOSAL.md`.

Il bordo runtime è intenzionalmente transport-neutral: il crate esporta i DTO,
i selector e la validazione fail-closed, ma l'adapter concreto e la risoluzione
degli artifact appartengono all'applicazione consumer.

## Pin e adozione corrente

`upstream/source.json` fissa `3aec9f724f5a0ad21366f293895b4d0dbcd0aad1`.
`upstream/vendor-lock.json` elenca origine e SHA-256 delle copie adottate;
`upstream/proposed` e storico, `upstream/ratified` contiene le sonde normative.
`adoption-policy.json` alimenta il manifest v4 prodotto dal packaging, incluse
le deviazioni RT-024 ed ERR-016. Catalogo e profilo storage restano v1;
si adottano Public Catalogs 2.0 e Runtime Binding 2.0 (wire v1).
Vedere [adozione](../docs/contract-adoption.md) e
[limiti](../docs/contract-limits.md). Verifica offline:
`python scripts/check_contract_adoption.py`.
