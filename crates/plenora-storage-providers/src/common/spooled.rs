//! Shared opt-in disk preparation, with complete validation before mutation.

use super::{
    AsyncRead, CopyRequest, ErrorPhase, ObjectMetadata, OperationContext, Provider,
    ProviderConnection, ProviderFactory, PutRequest, StorageResult, TransferResult, limit_error,
    metadata, spool::Spool, transfer,
};

impl<F: ProviderFactory> Provider<F> {
    pub(super) async fn put_prepared(
        &self,
        connection: &ProviderConnection,
        request: &PutRequest,
        source: &mut (dyn AsyncRead + Send + Unpin),
        context: &OperationContext<'_>,
    ) -> StorageResult<TransferResult> {
        let prepared = Spool::read(
            source,
            request.content_length,
            context,
            F::SPOOLED_MAX_BYTES,
        )
        .await?;
        let mut backend = self.connect(connection, context).await?;
        context
            .control
            .run(
                backend.put_file(request, prepared.file, prepared.size),
                ErrorPhase::Commit,
                true,
            )
            .await?;
        let mut result = transfer(&request.key, prepared.size, prepared.digest);
        result
            .artifact
            .content_type
            .clone_from(&request.content_type);
        Ok(result)
    }

    pub(super) async fn copy_prepared(
        &self,
        connection: &ProviderConnection,
        request: &CopyRequest,
        put: &PutRequest,
        context: &OperationContext<'_>,
    ) -> StorageResult<ObjectMetadata> {
        let mut backend = self.connect(connection, context).await?;
        let (meta, mut reader) = context
            .control
            .run(backend.get(&request.source_key), ErrorPhase::Read, false)
            .await?;
        let limit = context.policy.max_transfer_bytes.min(F::SPOOLED_MAX_BYTES);
        if meta.size > limit {
            return Err(limit_error());
        }
        let mut prepared = context
            .control
            .run(Spool::new(), ErrorPhase::Prepare, false)
            .await?;
        while let Some(chunk) = context
            .control
            .run(reader.next(), ErrorPhase::Read, false)
            .await?
        {
            context
                .control
                .run(prepared.append(&chunk, limit), ErrorPhase::Prepare, false)
                .await?;
        }
        context
            .control
            .run(reader.close(), ErrorPhase::Cleanup, false)
            .await?;
        context
            .control
            .run(prepared.finish(Some(meta.size)), ErrorPhase::Prepare, false)
            .await?;
        context
            .control
            .run(
                backend.put_file(put, prepared.file, meta.size),
                ErrorPhase::Commit,
                true,
            )
            .await?;
        Ok(metadata(&request.destination_key, meta.size))
    }
}
