# Plenora Storage Python SDK

Python >= 3.10, backed by the same Rust engine as the CLI. The standard wheel
contains local, S3, SFTP, FTP, explicit FTPS, Azure Blob, GCS, SMB and WebDAV.

```python
from pathlib import Path
from plenora_storage import Connection, Engine

connection = Connection("local", "plenora-storage-local-connection-v1",
                        {"root": str(Path("storage").resolve())}, "local:process")
with Engine() as engine:
    engine.put(connection, "report.csv", "report.csv", overwrite=False,
               publication_policy="atomic_required")
    engine.get(connection, "report.csv", "download.csv", overwrite=False)
```

The root directory must already exist. Local storage does not resolve credentials.
Other providers resolve `env:NAME` from a JSON object in the environment, or use
`Engine(credential_resolver=callback)` where the callback receives an opaque
reference and returns a mapping of provider-specific secret fields. Callbacks
must be thread-safe and bounded; Python callbacks cannot be forcibly interrupted.
Callback exception text is never exposed in storage errors.

`test`, `list`, `stat`, `get`, `put`, `copy`, `delete` accept `timeout_ms` and
`cancellation=CancellationToken()`. `get` and `put` stream files; no entire-object
Python byte buffer is required. Listing returns one page and a cursor owned by
the current engine. `copy` operates within one connection. Explicit `overwrite`,
`ignore_missing`, and `publication_policy` prevent accidental mutation defaults.
Provider configuration and limits are described in `docs/provider-expansion.md`
and the repository's vendored contracts.

`AsyncEngine` offers the same methods with `await` and `async with`. Cancelling
an asyncio task signals Rust and waits for the operation to settle; inspect
`CancelledError.storage_error` or `.storage_result` before retrying a mutation.
`StorageError`, a subclass of `PlenoraError`, exposes `code`, `category`, `phase`, `remote_effect`, `retry`, and
`provider`. Closing rejects new operations; it does not cancel existing calls.
Keep engines alive until pending calls finish. No connection pooling is promised.

`capabilities()` returns the `python_sdk` catalog for this wheel. `version()`
matches installed distribution metadata; prereleases use PEP 440 notation
(`1.0.0a1` corresponds to native `1.0.0-alpha.1`). `AsyncEngine.aclose()` is the
deterministic async lifecycle method; `await close()` remains an alias.
The common Python SDK contract and installed-wheel tests are included in the
adoption manifest. Operational qualification of each final wheel is separate.

Build locally with `maturin build --locked --manifest-path
crates/plenora-storage-py/Cargo.toml`. Select providers with `--no-default-features
--features local,s3`. Install and test the resulting wheel using
`python -m unittest discover -s crates/plenora-storage-py/python/tests`.
