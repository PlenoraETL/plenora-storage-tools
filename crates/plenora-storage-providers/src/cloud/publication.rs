//! Upload and copy publication, including cleanup and ambiguous commit outcomes.

use super::{
    Attribute, AttributeValue, Attributes, Bytes, Cloud, ObjectStore, PutMode, PutOptions,
    PutRequest, StorageResult, path, store_error,
};

pub async fn put(provider: &Cloud, request: &PutRequest, data: Bytes) -> StorageResult<()> {
    let mut attributes = Attributes::new();
    if let Some(value) = &request.content_type {
        attributes.insert(Attribute::ContentType, AttributeValue::from(value.clone()));
    }
    for (key, value) in &request.metadata {
        attributes.insert(
            Attribute::Metadata(key.clone().into()),
            AttributeValue::from(value.clone()),
        );
    }
    let options = PutOptions {
        mode: if request.overwrite {
            PutMode::Overwrite
        } else {
            PutMode::Create
        },
        attributes,
        ..PutOptions::default()
    };
    provider
        .store
        .put_opts(&path(&request.key)?, data.into(), options)
        .await
        .map_err(|error| store_error(error, true))?;
    Ok(())
}
