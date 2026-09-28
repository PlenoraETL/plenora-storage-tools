//! Bounded transfer, listing and publication primitives.

use super::{
    Attribute, AttributeValue, Attributes, CLEANUP_BUDGET, Cow, Digest, IntegrityMetadata,
    ObjectMeta, ObjectMetadata, PutRequest, Sha256, StorageError, StorageResult, WriteMultipart,
    unrepresentable_key_error, validate_object_key,
};

pub fn put_attributes(request: &PutRequest) -> Attributes {
    let mut attributes = Attributes::new();
    if let Some(content_type) = &request.content_type {
        attributes.insert(
            Attribute::ContentType,
            AttributeValue::from(content_type.clone()),
        );
    }
    for (key, value) in &request.metadata {
        attributes.insert(
            Attribute::Metadata(Cow::Owned(key.clone())),
            AttributeValue::from(value.clone()),
        );
    }
    attributes
}

/// Publishes provider metadata only when the backend key is a valid public key.
///
/// A bucket can hold names that the public contract cannot express; emitting one
/// would produce an invalid `storage.list` output and a cursor the next page
/// rejects.
pub fn public_metadata(metadata: ObjectMeta) -> StorageResult<ObjectMetadata> {
    let key = metadata.location.to_string();
    validate_object_key(&key).map_err(|_| unrepresentable_key_error())?;
    Ok(ObjectMetadata {
        key,
        size: metadata.size,
        last_modified: Some(metadata.last_modified.to_rfc3339()),
        etag: metadata.e_tag,
        version: metadata.version,
    })
}

pub fn sha256_metadata(digest: Sha256) -> IntegrityMetadata {
    IntegrityMetadata {
        algorithm: "sha256".to_owned(),
        value: hex::encode(digest.finalize()),
    }
}

/// Aborts a started multipart upload and states the outcome the abort actually
/// achieved, instead of assuming the uploaded parts were removed.
///
/// The original cause is preserved either way: replacing it with a generic
/// cleanup error would hide why the upload failed.
pub async fn abort_multipart(writer: WriteMultipart, error: StorageError) -> StorageError {
    match tokio::time::timeout(CLEANUP_BUDGET, writer.abort()).await {
        Ok(Ok(())) => error.rolled_back(),
        _ => error.cleanup_unconfirmed("multipart_abort_failed"),
    }
}
