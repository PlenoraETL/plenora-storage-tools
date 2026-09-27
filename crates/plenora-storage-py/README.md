# Plenora Storage Python SDK

Python >= 3.10, backed by the same Rust engine as the CLI. The standard wheel
contains local, S3, SFTP, FTP, explicit FTPS, Azure Blob, GCS, SMB and WebDAV.

## Installation

Distribution is through [GitHub Releases](https://github.com/PlenoraETL/plenora-storage-tools/releases).
When a qualified release is published, download the wheel for your platform,
verify its SHA-256 against the release checksums and install the downloaded file
with `python -m pip install <wheel-file>`. The first 1.0 is still a candidate;
availability of source code does not imply that a production release is published.
The supported targets and fixture-only compatibility scope are listed in the
[compatibility matrix](../../docs/compatibility-1.0.md).

## Basic use

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
Provider configuration and limits are described in the [provider guide](../../docs/provider-expansion.md)
and the repository's vendored contracts.

`AsyncEngine` offers the same methods with `await` and `async with`. Cancelling
an asyncio task signals Rust and waits for the operation to settle. Pass the
caught exception to `cancellation_outcome(error)` before retrying a mutation:
it returns the settled result dictionary, a `PlenoraError`, or `None` when no
outcome is known. `None` never proves that a mutation had no effect. The helper
also follows `wait_for` timeout causes and Python 3.10 cancellation contexts;
direct access to `.storage_error`/`.storage_result` is not portable across
these wrappers. Do not suppress the cancellation after inspecting its outcome.
`StorageError`, a subclass of `PlenoraError`, exposes `code`, `category`, `phase`, `remote_effect`, `retry`, and
`provider`. Closing rejects new operations; it does not cancel existing calls.
Keep engines alive until pending calls finish. No connection pooling is promised.

`capabilities()` returns the `python_sdk` catalog for this wheel. `version()`
matches installed distribution metadata; prereleases use PEP 440 notation
(`1.0.0a1` corresponds to native `1.0.0-alpha.1`). `AsyncEngine.aclose()` is the
deterministic async lifecycle method; `await close()` remains an alias.
The common Python SDK contract and installed-wheel tests are included in the
adoption manifest. Operational qualification of each final wheel is separate.

## Typed results and examples

Results remain dictionaries. The public stubs describe `TestResult`, `ListResult`,
`ObjectInfo`, `TransferResult` and `DeleteResult` from `plenora_storage.types`.
Nullable metadata keys are present with `None` when the provider has no value.
The SDK uses `publication_policy="atomic_required"` or `"best_effort"`; these
Python values use underscores, unlike the CLI flag values.

```python
from plenora_storage import Engine, Connection
from plenora_storage.types import TransferResult

def upload(engine: Engine, connection: Connection) -> str:
    result: TransferResult = engine.put(
        connection, "report.csv", "report.csv", overwrite=False,
        publication_policy="atomic_required", timeout_ms=5000,
    )
    return result["checksum"]["value"]
```

The [local example](examples/local_roundtrip.py) exercises sync upload and async
download in a temporary directory and needs no account. Run it with
`python crates/plenora-storage-py/examples/local_roundtrip.py` from the repository
after installing the wheel. The CI matrix runs this example, installed-wheel
tests and a strict mypy consumer on Python 3.10–3.14 on Linux and Windows.
The consumer also checks that invalid policy values, unknown controls and wrong
result types are rejected. Dynamic provider configuration and capability
extensions remain open mappings.

## Development

Build locally with `maturin build --locked --manifest-path
crates/plenora-storage-py/Cargo.toml`. Select providers with `--no-default-features
--features local,s3`. Install and test the resulting wheel using
`python -m unittest discover -s crates/plenora-storage-py/python/tests`.
