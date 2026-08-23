use std::collections::BTreeMap;

use crate::{StorageError, StorageResult};

/// Secret material resolved inside the process. It deliberately has no
/// `Debug`, `Display`, `Serialize` or `Clone` implementation.
pub struct CredentialMaterial {
    fields: BTreeMap<String, String>,
}

impl CredentialMaterial {
    #[must_use]
    pub fn new(fields: BTreeMap<String, String>) -> Self {
        Self { fields }
    }

    pub fn required(&self, name: &str) -> StorageResult<&str> {
        self.fields.get(name).map(String::as_str).ok_or_else(|| {
            StorageError::invalid_configuration(
                "CREDENTIAL_FIELD_MISSING",
                format!("resolved credential lacks required field '{name}'"),
            )
        })
    }

    #[must_use]
    pub fn optional(&self, name: &str) -> Option<&str> {
        self.fields.get(name).map(String::as_str)
    }
}

pub trait CredentialResolver: Send + Sync {
    fn resolve(&self, reference: &str) -> StorageResult<CredentialMaterial>;
}

/// Resolves `env:VARIABLE_NAME` references. The variable value is a JSON
/// object of provider-specific secret fields.
#[derive(Clone, Copy, Debug, Default)]
pub struct EnvironmentCredentialResolver;

impl CredentialResolver for EnvironmentCredentialResolver {
    fn resolve(&self, reference: &str) -> StorageResult<CredentialMaterial> {
        let variable = reference.strip_prefix("env:").ok_or_else(|| {
            StorageError::invalid_configuration(
                "CREDENTIAL_REFERENCE_UNSUPPORTED",
                "the CLI credential reference must use the env: scheme",
            )
        })?;
        if variable.is_empty()
            || !variable
                .bytes()
                .all(|byte| byte.is_ascii_uppercase() || byte.is_ascii_digit() || byte == b'_')
        {
            return Err(StorageError::invalid_configuration(
                "CREDENTIAL_REFERENCE_INVALID",
                "credential environment variable name is invalid",
            ));
        }
        let encoded = std::env::var(variable).map_err(|_| {
            StorageError::invalid_configuration(
                "CREDENTIAL_UNAVAILABLE",
                "credential reference could not be resolved",
            )
        })?;
        let fields = serde_json::from_str::<BTreeMap<String, String>>(&encoded).map_err(|_| {
            StorageError::invalid_configuration(
                "CREDENTIAL_INVALID",
                "resolved credential does not have the required JSON object shape",
            )
        })?;
        Ok(CredentialMaterial::new(fields))
    }
}
