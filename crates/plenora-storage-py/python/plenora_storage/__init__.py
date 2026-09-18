"""Storage operations backed by the shared Rust engine.

Transfers use filesystem paths and stream in Rust. Connection documents contain
credential references; a host callback or the environment resolves the secrets.
"""
from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass
import json
import os
from typing import Any, Callable, Mapping

from . import _native
from ._native import CancellationToken, __version__

__all__ = ["Engine", "AsyncEngine", "EngineConfig", "Connection", "StorageError",
           "CancellationToken", "__version__"]


class StorageError(Exception):
    """Redacted Rust error, including effect and retry disposition."""

    def __init__(self, document: Mapping[str, Any]):
        self.code = document["code"]
        self.category = document["category"]
        self.phase = document["phase"]
        self.remote_effect = document["remote_effect"]
        self.retry = dict(document["retry"])
        self.provider = document.get("provider")
        self.details = dict(document.get("details", {}))
        self.message = document["message"]
        super().__init__(self.message)


@dataclass(frozen=True)
class EngineConfig:
    allow_insecure_http: bool = False
    allow_insecure_ftp: bool = False
    allow_private_network: bool = False
    allow_unverified_ssh: bool = False
    max_transfer_bytes: int = 1_073_741_824
    max_list_items: int = 10_000
    max_buffered_put_bytes: int = 67_108_864


@dataclass(frozen=True, repr=False)
class Connection:
    provider: str
    config_contract: str
    config: Mapping[str, Any]
    credential_ref: str

    def __repr__(self) -> str:
        return "Connection(<redacted>)"

    def _document(self) -> dict[str, Any]:
        return {"provider": self.provider, "config_contract": self.config_contract,
                "config": dict(self.config), "credential_ref": self.credential_ref}


CredentialResolver = Callable[[str], Mapping[str, str]]


def _encode(value: Any) -> str:
    try:
        return json.dumps(value, allow_nan=False)
    except (TypeError, ValueError, OverflowError):
        raise ValueError("storage input is not a JSON-compatible document") from None


def _translate(error: ValueError) -> StorageError:
    try:
        return StorageError(json.loads(str(error)))
    except (ValueError, TypeError, KeyError):
        return StorageError({"code": "SDK_INTERNAL", "category": "internal",
                             "phase": "prepare", "remote_effect": "unknown",
                             "retry": {"kind": "requires_recovery"},
                             "message": "storage SDK operation failed"})


class Engine:
    """Reusable synchronous engine; operations release the Python GIL.

    Cursors belong to this engine. ``close`` rejects new operations; operations
    already in flight retain their own cancellation token and may complete.
    """

    def __init__(self, config: EngineConfig | None = None, *,
                 credential_resolver: CredentialResolver | None = None):
        if credential_resolver is not None and not callable(credential_resolver):
            raise TypeError("credential_resolver must be callable")
        try:
            self._native = _native.Engine(_encode(asdict(config or EngineConfig())), credential_resolver)
        except ValueError as error:
            raise _translate(error) from None

    @property
    def is_closed(self) -> bool:
        return self._native.is_closed

    def close(self) -> None:
        self._native.close()

    def __enter__(self) -> Engine:
        if self.is_closed:
            raise RuntimeError("storage engine is closed")
        return self

    def __exit__(self, *_args: Any) -> None:
        self.close()

    def capabilities(self) -> dict[str, Any]:
        """Native Rust capability catalog for the providers in this wheel."""
        return json.loads(self._native.capabilities())

    def _invoke(self, operation: str, connection: Connection, request: dict[str, Any], *,
                cancellation: CancellationToken | None = None,
                timeout_ms: int | None = None) -> dict[str, Any]:
        if timeout_ms is not None and (type(timeout_ms) is not int or timeout_ms < 0):
            raise ValueError("timeout_ms must be a nonnegative integer")
        try:
            result = self._native.invoke(operation, _encode(connection._document()), _encode(request),
                                         cancellation or CancellationToken(), timeout_ms)
        except ValueError as error:
            raise _translate(error) from None
        return json.loads(result)

    def test(self, connection: Connection, **controls: Any) -> dict[str, Any]:
        return self._invoke("test", connection, {}, **controls)

    def list(self, connection: Connection, *, prefix: str | None = None,
             cursor: str | None = None, max_items: int | None = None,
             **controls: Any) -> dict[str, Any]:
        return self._invoke("list", connection, {"prefix": prefix, "cursor": cursor,
                                                 "max_items": max_items}, **controls)

    def stat(self, connection: Connection, key: str, **controls: Any) -> dict[str, Any]:
        return self._invoke("stat", connection, {"key": key}, **controls)

    def get(self, connection: Connection, key: str, output: str | os.PathLike[str], *,
            overwrite: bool, **controls: Any) -> dict[str, Any]:
        """Download to a staged file, then publish atomically at ``output``."""
        return self._invoke("get", connection, {"key": key, "output": os.fspath(output),
                                                "overwrite": overwrite}, **controls)

    def put(self, connection: Connection, key: str, input: str | os.PathLike[str], *,
            overwrite: bool, publication_policy: str, content_type: str | None = None,
            **controls: Any) -> dict[str, Any]:
        return self._invoke("put", connection, {"key": key, "input": os.fspath(input),
                            "overwrite": overwrite, "publication_policy": publication_policy,
                            "content_type": content_type}, **controls)

    def copy(self, connection: Connection, source_key: str, destination_key: str, *,
             overwrite: bool, publication_policy: str, **controls: Any) -> dict[str, Any]:
        return self._invoke("copy", connection, {"source_key": source_key,
                            "destination_key": destination_key, "overwrite": overwrite,
                            "publication_policy": publication_policy}, **controls)

    def delete(self, connection: Connection, key: str, *, ignore_missing: bool,
               **controls: Any) -> dict[str, Any]:
        return self._invoke("delete", connection, {"key": key, "ignore_missing": ignore_missing}, **controls)


