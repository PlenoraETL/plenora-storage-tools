//! Private disk preparation validates the complete input before publication.

use super::{Digest, ErrorPhase, Sha256, StorageResult, invalid, io_error, limit_error};
use plenora_storage_core::OperationContext;
use tokio::{
    fs::File,
    io::{AsyncRead, AsyncReadExt, AsyncSeekExt, AsyncWriteExt},
};

/// Owns an unnamed temporary file; dropping it also drops its private storage.
pub(super) struct Spool {
    pub(super) file: File,
    pub(super) size: u64,
    pub(super) digest: Sha256,
}

impl Spool {
    pub(super) async fn new() -> StorageResult<Self> {
        // tempfile uses exclusive creation and owner-only permissions. No path
        // enters an error, callback, connection or provider request.
        let file = tokio::task::spawn_blocking(tempfile::tempfile)
            .await
            .map_err(|_| invalid("UPLOAD_PREPARATION_FAILED"))?
            .map_err(|error| io_error(&error, ErrorPhase::Prepare, false))?;
        Ok(Self {
            file: File::from_std(file),
            size: 0,
            digest: Sha256::new(),
        })
    }

    pub(super) async fn append(&mut self, bytes: &[u8], limit: u64) -> StorageResult<()> {
        let size = self
            .size
            .checked_add(bytes.len() as u64)
            .ok_or_else(limit_error)?;
        if size > limit {
            return Err(limit_error());
        }
        self.file
            .write_all(bytes)
            .await
            .map_err(|error| io_error(&error, ErrorPhase::Prepare, false))?;
        self.size = size;
        self.digest.update(bytes);
        Ok(())
    }

    pub(super) async fn finish(&mut self, expected: Option<u64>) -> StorageResult<()> {
        if expected.is_some_and(|size| size != self.size) {
            return Err(invalid("CONTENT_LENGTH_MISMATCH"));
        }
        self.file
            .flush()
            .await
            .map_err(|error| io_error(&error, ErrorPhase::Prepare, false))?;
        self.file
            .rewind()
            .await
            .map_err(|error| io_error(&error, ErrorPhase::Prepare, false))?;
        Ok(())
    }

    pub(super) async fn read(
        source: &mut (dyn AsyncRead + Send + Unpin),
        expected: Option<u64>,
        context: &OperationContext<'_>,
        protocol_limit: u64,
    ) -> StorageResult<Self> {
        let limit = context.policy.max_transfer_bytes.min(protocol_limit);
        if expected.is_some_and(|size| size > limit) {
            return Err(limit_error());
        }
        let mut spool = context
            .control
            .run(Self::new(), ErrorPhase::Prepare, false)
            .await?;
        let mut buffer = vec![0; 64 * 1024];
        loop {
            let count = context
                .control
                .run(
                    async {
                        source
                            .read(&mut buffer)
                            .await
                            .map_err(|error| io_error(&error, ErrorPhase::Read, false))
                    },
                    ErrorPhase::Read,
                    false,
                )
                .await?;
            if count == 0 {
                break;
            }
            context
                .control
                .run(
                    spool.append(&buffer[..count], limit),
                    ErrorPhase::Prepare,
                    false,
                )
                .await?;
        }
        context
            .control
            .run(spool.finish(expected), ErrorPhase::Prepare, false)
            .await?;
        Ok(spool)
    }
}

#[cfg(test)]
#[path = "spool_tests.rs"]
mod tests;
