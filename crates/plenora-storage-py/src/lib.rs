//! Private Python bridge; the Python package owns the public convenience API.
#![forbid(unsafe_code)]

use plenora_storage_core::{
    CancellationToken, CredentialMaterial, CredentialResolver, Engine, EngineConfig,
    EnvironmentCredentialResolver, ExecutionControl, ProviderConnection, StorageError,
    StorageResult,
};
use plenora_storage_engine::{PutFileOptions, build_engine, get_to_file, put_from_file};
use pyo3::{exceptions::PyValueError, prelude::*};
use serde::{Deserialize, de::DeserializeOwned};
use std::{
    collections::BTreeMap,
    path::PathBuf,
    sync::Arc,
    time::{Duration, Instant},
};

struct Credentials(Option<Py<PyAny>>);
impl CredentialResolver for Credentials {
    fn resolve(&self, reference: &str) -> StorageResult<CredentialMaterial> {
        let Some(callback) = &self.0 else {
            return EnvironmentCredentialResolver.resolve(reference);
        };
        Python::attach(|py| {
            callback
                .call1(py, (reference,))
                .and_then(|value| value.extract::<BTreeMap<String, String>>(py))
                .map(CredentialMaterial::new)
                .map_err(|_| invalid("CREDENTIAL_RESOLVER_FAILED", "credential resolver failed"))
        })
    }
}

#[pyclass(name = "CancellationToken", module = "plenora_storage._native", frozen)]
#[derive(Default)]
struct Token(CancellationToken);
#[pymethods]
impl Token {
    #[new]
    fn new() -> Self {
        Self::default()
    }
    fn cancel(&self) {
        self.0.cancel();
    }
    #[getter]
    fn is_cancelled(&self) -> bool {
        self.0.is_cancelled()
    }
}

#[pyclass(name = "Engine", module = "plenora_storage._native", frozen)]
struct NativeEngine {
    engine: Engine,
    runtime: tokio::runtime::Runtime,
}

#[pymethods]
impl NativeEngine {
    #[new]
    #[pyo3(signature = (config, resolver=None))]
    fn new(config: &str, resolver: Option<Py<PyAny>>) -> PyResult<Self> {
        let config: EngineConfig = decode(config).map_err(python_error)?;
        let runtime = tokio::runtime::Builder::new_multi_thread()
            .worker_threads(2)
            .enable_all()
            .build()
            .map_err(|_| PyValueError::new_err("storage runtime creation failed"))?;
        let engine = build_engine(config, Arc::new(Credentials(resolver))).map_err(python_error)?;
        Ok(Self { engine, runtime })
    }

    fn close(&self) {
        self.engine.close();
    }
    #[getter]
    fn is_closed(&self) -> bool {
        self.engine.is_closed()
    }

    fn capabilities(&self) -> PyResult<String> {
        encode(&self.engine.capabilities()).map_err(python_error)
    }

    #[pyo3(signature = (operation, connection, request, token, timeout_ms=None))]
    fn invoke(
        &self,
        py: Python<'_>,
        operation: &str,
        connection: &str,
        request: &str,
        token: &Token,
        timeout_ms: Option<u64>,
    ) -> PyResult<String> {
        let connection = decode(connection).map_err(python_error)?;
        let mut control = ExecutionControl::new(token.0.clone());
        if let Some(milliseconds) = timeout_ms {
            control.deadline = Some(
                Instant::now()
                    .checked_add(Duration::from_millis(milliseconds))
                    .ok_or_else(|| {
                        python_error(invalid("DEADLINE_INVALID", "deadline is out of range"))
                    })?,
            );
        }
        py.detach(|| {
            self.runtime
                .block_on(self.execute(operation, &connection, request, &control))
        })
        .map_err(python_error)
    }
}

impl NativeEngine {
    async fn execute(
        &self,
        operation: &str,
        connection: &ProviderConnection,
        request: &str,
        control: &ExecutionControl,
    ) -> StorageResult<String> {
        control.check(plenora_storage_core::ErrorPhase::Validate, false)?;
        match operation {
            "test" => {
                let _: Empty = decode(request)?;
                encode(&self.engine.test(connection, control).await?)
            }
            "list" => encode(
                &self
                    .engine
                    .list(connection, &decode(request)?, control)
                    .await?,
            ),
            "stat" => encode(
                &self
                    .engine
                    .stat(connection, &decode(request)?, control)
                    .await?,
            ),
            "delete" => encode(
                &self
                    .engine
                    .delete(connection, &decode(request)?, control)
                    .await?,
            ),
            "copy" => encode(
                &self
                    .engine
                    .copy(connection, &decode(request)?, control)
                    .await?,
            ),
            "get" => {
                let request: Download = decode(request)?;
                encode(
                    &get_to_file(
                        &self.engine,
                        connection,
                        request.key,
                        &request.output,
                        request.overwrite,
                        control,
                    )
                    .await?,
                )
            }
            "put" => {
                let request: Upload = decode(request)?;
                encode(
                    &put_from_file(
                        &self.engine,
                        connection,
                        PutFileOptions {
                            key: request.key,
                            input: request.input,
                            overwrite: request.overwrite,
                            publication_policy: request.publication_policy,
                            content_type: request.content_type,
                        },
                        control,
                    )
                    .await?,
                )
            }
            _ => Err(StorageError::unsupported(
                "storage operation is unsupported",
            )),
        }
    }
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Empty {}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Download {
    key: String,
    output: PathBuf,
    overwrite: bool,
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Upload {
    key: String,
    input: PathBuf,
    overwrite: bool,
    publication_policy: plenora_storage_core::PublicationPolicy,
    content_type: Option<String>,
}

fn invalid(code: &'static str, message: &'static str) -> StorageError {
    StorageError::invalid_configuration(code, message)
}
fn decode<T: DeserializeOwned>(input: &str) -> StorageResult<T> {
    if input.len() > 1_048_576 {
        return Err(invalid(
            "SDK_INPUT_TOO_LARGE",
            "SDK document exceeds the 1 MiB limit",
        ));
    }
    serde_json::from_str(input)
        .map_err(|_| invalid("SDK_INPUT_INVALID", "SDK document has an invalid shape"))
}
fn encode<T: serde::Serialize>(value: &T) -> StorageResult<String> {
    serde_json::to_string(value)
        .map_err(|_| invalid("SDK_RESULT_INVALID", "SDK result serialization failed"))
}
fn python_error(error: StorageError) -> PyErr {
    PyValueError::new_err(
        serde_json::to_string(&error).unwrap_or_else(|_| "storage operation failed".to_owned()),
    )
}

#[pymodule]
fn _native(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_class::<NativeEngine>()?;
    module.add_class::<Token>()?;
    module.add("__version__", env!("CARGO_PKG_VERSION"))?;
    Ok(())
}
