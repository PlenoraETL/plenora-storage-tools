"""Cross-field checks for existing public contract requirements.

Call after structural schema validation. These checks do not redefine which
JSON documents satisfy the immutable schemas.
"""

from pathlib import Path
from typing import Any, Iterable

from json_values import same_value


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
        errors.extend(surface_side_effect_errors(operation))
    return errors


EFFECT_ORDER = {"none": 0, "local": 1, "remote": 2}
SURFACE_EFFECTS_KEY = "plenora.surface_side_effects"


def surface_side_effect_errors(operation: dict[str, Any]) -> list[str]:
    """SB-001: the effect a surface adds is an object of listed surfaces to
    `local` or `remote`, each stricter than the operation's `side_effect`."""
    attributes = operation.get("attributes")
    if not isinstance(attributes, dict):
        return []
    errors = []
    for key in attributes:
        if key.startswith("plenora.") and key != SURFACE_EFFECTS_KEY:
            errors.append("CAP-013: attribute key reserved for shared contracts")
    if SURFACE_EFFECTS_KEY not in attributes:
        return errors
    effects = attributes[SURFACE_EFFECTS_KEY]
    if not isinstance(effects, dict) or not effects:
        return errors + ["SB-001: surface side effects are not a non-empty object"]
    floor = EFFECT_ORDER.get(operation["side_effect"], 0)
    for surface, effect in effects.items():
        if surface not in operation["surfaces"]:
            errors.append("SB-001: surface side effect for a surface the operation does not list")
        if effect not in ("local", "remote"):
            errors.append("SB-001: surface side effect is not local or remote")
        elif EFFECT_ORDER[effect] <= floor:
            errors.append("SB-001: surface side effect is not stricter than the operation's")
    return errors


def adoption_errors(document: dict[str, Any]) -> list[str]:
    errors = []
    artifacts = {}
    for artifact in document["artifacts"]:
        previous = artifacts.get(artifact["name"])
        identity = {key: value for key, value in artifact.items() if key != "verification"}
        # VALUE: two declarations of one artifact in one manifest.
        if previous is not None and not same_value(identity, {
            key: value for key, value in previous.items() if key != "verification"
        }):
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
