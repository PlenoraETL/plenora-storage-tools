use super::transfer::{parse_listing_entry, read_listing_line};
use super::{
    FtpConnectionConfig, MAX_MLSD_LINE_BYTES, ensure_parent_directories, scan_directory,
    validate_key,
};
use plenora_storage_core::{EngineConfig, ExecutionControl, OperationContext};
use tokio::io::{AsyncBufReadExt, AsyncWriteExt, BufReader};

#[test]
fn malformed_unix_modes_are_protocol_errors_not_panics() {
    for mode in ["é7", "7é", "雪", "💥", "éé", "888", "07x5", "", "07555"] {
        for name in ["unix.mode", "UNIX.mode", "UnIx.MoDe"] {
            let line = format!("type=file;{name}={mode}; entry");
            assert!(parse_listing_entry(&line).is_err());
        }
    }
    for mode in ["755", "0755", "4755", "000"] {
        let line = format!("type=file;size=123;UNIX.mode={mode}; entry");
        let file = parse_listing_entry(&line).unwrap();
        assert_eq!(file.name(), "entry");
        assert_eq!(file.size(), 123);
    }
}

#[tokio::test]
async fn concurrent_parent_creation_requires_proof_of_a_directory() {
    for (reply, succeeds) in [
        (
            "250-listing\r\n type=dir;modify=20260926000000; parent\r\n250 end\r\n",
            true,
        ),
        (
            "250-listing\r\n type=file;size=0;modify=20260926000000; parent\r\n250 end\r\n",
            false,
        ),
        ("550 still unavailable\r\n", false),
        ("250-listing\r\n malformed\r\n250 end\r\n", false),
    ] {
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0")
            .await
            .expect("listener");
        let address = listener.local_addr().expect("address");
        let server = tokio::spawn(async move {
            let (stream, _) = listener.accept().await.expect("accept");
            let mut stream = BufReader::new(stream);
            stream
                .get_mut()
                .write_all(b"220 ready\r\n")
                .await
                .expect("greeting");
            for (expected, response) in [
                ("MLST parent\r\n", "550 missing\r\n"),
                ("MKD parent\r\n", "550 unavailable\r\n"),
                ("MLST parent\r\n", reply),
            ] {
                let mut command = String::new();
                stream.read_line(&mut command).await.expect("command");
                assert_eq!(command, expected);
                stream
                    .get_mut()
                    .write_all(response.as_bytes())
                    .await
                    .expect("response");
            }
        });
        let mut ftp = suppaftp::tokio::AsyncRustlsFtpStream::connect(address)
            .await
            .expect("connect");
        let policy = EngineConfig::default();
        let control = ExecutionControl::default()
            .with_deadline(std::time::Instant::now() + std::time::Duration::from_secs(2));
        let mut prepared = false;
        let result = ensure_parent_directories(
            &mut ftp,
            "parent/object",
            &OperationContext {
                policy: &policy,
                control: &control,
            },
            &mut prepared,
        )
        .await;
        assert_eq!(result.is_ok(), succeeds);
        assert!(prepared);
        server.await.expect("server");
    }
}

#[tokio::test]
async fn parent_probe_obeys_deadline_and_cancellation() {
    for cancel in [false, true] {
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0")
            .await
            .expect("listener");
        let address = listener.local_addr().expect("address");
        let (sent, received) = tokio::sync::oneshot::channel();
        let server = tokio::spawn(async move {
            let (stream, _) = listener.accept().await.expect("accept");
            let mut stream = BufReader::new(stream);
            stream
                .get_mut()
                .write_all(b"220 ready\r\n")
                .await
                .expect("greeting");
            let mut command = String::new();
            stream.read_line(&mut command).await.expect("MLST");
            assert_eq!(command, "MLST parent\r\n");
            sent.send(()).expect("signal");
            std::future::pending::<()>().await;
        });
        let mut ftp = suppaftp::tokio::AsyncRustlsFtpStream::connect(address)
            .await
            .expect("connect");
        let control = if cancel {
            ExecutionControl::default()
        } else {
            ExecutionControl::default()
                .with_deadline(std::time::Instant::now() + std::time::Duration::from_millis(200))
        };
        let token = control.cancellation.clone();
        let trigger = tokio::spawn(async move {
            received.await.expect("probe reached");
            if cancel {
                token.cancel();
            }
        });
        let policy = EngineConfig::default();
        let result = tokio::time::timeout(
            std::time::Duration::from_secs(2),
            ensure_parent_directories(
                &mut ftp,
                "parent/object",
                &OperationContext {
                    policy: &policy,
                    control: &control,
                },
                &mut false,
            ),
        )
        .await;
        server.abort();
        trigger.await.expect("trigger");
        let error = result.expect("bounded operation").expect_err("must stop");
        assert_eq!(error.code, if cancel { "CANCELLED" } else { "TIMEOUT" });
        assert_eq!(
            error.remote_effect,
            plenora_storage_core::RemoteEffect::None
        );
    }
}

