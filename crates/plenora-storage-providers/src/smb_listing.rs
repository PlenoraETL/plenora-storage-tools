//! Bounded SMB directory enumeration, without collecting a whole remote directory.
use crate::{
    common::{failure, invalid, limit_error, metadata, select},
    local::portable_key,
};
use plenora_storage_core::{
    ErrorCategory, ErrorPhase, ObjectMetadata, ProviderListRequest, StorageResult,
    directory_may_contain,
};
use smb2::{
    Tree,
    client::connection::Connection,
    msg::{
        create::{
            CreateDisposition, CreateRequest, CreateResponse, ImpersonationLevel, ShareAccess,
        },
        query_directory::{
            FileInformationClass, QueryDirectoryFlags, QueryDirectoryRequest,
            QueryDirectoryResponse,
        },
    },
    pack::{ReadCursor, Unpack},
    types::{Command, OplockLevel, flags::FileAccessMask, status::NtStatus},
};
use std::collections::BTreeMap;

// The traversal owns the page, pending directories and global scan budget.
#[allow(clippy::too_many_arguments)]
pub async fn directory(
    conn: &mut Connection,
    tree: &Tree,
    path: &str,
    parent: &str,
    request: &ProviderListRequest,
    limit: usize,
    selected: &mut BTreeMap<String, ObjectMetadata>,
    stack: &mut Vec<String>,
    scanned: &mut usize,
) -> StorageResult<()> {
    let create = CreateRequest {
        requested_oplock_level: OplockLevel::None,
        impersonation_level: ImpersonationLevel::Impersonation,
        desired_access: FileAccessMask::new(
            FileAccessMask::FILE_READ_DATA | FileAccessMask::FILE_READ_ATTRIBUTES,
        ),
        file_attributes: 0,
        share_access: ShareAccess(7),
        create_disposition: CreateDisposition::FileOpen,
        create_options: 1,
        name: smb2::encode_path(path),
        create_contexts: vec![],
    };
    let frame = conn
        .execute(Command::Create, &create, Some(tree.tree_id))
        .await
        .map_err(protocol)?;
    check(frame.header.status)?;
    let id = CreateResponse::unpack(&mut ReadCursor::new(&frame.body))
        .map_err(protocol)?
        .file_id;
    let result = async {
        let mut first = true;
        loop {
            let query = QueryDirectoryRequest {
                file_information_class: FileInformationClass::FileDirectoryInformation,
                flags: QueryDirectoryFlags(if first {
                    QueryDirectoryFlags::RESTART_SCANS
                } else {
                    0
                }),
                file_index: 0,
                file_id: id,
                output_buffer_length: 65536,
                file_name: "*".to_owned(),
            };
            first = false;
            let frame = conn
                .execute(Command::QueryDirectory, &query, Some(tree.tree_id))
                .await
                .map_err(protocol)?;
            if frame.header.status == NtStatus::NO_MORE_FILES {
                break;
            }
            check(frame.header.status)?;
            let response = QueryDirectoryResponse::unpack(&mut ReadCursor::new(&frame.body))
                .map_err(protocol)?;
            if response.output_buffer.len() > 65536 || response.output_buffer.is_empty() {
                return Err(invalid("SMB_DIRECTORY_PAGE_INVALID"));
            }
            parse_page(
                &response.output_buffer,
                parent,
                request,
                limit,
                selected,
                stack,
                scanned,
            )?;
        }
        Ok(())
    }
    .await;
    let close = tree.close_handle(conn, id).await.map_err(protocol);
    result.and(close)
}
fn parse_page(
    data: &[u8],
    parent: &str,
    request: &ProviderListRequest,
    limit: usize,
    selected: &mut BTreeMap<String, ObjectMetadata>,
    stack: &mut Vec<String>,
    scanned: &mut usize,
) -> StorageResult<()> {
    let mut remaining = data;
    loop {
        *scanned += 1;
        if *scanned > 100_000 {
            return Err(limit_error());
        }
        let mut cursor = ReadCursor::new(remaining);
        let next =
            usize::try_from(cursor.read_u32_le().map_err(protocol)?).map_err(|_| limit_error())?;
        cursor.skip(36).map_err(protocol)?;
        let size = cursor.read_u64_le().map_err(protocol)?;
        cursor.skip(8).map_err(protocol)?;
        let attributes = cursor.read_u32_le().map_err(protocol)?;
        let length =
            usize::try_from(cursor.read_u32_le().map_err(protocol)?).map_err(|_| limit_error())?;
        if length == 0 || length % 2 != 0 || length > 8192 || (next != 0 && next < 64 + length) {
            return Err(invalid("SMB_DIRECTORY_PAGE_INVALID"));
        }
        let name = cursor.read_utf16_le(length).map_err(protocol)?;
        if !matches!(name.as_str(), "." | "..") {
            // Reparse points can escape the selected tree or introduce cycles.
            if name.contains('/') || attributes & 0x400 != 0 {
                return Err(invalid("SMB_REPARSE_OR_NAME_FORBIDDEN"));
            }
            let key = if parent.is_empty() {
                name
            } else {
                format!("{parent}/{name}")
            };
            portable_key(&key)?;
            if attributes & 0x10 != 0 {
                if directory_may_contain(&key, request.prefix.as_deref().unwrap_or_default()) {
                    stack.push(key);
                }
            } else {
                select(selected, metadata(&key, size), request, limit)?;
            }
        }
        if next == 0 {
            break;
        }
        remaining = remaining
            .get(next..)
            .ok_or_else(|| invalid("SMB_DIRECTORY_PAGE_INVALID"))?;
    }
    Ok(())
}
fn protocol(_: smb2::Error) -> plenora_storage_core::StorageError {
    failure(ErrorCategory::Protocol, ErrorPhase::Read, false)
}
fn check(status: NtStatus) -> StorageResult<()> {
    if status == NtStatus::SUCCESS {
        Ok(())
    } else {
        Err(failure(ErrorCategory::Protocol, ErrorPhase::Read, false))
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn malformed_directory_offsets_and_names_are_rejected() {
        for data in [vec![], vec![0; 63], vec![255; 128]] {
            assert!(
                parse_page(
                    &data,
                    "",
                    &ProviderListRequest::default(),
                    10,
                    &mut BTreeMap::new(),
                    &mut Vec::new(),
                    &mut 0
                )
                .is_err()
            );
        }
    }
}
