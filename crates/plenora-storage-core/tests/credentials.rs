//! Resolver failures must preserve the public error axes without exposing secrets.
use plenora_storage_core::{
    CredentialResolver, EnvironmentCredentialResolver, ErrorCategory, ErrorPhase, RemoteEffect,
    RetryDisposition,
};
use std::process::Command;

#[allow(
    clippy::manual_assert_eq,
    reason = "assert_eq! would print the credential value if the comparison failed"
)]
#[test]
fn credential_resolution_child() {
    let Ok(case) = std::env::var("PLENORA_CREDENTIAL_TEST_CASE") else {
        return;
    };
    let (reference, expected) = match case.as_str() {
        "scheme" => (
            "secret:private-endpoint",
            "CREDENTIAL_REFERENCE_UNSUPPORTED",
        ),
        "empty" => ("env:", "CREDENTIAL_REFERENCE_INVALID"),
        "name" => ("env:private-endpoint", "CREDENTIAL_REFERENCE_INVALID"),
        "missing" => (
            "env:PLENORA_CREDENTIAL_TEST_VALUE",
            "CREDENTIAL_UNAVAILABLE",
        ),
        "json" | "shape" => ("env:PLENORA_CREDENTIAL_TEST_VALUE", "CREDENTIAL_INVALID"),
        "valid" => ("env:PLENORA_CREDENTIAL_TEST_VALUE", ""),
        _ => panic!("unknown credential test case"),
    };
    let result = EnvironmentCredentialResolver.resolve(reference);
    if expected.is_empty() {
        let material = result.expect("valid credential object");
        assert!(material.required("password").unwrap() == "private-secret");
        assert!(material.optional("password").is_some());
        assert!(material.optional("absent").is_none());
        let error = material.required("absent").unwrap_err();
        assert_eq!(error.code, "CREDENTIAL_FIELD_MISSING");
        assert!(
            !serde_json::to_string(&error)
                .unwrap()
                .contains("private-secret")
        );
    } else {
        let error = result.err().expect("invalid credential must fail");
        assert_eq!(error.code, expected);
        assert_eq!(error.category, ErrorCategory::InvalidConfiguration);
        assert_eq!(error.phase, ErrorPhase::Validate);
        assert_eq!(error.remote_effect, RemoteEffect::None);
        assert_eq!(error.retry, RetryDisposition::Never);
        let public = serde_json::to_string(&error).unwrap();
        for sensitive in [
            "private-secret",
            "private-endpoint",
            "PLENORA_CREDENTIAL_TEST_VALUE",
        ] {
            assert!(!public.contains(sensitive));
        }
    }
}

#[test]
fn environment_credentials_are_validated_in_isolated_processes() {
    // Child environments avoid unsafe process-wide mutation in concurrent tests.
    for (case, value) in [
        ("scheme", None),
        ("empty", None),
        ("name", None),
        ("missing", None),
        ("json", Some("private-secret")),
        ("shape", Some(r#"{"password":["private-secret"]}"#)),
        ("valid", Some(r#"{"password":"private-secret"}"#)),
    ] {
        let mut child = Command::new(std::env::current_exe().unwrap());
        child
            .args(["--exact", "credential_resolution_child", "--nocapture"])
            .env("PLENORA_CREDENTIAL_TEST_CASE", case)
            .env_remove("PLENORA_CREDENTIAL_TEST_VALUE");
        if let Some(value) = value {
            child.env("PLENORA_CREDENTIAL_TEST_VALUE", value);
        }
        let output = child.output().unwrap();
        assert!(
            output.status.success(),
            "credential case {case} failed: {}",
            String::from_utf8_lossy(&output.stderr)
        );
    }
}
