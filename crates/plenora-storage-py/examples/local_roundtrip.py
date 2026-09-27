"""Run a local sync/async roundtrip in a temporary directory; no account required."""
import asyncio
from pathlib import Path
from tempfile import TemporaryDirectory

from plenora_storage import AsyncEngine, Connection, Engine
from plenora_storage.types import TransferResult


async def read_async(connection: Connection, source: Path, output: Path) -> None:
    async with AsyncEngine() as engine:
        result: TransferResult = await engine.get(connection, 'example.txt', output,
                                                  overwrite=False, timeout_ms=5000)
        assert result['bytes_transferred'] == source.stat().st_size
        assert output.read_bytes() == source.read_bytes()


def main() -> None:
    with TemporaryDirectory(prefix='plenora-storage-example-') as directory:
        root = Path(directory)
        storage = root / 'storage'
        storage.mkdir()
        source = root / 'input.txt'
        source.write_text('storage example\n', encoding='utf-8')
        connection = Connection('local', 'plenora-storage-local-connection-v1',
                                {'root': str(storage)}, 'local:process')
        with Engine() as engine:
            result: TransferResult = engine.put(connection, 'example.txt', source,
                                                overwrite=False, publication_policy='atomic_required')
            assert result['bytes_transferred'] == source.stat().st_size
            assert engine.stat(connection, 'example.txt')['size'] == result['bytes_transferred']
        asyncio.run(read_async(connection, source, root / 'download.txt'))
    print('PASS local sync/async example')


if __name__ == '__main__':
    main()
