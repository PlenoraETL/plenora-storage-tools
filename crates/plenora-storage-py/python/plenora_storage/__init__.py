"""Storage operations backed by the shared Rust engine.

Transfers use filesystem paths and stream in Rust. Connection documents contain
credential references; a host callback or the environment resolves the secrets.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, fields
import json
import os
import re
from typing import Any, Callable, Mapping

from . import _native
from ._native import CancellationToken

# Cargo uses SemVer prereleases; wheel metadata uses the equivalent PEP 440 form.
__version__ = re.sub(r"-(alpha|beta|rc)\.(\d+)$",
                     lambda match: {"alpha": "a", "beta": "b", "rc": "rc"}[match[1]] + match[2],
                     _native.__version__)

__all__ = ["Engine", "AsyncEngine", "EngineConfig", "Connection", "PlenoraError", "StorageError",
           "CancellationToken", "cancellation_outcome", "version", "__version__"]


def version() -> str:
    """Installed release identity, in the same format as wheel metadata."""
    return __version__


class PlenoraError(Exception):
    """Redacted Rust error, including effect and retry disposition."""

    def __init__(self, document: Mapping[str, Any]):
        self.code = document["code"]
        self.category = document["category"]
        self.phase = document["phase"]
        self.remote_effect = document["remote_effect"]
        self.retry = dict(document["retry"])
        self.provider = document.get("provider")
        self.execution_id = document.get("execution_id")
        self.details = dict(document.get("details", {}))
        self.message = document["message"]
        super().__init__(self.message)


class StorageError(PlenoraError):
    """Storage failure with the shared Plenora error axes."""


def cancellation_outcome(error: BaseException) -> dict[str, Any] | PlenoraError | None:
    """Find the settled storage outcome through asyncio cancellation wrappers.

    Python 3.10 creates new CancelledError instances linked via __context__;
    wait_for timeouts also wrap cancellation in __cause__. None means no known
    storage outcome, never proof that a remote mutation had no effect.
    """
    pending = [error]
    visited: set[int] = set()
    while pending:
        current = pending.pop()
        if id(current) in visited:
            continue
        visited.add(id(current))
        if isinstance(current, asyncio.CancelledError):
            result = getattr(current, "storage_result", None)
            if isinstance(result, dict):
                return result
            failure = getattr(current, "storage_error", None)
            if isinstance(failure, PlenoraError):
                return failure
        if current.__context__ is not None:
            pending.append(current.__context__)
        if current.__cause__ is not None:
            pending.append(current.__cause__)
    return None


def _invalid(code: str, message: str) -> StorageError:
    return StorageError({"code": code, "category": "invalid_configuration",
                         "phase": "validate", "remote_effect": "none",
                         "retry": {"kind": "never"}, "message": message})


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
        try:
            return {"provider": self.provider, "config_contract": self.config_contract,
                    "config": dict(self.config), "credential_ref": self.credential_ref}
        except Exception:
            raise _invalid("SDK_CONNECTION_INVALID", "connection configuration must be a mapping") from None


CredentialResolver = Callable[[str], Mapping[str, str]]


def _encode(value: Any) -> str:
    try:
        return json.dumps(value, allow_nan=False)
    except Exception:
        raise _invalid("SDK_INPUT_INVALID", "storage input is not a JSON-compatible document") from None


def _path(value: str | os.PathLike[str]) -> str:
    try:
        result = os.fspath(value)
        if not isinstance(result, str):
            raise TypeError()
        return result
    except Exception:
        raise _invalid("SDK_PATH_INVALID", "file path must resolve to a string") from None


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
                 credential_resolver: CredentialResolver | None = None, spool_uploads: bool = False):
        if not isinstance(spool_uploads, bool):
            raise _invalid("SDK_CONFIG_INVALID", "spool_uploads must be a boolean")
        if credential_resolver is not None and not callable(credential_resolver):
            raise _invalid("SDK_RESOLVER_INVALID", "credential_resolver must be callable")
        if config is not None and not isinstance(config, EngineConfig):
            raise _invalid("SDK_CONFIG_INVALID", "config must be an EngineConfig")
        # The public API accepts Mapping, while PyO3's BTreeMap conversion takes
        # a concrete dict. Convert inside the callback so Rust also redacts any
        # conversion exception, without resolving secrets during construction.
        resolver = None
        if credential_resolver is not None:
            resolver = lambda reference: dict(credential_resolver(reference))
        try:
            # Config fields are scalars. Deep-copying unvalidated values would
            # execute arbitrary __deepcopy__ callbacks before error redaction.
            config = config if config is not None else EngineConfig()
            try:
                document = {field.name: getattr(config, field.name) for field in fields(EngineConfig)}
            except Exception:
                raise _invalid("SDK_INPUT_INVALID", "engine configuration could not be read") from None
            self._native = _native.Engine(_encode(document), resolver, spool_uploads)
        except ValueError as error:
            raise _translate(error) from None

    @property
    def is_closed(self) -> bool:
        return self._native.is_closed

    def close(self) -> None:
        self._native.close()

    def __enter__(self) -> Engine:
        if self.is_closed:
            raise StorageError({"code": "ENGINE_CLOSED", "category": "execution",
                                "phase": "validate", "remote_effect": "none",
                                "retry": {"kind": "never"}, "message": "storage engine is closed"})
        return self

    def __exit__(self, *_args: Any) -> None:
        self.close()

    def capabilities(self) -> dict[str, Any]:
        """Python capability catalog for the providers in this wheel."""
        return json.loads(self._native.capabilities())

    def _invoke(self, operation: str, connection: Connection, request: dict[str, Any], *,
                cancellation: CancellationToken | None = None,
                timeout_ms: int | None = None) -> dict[str, Any]:
        if timeout_ms is not None and (type(timeout_ms) is not int or not 0 <= timeout_ms <= 2**64 - 1):
            raise _invalid("SDK_TIMEOUT_INVALID", "timeout_ms must be an unsigned 64-bit integer")
        if not isinstance(connection, Connection):
            raise _invalid("SDK_CONNECTION_INVALID", "connection must be a Connection")
        if cancellation is not None and not isinstance(cancellation, CancellationToken):
            raise _invalid("SDK_CANCELLATION_INVALID", "cancellation must be a CancellationToken")
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
        return self._invoke("get", connection, {"key": key, "output": _path(output),
                                                "overwrite": overwrite}, **controls)

    def put(self, connection: Connection, key: str, input: str | os.PathLike[str], *,
            overwrite: bool, publication_policy: str, content_type: str | None = None,
            **controls: Any) -> dict[str, Any]:
        return self._invoke("put", connection, {"key": key, "input": _path(input),
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

    When a task is cancelled, use ``cancellation_outcome(error)`` to retrieve
    the settled result or storage error, including through Python 3.10 and
    wait_for exception wrappers. Inspect that outcome before retrying a mutation.
    """

    def __init__(self, config: EngineConfig | None = None, *,
                 credential_resolver: CredentialResolver | None = None, spool_uploads: bool = False):
        self._engine = Engine(config, credential_resolver=credential_resolver, spool_uploads=spool_uploads)

    @property
    def is_closed(self) -> bool:
        return self._engine.is_closed

    async def close(self) -> None:
        """Compatibility alias for aclose()."""
        await self.aclose()

    async def aclose(self) -> None:
        await asyncio.to_thread(self._engine.close)

    async def __aenter__(self) -> AsyncEngine:
        self._engine.__enter__()
        return self

    async def __aexit__(self, *_args: Any) -> None:
        await self.aclose()

    def capabilities(self) -> dict[str, Any]:
        return self._engine.capabilities()

    async def _call(self, method: str, *args: Any, **kwargs: Any) -> dict[str, Any]:
        token = kwargs.setdefault("cancellation", CancellationToken())
        if token is None:
            token = kwargs["cancellation"] = CancellationToken()
        if not isinstance(token, CancellationToken):
            raise _invalid("SDK_CANCELLATION_INVALID", "cancellation must be a CancellationToken")
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
