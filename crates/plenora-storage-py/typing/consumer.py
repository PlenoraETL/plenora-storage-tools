"""Static contract exercised against an installed wheel, including invalid calls."""
from pathlib import Path
from typing_extensions import assert_type
from plenora_storage import AsyncEngine, CancellationToken, Connection, Engine
from plenora_storage.types import DeleteResult, ListResult, ObjectInfo, TestResult, TransferResult


def sync(engine: Engine, connection: Connection) -> None:
    assert_type(engine.test(connection), TestResult)
    page = engine.list(connection, max_items=10, timeout_ms=1000)
    assert_type(page, ListResult)
    assert_type(page['objects'], list[ObjectInfo])
    assert_type(page['next_cursor'], str | None)
    assert_type(engine.stat(connection, 'source')['size'], int)
    assert_type(engine.get(connection, 'source', Path('download'), overwrite=False), TransferResult)
    assert_type(engine.put(connection, 'source', 'upload', overwrite=True,
                           publication_policy='atomic_required', cancellation=CancellationToken()), TransferResult)
    assert_type(engine.copy(connection, 'source', 'copy', overwrite=True,
                            publication_policy='best_effort'), ObjectInfo)
    assert_type(engine.delete(connection, 'copy', ignore_missing=True), DeleteResult)
    # Unused ignores fail strict checking if a future stub silently permits these calls.
    engine.stat(connection, 'source', timeout_ms='1000')  # type: ignore[arg-type]
    engine.stat(connection, 'source', cancellation=True)  # type: ignore[arg-type]
    engine.stat(connection, 'source', unsupported=True)  # type: ignore[call-arg]
    engine.put(connection, 'source', 'upload', overwrite=True, publication_policy='atomic-required')  # type: ignore[arg-type]
    engine.get(connection, 'source', 'download')  # type: ignore[call-arg]
    page['missing']  # type: ignore[typeddict-item]
    invalid_size: str = engine.stat(connection, 'source')['size']  # type: ignore[assignment]


async def asynchronous(engine: AsyncEngine, connection: Connection) -> None:
    assert_type(await engine.test(connection), TestResult)
    assert_type(await engine.list(connection), ListResult)
    assert_type(await engine.stat(connection, 'source'), ObjectInfo)
    assert_type(await engine.get(connection, 'source', 'download', overwrite=False), TransferResult)
    assert_type(await engine.put(connection, 'source', 'upload', overwrite=True,
                                 publication_policy='atomic_required'), TransferResult)
    assert_type(await engine.copy(connection, 'source', 'copy', overwrite=False,
                                  publication_policy='atomic_required'), ObjectInfo)
    assert_type(await engine.delete(connection, 'copy', ignore_missing=True), DeleteResult)
    await engine.stat(connection, 'source', timeout_ms='1000')  # type: ignore[arg-type]
    await engine.aclose()