#[test]
fn keys_cannot_escape_the_remote_root() {
    assert!(validate_key("folder/object.bin").is_ok());
    assert!(validate_key("../secret").is_err());
    assert!(validate_key("/absolute").is_err());
}

#[tokio::test]
async fn listing_line_without_a_terminator_has_a_fixed_memory_bound() {
    let mut reader = BufReader::new(tokio::io::repeat(b'x'));
    let error = read_listing_line(&mut reader)
        .await
        .expect_err("oversized line");
    assert_eq!(error.code, "LIST_SCAN_LIMIT_EXCEEDED");
    assert_eq!(MAX_MLSD_LINE_BYTES, 32 * 1_024);
}

#[tokio::test]
async fn listing_stops_at_scan_limit_without_waiting_for_directory_eof() {
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0")
        .await
        .expect("control listener");
    let address = listener.local_addr().expect("address");
    let server = tokio::spawn(async move {
        let (control, _) = listener.accept().await.expect("control");
        let mut control = BufReader::new(control);
        control
            .get_mut()
            .write_all(b"220 ready\r\n")
            .await
            .expect("greeting");
        let mut command = String::new();
        control.read_line(&mut command).await.expect("PASV");
        assert_eq!(command, "PASV\r\n");
        let data = tokio::net::TcpListener::bind("127.0.0.1:0")
            .await
            .expect("data listener");
        let port = data.local_addr().expect("data address").port();
        control
            .get_mut()
            .write_all(
                format!("227 passive (127,0,0,1,{},{})\r\n", port / 256, port % 256).as_bytes(),
            )
            .await
            .expect("passive reply");
        let (mut data, _) = data.accept().await.expect("data");
        command.clear();
        control.read_line(&mut command).await.expect("MLSD");
        assert_eq!(command, "MLSD .\r\n");
        control
            .get_mut()
            .write_all(b"150 listing\r\n")
            .await
            .expect("listing reply");
        data.write_all(b"type=file;size=1; a\r\ntype=file;size=1; b\r\ntype=file;size=1; c\r\n")
            .await
            .expect("entries");
        std::future::pending::<()>().await;
    });
    let mut ftp = suppaftp::tokio::AsyncRustlsFtpStream::connect(address)
        .await
        .expect("connect");
    let policy = EngineConfig {
        max_list_items: 2,
        ..EngineConfig::default()
    };
    let control = ExecutionControl::default()
        .with_deadline(std::time::Instant::now() + std::time::Duration::from_secs(2));
    let mut visited = Vec::new();
    let error = scan_directory(
        &mut ftp,
        ".",
        &OperationContext {
            policy: &policy,
            control: &control,
        },
        &mut 0,
        |file| {
            visited.push(file.name().to_owned());
            Ok(())
        },
    )
    .await
    .expect_err("scan bound before EOF");
    server.abort();
    assert_eq!(error.code, "LIST_SCAN_LIMIT_EXCEEDED");
    assert_eq!(visited, ["a", "b"]);
}

/// The FTPS schema types `tls_ca_pem` as a string. A `null` must not be read as
/// an omitted key, which would silently keep only the default trust anchors.
#[test]
fn tls_ca_pem_may_be_omitted_but_not_null() {
    let omitted: FtpConnectionConfig =
        serde_json::from_value(serde_json::json!({"host": "ftp.example.invalid"})).unwrap();
    assert_eq!(omitted.tls_ca_pem, None);
    let present: FtpConnectionConfig = serde_json::from_value(
        serde_json::json!({"host": "ftp.example.invalid", "tls_ca_pem": "pem"}),
    )
    .unwrap();
    assert_eq!(present.tls_ca_pem.as_deref(), Some("pem"));
    assert!(
        serde_json::from_value::<FtpConnectionConfig>(
            serde_json::json!({"host": "ftp.example.invalid", "tls_ca_pem": null})
        )
        .is_err()
    );
}