class AsyncEngine:
    """Asyncio facade with cooperative Rust cancellation and operation draining.

    When a task is cancelled, the raised ``CancelledError`` carries either
    ``storage_result`` or ``storage_error`` after the Rust operation settles.
    Inspect that outcome before retrying a mutation.
    """

    def __init__(self, config: EngineConfig | None = None, *,
                 credential_resolver: CredentialResolver | None = None):
        self._engine = Engine(config, credential_resolver=credential_resolver)

    @property
    def is_closed(self) -> bool:
        return self._engine.is_closed

    async def close(self) -> None:
        self._engine.close()

    async def __aenter__(self) -> AsyncEngine:
        self._engine.__enter__()
        return self

    async def __aexit__(self, *_args: Any) -> None:
        await self.close()

    def capabilities(self) -> dict[str, Any]:
        return self._engine.capabilities()

    async def _call(self, method: str, *args: Any, **kwargs: Any) -> dict[str, Any]:
        token = kwargs.setdefault("cancellation", CancellationToken())
        if token is None:
            token = kwargs["cancellation"] = CancellationToken()
        task = asyncio.create_task(asyncio.to_thread(getattr(self._engine, method), *args, **kwargs))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError as cancelled:
            token.cancel()
            while not task.done():
                try:
                    await asyncio.shield(task)
                except asyncio.CancelledError:
                    token.cancel()
                except Exception:
                    break
            try:
                cancelled.storage_result = task.result()
            except Exception as error:
                cancelled.storage_error = error
            raise cancelled

    async def test(self, connection: Connection, **controls: Any) -> dict[str, Any]:
        return await self._call("test", connection, **controls)

    async def list(self, connection: Connection, *, prefix: str | None = None,
                   cursor: str | None = None, max_items: int | None = None,
                   **controls: Any) -> dict[str, Any]:
        return await self._call("list", connection, prefix=prefix, cursor=cursor, max_items=max_items, **controls)

    async def stat(self, connection: Connection, key: str, **controls: Any) -> dict[str, Any]:
        return await self._call("stat", connection, key, **controls)

    async def get(self, connection: Connection, key: str, output: str | os.PathLike[str], *,
                  overwrite: bool, **controls: Any) -> dict[str, Any]:
        return await self._call("get", connection, key, output, overwrite=overwrite, **controls)

    async def put(self, connection: Connection, key: str, input: str | os.PathLike[str], *,
                  overwrite: bool, publication_policy: str, content_type: str | None = None,
                  **controls: Any) -> dict[str, Any]:
        return await self._call("put", connection, key, input, overwrite=overwrite,
                                publication_policy=publication_policy, content_type=content_type, **controls)

    async def copy(self, connection: Connection, source_key: str, destination_key: str, *,
                   overwrite: bool, publication_policy: str, **controls: Any) -> dict[str, Any]:
        return await self._call("copy", connection, source_key, destination_key, overwrite=overwrite,
                                publication_policy=publication_policy, **controls)

    async def delete(self, connection: Connection, key: str, *, ignore_missing: bool,
                     **controls: Any) -> dict[str, Any]:
        return await self._call("delete", connection, key, ignore_missing=ignore_missing, **controls)
