"""JSON control documents must have unique member names at every depth."""
import json


class DuplicateJsonMember(ValueError):
    """A redacted parse failure, never containing a document key or value."""


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise DuplicateJsonMember('JSON document contains duplicate members')
        result[key] = value
    return result


def loads(document):
    """Parse before schema validation, rejecting duplicates including in arrays."""
    return json.loads(document, object_pairs_hook=_unique_object)


def load(stream):
    """Read and parse a JSON control document."""
    return loads(stream.read())
