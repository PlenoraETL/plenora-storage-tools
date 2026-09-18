"""Cross-field checks for existing public contract requirements.

Call after structural schema validation. These checks do not redefine which
JSON documents satisfy the immutable schemas.
"""

from pathlib import Path
from typing import Any, Iterable


def capability_errors(document: dict[str, Any]) -> list[str]:
    errors = []
    interfaces = {item["kind"] for item in document["interfaces"]}
    identities = set()
    for operation in document["operations"]:
        identity = (operation["id"], operation["version"])
        if identity in identities:
            errors.append("CAP-005: duplicate operation identity")
        identities.add(identity)
        if not set(operation["surfaces"]).issubset(interfaces):
            errors.append("CAP-007: operation surface absent from interfaces")
    return errors


def adoption_errors(document: dict[str, Any]) -> list[str]:
    errors = []
    artifacts = {}
    for artifact in document["artifacts"]:
        previous = artifacts.get(artifact["name"])
        identity = {key: value for key, value in artifact.items() if key != "verification"}
        if previous is not None and identity != {
            key: value for key, value in previous.items() if key != "verification"
        }:
            errors.append("ambiguous artifact name in adoption manifest")
        artifacts[artifact["name"]] = artifact
    contracts = {}
    for contract in document["contracts"]:
        previous = contracts.get(contract["id"])
        if previous is not None and previous != contract["status"]:
            errors.append("duplicate contract identity with conflicting adoption status")
        contracts[contract["id"]] = contract["status"]
    for deviation in document["deviations"]:
        name = deviation.get("artifact")
        if name is None:
            # A surface-only deviation may describe an entirely missing surface.
            continue
        artifact = artifacts.get(name)
        if artifact is None:
            errors.append("deviation refers to an undeclared artifact")
        elif "surface" in deviation and deviation["surface"] != artifact["surface"]:
            errors.append("deviation surface differs from the named artifact")
    return errors


def public_semantic_errors(schema_name: str, document: Any) -> list[str]:
    if schema_name == "capabilities-v2.schema.json":
        return capability_errors(document)
    if schema_name in {
        "adoption-manifest-v2.schema.json",
        "adoption-manifest-v3.schema.json",
        "adoption-manifest-v4.schema.json",
    }:
        return adoption_errors(document)
    return []


def example_inventory_errors(
    root: Path, registrations: Iterable[tuple[str, str, str]]
) -> list[str]:
    """Each example must be registered; multiple different checks are allowed."""
    errors = []
    seen = set()
    paths = set()
    for path, expectation, check in registrations:
        identity = (path, check)
        if identity in seen:
            errors.append(f"duplicate example registration: {path} ({check})")
        seen.add(identity)
        paths.add(path)
        if expectation not in {"valid", "invalid"} or not path.startswith(
            f"examples/{expectation}/"
        ):
            errors.append(f"example classification conflicts with its directory: {path}")
    actual = {path.relative_to(root).as_posix() for path in (root / "examples").rglob("*.json")}
    errors.extend(f"unregistered example: {path}" for path in sorted(actual - paths))
    errors.extend(f"registered example is missing: {path}" for path in sorted(paths - actual))
    return errors
