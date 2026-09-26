# Plenora Python SDK Contract 1.0

Status: normative

Contract identifier: `plenora-python-sdk-v1`

This document is normative. The key words MUST, MUST NOT, SHOULD, SHOULD NOT and
MAY describe requirement strength.

## 1. Applicability

This contract applies when a component publishes a Python SDK. A Rust crate,
service or CLI without a public Python package may declare the SDK contract not
applicable.

The contract governs public behavior, not implementation language. A component
MAY use Rust, PyO3 and Maturin, but consumers MUST NOT depend on that choice.

## 2. Package identity

The distribution name MUST be `plenora-<domain>` and the import package MUST be
`plenora_<domain>`, using a stable lowercase domain identifier.

The package MUST support Python 3.10 or newer. It MAY support newer versions and
MAY drop an older version only in a component major release.

The SDK MUST expose `version()` returning the same version as
`importlib.metadata.version(<distribution>)`. Native package metadata, Python
package metadata and the built wheel filename MUST agree.

## 3. Typing and public surface

The package MUST ship PEP 561 typing information through inline annotations or
stubs plus `py.typed`.

Public names MUST be intentionally exported. Internal native modules and helper
types MUST NOT become part of the public API merely because they are importable.

Return values used for machine decisions MUST be structured types, not strings
that consumers need to parse.

For tabular inputs and outputs, an SDK SHOULD use PyArrow objects or Arrow IPC at
the public boundary. Non-tabular SDKs are not required to depend on Arrow.

## 4. Sync and async parity

If an I/O operation has both synchronous and asynchronous entry points, they
MUST have equivalent parameters, defaults, validation, results, errors,
security behavior and capability checks.

An SDK MAY expose only sync or only async operation where the component records
that choice in the `api_modes` field of each Python artifact in adoption
manifest v4. It MUST NOT expose a nominal async method that performs blocking
I/O on the event-loop thread.

## 5. Lifecycle

Long-lived resources MUST expose deterministic lifecycle methods:

- synchronous resources: `close()`, `__enter__`, `__exit__`;
- asynchronous resources: `aclose()`, `__aenter__`, `__aexit__`.

Close operations MUST be idempotent. Operations after close MUST fail with a
typed error. Correctness MUST NOT rely on garbage collection or destructors.

When an operation leaves remote outcome or connection state ambiguous, the
affected session or resource MUST be quarantined and MUST NOT return to a pool.

## 6. Errors

The SDK MUST expose a public root exception named `PlenoraError`. Components MAY
add typed subclasses.

Every public exception MUST make these fields available without parsing text:

- `category`;
- `phase`;
- `remote_effect`;
- `retry`;
- `message`.

The values and semantics MUST match `schemas/error-v1.schema.json`. Additional
context MAY be exposed in a structured `details` or domain-specific attribute.

Equivalent sync and async failures MUST map to the same public exception class
and axes. Native implementation exceptions MUST NOT leak through the public
boundary.

Messages and representations MUST redact credentials, tokens, connection
strings, authorization headers, bound statements and source payloads.

## 7. Capabilities and fail-closed behavior

The SDK MUST provide structured capability discovery before a consumer needs to
attempt a destructive operation.

Capability discovery MUST expose a value semantically equivalent to
`schemas/capabilities-v2.schema.json`. Python-native typed objects MAY wrap the
document, but operation identifiers, versions, surfaces, contracts, status and
controls MUST remain available without parsing text.

Unsupported or unqualified behavior MUST fail closed with category
`unsupported` or a more precise validation category. An argument MUST NOT be
silently ignored or emulated with different semantics.

Where products or providers have different semantics, provider selection MUST
be explicit. Connection-time product detection MAY reject a mismatch but MUST
NOT silently select a different public provider.

## 8. Cancellation, deadlines and budgets

Operations that can block SHOULD accept the component's standard cancellation,
deadline and resource-budget controls. Controls offered by sync and async APIs
MUST be semantically equivalent.

Cancellation and timeout do not prove that a remote write was rolled back. The
exception MUST preserve the actual or conservative `remote_effect` and `retry`
axes.

## 9. Security

Network clients MUST verify server identity by default. Insecure modes MUST be
explicitly named, opt-in and documented as development-only.

Secret values MUST NOT appear in `repr`, `str(exception)`, logs, tracebacks or
serialized diagnostic payloads.

Objects that store secrets SHOULD use redacted representations and SHOULD avoid
copying secrets into immutable strings when a byte-oriented API is practical.

## 10. Packaging and artifact verification

A released wheel MUST be tested after installation in an environment that
cannot import the source checkout by accident.

The verification MUST confirm:

- import succeeds from the installed distribution;
- `version()` matches installed metadata;
- the wheel and any native module belong to the intended release;
- all declared public typing artifacts are present.

Builds SHOULD use locked dependencies. Every distributed platform artifact MUST
be verified on its target platform before release.

## 11. Compatibility

Components follow semantic versioning for the public Python package.

The following require a component major release:

- removing or renaming a public symbol;
- making an optional parameter required;
- changing a default with security or behavioral impact;
- changing a return type or error classification incompatibly;
- dropping a supported Python version.

Additive optional parameters and new exception subclasses are compatible only
when existing callers retain the same behavior.

## 12. Operation identity across surfaces

Every public SDK method that invokes domain functionality MUST map to an
operation identifier advertised by capability discovery. Python method naming
MAY be idiomatic, but validation, defaults, output meaning, error axes and side
effects MUST remain equivalent to the same operation version on other public
surfaces.
