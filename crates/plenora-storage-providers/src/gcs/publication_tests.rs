use super::*;
use tokio::io::AsyncWriteExt;

#[tokio::test]
async fn multipart_boundary_scanner_checks_across_chunk_boundaries_and_rewinds() {
    let marker = b"boundary-marker";
    let mut file = tokio::fs::File::from_std(tempfile::tempfile().unwrap());
    file.write_all(&vec![0; 64 * 1024 + marker.len() - 3])
        .await
        .unwrap();
    file.write_all(marker).await.unwrap();
    file.write_all(b"tail").await.unwrap();
    file.flush().await.unwrap();
    assert!(contains_boundary(&mut file, marker).await.unwrap());
    assert!(
        !contains_boundary(&mut file, b"absent-boundary")
            .await
            .unwrap()
    );
    assert!(contains_boundary(&mut file, marker).await.unwrap());
}
