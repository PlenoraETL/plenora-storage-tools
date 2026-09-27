"""Public result shapes; values remain ordinary dictionaries at runtime.

Keys follow the storage v1 schemas. Nullable metadata is always present: None
means the provider did not supply that value, not that the object is absent.
"""
from typing import Literal, TypedDict

PublicationPolicy = Literal["best_effort", "atomic_required"]


class TestResult(TypedDict):
    provider: str
    reachable: bool


class ObjectInfo(TypedDict):
    key: str
    size: int
    last_modified: str | None
    etag: str | None
    version: str | None


class ListResult(TypedDict):
    objects: list[ObjectInfo]
    truncated: bool
    next_cursor: str | None


class Checksum(TypedDict):
    algorithm: Literal["sha256"]
    value: str


class ArtifactMetadata(TypedDict):
    content_type: str | None
    size: int | None
    sha256: str | None


class TransferResult(TypedDict):
    key: str
    bytes_transferred: int
    checksum: Checksum
    artifact: ArtifactMetadata
    etag: str | None
    version: str | None


class DeleteResult(TypedDict):
    key: str
    deleted: bool


OperationResult = TestResult | ObjectInfo | ListResult | TransferResult | DeleteResult
