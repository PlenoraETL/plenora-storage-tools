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

/// Case 9e of the common runtime matrix: a proved publication whose metadata
/// cannot be read back is `committed` in `cleanup` and is never retried,
/// because repeating the request would publish again.
#[test]
fn unavailable_metadata_after_publication_is_committed_and_never_retried() {
    let error = super::committed_verification_error();
    assert_eq!(
        error.remote_effect,
        plenora_storage_core::RemoteEffect::Committed
    );
    assert_eq!(error.phase, plenora_storage_core::ErrorPhase::Cleanup);
    assert_eq!(error.retry, plenora_storage_core::RetryDisposition::Never);
}

/// A committed object whose size differs from the transferred bytes remains
/// to be reconciled.
#[test]
fn committed_size_mismatch_requires_recovery() {
    let error = super::committed_mismatch_error();
    assert_eq!(
        error.remote_effect,
        plenora_storage_core::RemoteEffect::Committed
    );
    assert_eq!(
        error.retry,
        plenora_storage_core::RetryDisposition::RequiresRecovery
    );
}

fn login_reply(line: &str) -> suppaftp::FtpError {
    let code: u32 = line[..3].parse().expect("test reply code");
    suppaftp::FtpError::UnexpectedResponse(super::Response {
        status: suppaftp::Status::from(code),
        body: line.as_bytes().to_vec(),
    })
}

fn axes(
    error: &plenora_storage_core::StorageError,
) -> (
    plenora_storage_core::ErrorCategory,
    plenora_storage_core::ErrorPhase,
    plenora_storage_core::RemoteEffect,
    plenora_storage_core::RetryDisposition,
) {
    (
        error.category,
        error.phase,
        error.remote_effect,
        error.retry.clone(),
    )
}

/// Only 430, 530 and 532 reject the credentials. 430 is a 4xx code, but
/// retrying rejected credentials would only repeat the rejection.
#[test]
fn login_credential_rejections_are_authentication_never() {
    use plenora_storage_core::{ErrorCategory, ErrorPhase, RemoteEffect, RetryDisposition};
    for reply in [
        "430 Invalid username or password\r\n",
        "530 Login authentication failed\r\n",
        "532 Need account\r\n",
    ] {
        let error = super::map_ftp_auth_error(login_reply(reply));
        assert_eq!(
            axes(&error),
            (
                ErrorCategory::Authentication,
                ErrorPhase::Connect,
                RemoteEffect::None,
                RetryDisposition::Never
            ),
            "{reply}"
        );
        assert_eq!(error.code, "FTP_AUTHENTICATION_FAILED");
    }
}

/// A 4xx reply refuses the login temporarily: pure-ftpd answers
/// `421 32 users (the maximum) are already logged in` under load. Logging in
/// has no remote effect, so the retry is safe. Before 3.0.0 this was
/// `FTP_AUTHENTICATION_FAILED` with retry `never`. 4xx codes the client
/// library does not name (here 499) keep their class.
#[test]
fn login_transient_refusals_are_transient_and_safe() {
    use plenora_storage_core::{ErrorCategory, ErrorPhase, RemoteEffect, RetryDisposition};
    for reply in [
        "421 32 users (the maximum) are already logged in, sorry\r\n",
        "450 Requested action not taken\r\n",
        "499 Unnamed transient reply\r\n",
    ] {
        let error = super::map_ftp_auth_error(login_reply(reply));
        assert_eq!(
            axes(&error),
            (
                ErrorCategory::Transient,
                ErrorPhase::Connect,
                RemoteEffect::None,
                RetryDisposition::Safe
            ),
            "{reply}"
        );
        assert_eq!(error.code, "FTP_LOGIN_TEMPORARILY_REFUSED");
    }
}

/// Unexpected replies stay explicit protocol errors and are never reported
/// as rejected credentials.
#[test]
fn login_unexpected_replies_are_protocol_errors_not_authentication() {
    use plenora_storage_core::{ErrorCategory, ErrorPhase, RemoteEffect, RetryDisposition};
    let unexpected = [
        login_reply("500 Syntax error\r\n"),
        login_reply("550 Unavailable\r\n"),
        login_reply("599 Unnamed permanent reply\r\n"),
        login_reply("220 Unexpected greeting\r\n"),
        suppaftp::FtpError::UnexpectedResponse(super::Response {
            status: suppaftp::Status::Unknown,
            body: b"x1".to_vec(),
        }),
        suppaftp::FtpError::BadResponse,
    ];
    for error in unexpected {
        let mapped = super::map_ftp_auth_error(error);
        assert_eq!(
            axes(&mapped),
            (
                ErrorCategory::Protocol,
                ErrorPhase::Connect,
                RemoteEffect::None,
                RetryDisposition::Never
            )
        );
        assert_eq!(mapped.code, "FTP_LOGIN_UNEXPECTED_RESPONSE");
    }
}

/// A transport failure during login follows the general mapping and is not
/// an authentication failure. Messages carry no server text.
#[test]
fn login_transport_failures_follow_the_general_mapping() {
    use plenora_storage_core::{ErrorCategory, RemoteEffect, RetryDisposition};
    let error = super::map_ftp_auth_error(suppaftp::FtpError::ConnectionError(
        std::io::Error::from(std::io::ErrorKind::ConnectionReset),
    ));
    assert_ne!(error.category, ErrorCategory::Authentication);
    assert_eq!(error.remote_effect, RemoteEffect::None);
    assert_eq!(error.retry, RetryDisposition::Safe);
    let refused = super::map_ftp_auth_error(login_reply(
        "421 32 users (the maximum) are already logged in, sorry\r\n",
    ));
    assert!(!refused.message.contains("32 users"));
    assert!(refused.details.is_empty());
}

fn raw_reply(body: &[u8]) -> suppaftp::FtpError {
    suppaftp::FtpError::UnexpectedResponse(super::Response {
        status: suppaftp::Status::Unknown,
        body: body.to_vec(),
    })
}

/// Replies are parsed strictly (RFC 959 section 4.2): the code that decides
/// is the terminal one, with CRLF or LF line ends.
#[test]
fn login_replies_are_classified_by_their_complete_code() {
    use plenora_storage_core::ErrorCategory;
    let cases: [(&[u8], ErrorCategory); 6] = [
        (b"530 Login incorrect\r\n", ErrorCategory::Authentication),
        (b"530 Login incorrect\n", ErrorCategory::Authentication),
        (b"530\r\n", ErrorCategory::Authentication),
        (
            b"530-Login refused.\r\n Contact the administrator.\r\n530 Login incorrect\r\n",
            ErrorCategory::Authentication,
        ),
        (
            b"421-Too many users\n421 Try again later\n",
            ErrorCategory::Transient,
        ),
        (b"421 Service not available", ErrorCategory::Transient),
    ];
    for (body, expected) in cases {
        let error = super::map_ftp_auth_error(raw_reply(body));
        assert_eq!(
            error.category,
            expected,
            "{}",
            String::from_utf8_lossy(body)
        );
    }
}

/// Any malformed reply is `protocol`/`never`, never `authentication` or
/// `transient`.
#[test]
fn malformed_login_replies_are_protocol_errors() {
    use plenora_storage_core::{ErrorCategory, RetryDisposition};
    let malformed: [&[u8]; 10] = [
        // Empty body.
        b"",
        b"\r\n",
        // Four-digit code.
        b"4210 text\r\n421 end\r\n",
        b"5300 Login incorrect\r\n",
        // A code followed by an invalid character.
        b"530x Login incorrect\r\n",
        b"421\tbusy\r\n",
        // Multiline reply whose terminal code differs from the opening one.
        b"421-start\r\n530 Login incorrect\r\n",
        // Multiline reply without a terminal line.
        b"530-Login refused\r\n",
        // Single-line reply followed by more lines.
        b"530 Login incorrect\r\n421 later\r\n",
        // Terminator before the last line.
        b"530-start\r\n530 middle\r\n530 end\r\n",
    ];
    for body in malformed {
        let error = super::map_ftp_auth_error(raw_reply(body));
        assert_eq!(
            (error.category, error.retry, error.code.as_str()),
            (
                ErrorCategory::Protocol,
                RetryDisposition::Never,
                "FTP_LOGIN_UNEXPECTED_RESPONSE"
            ),
            "{}",
            String::from_utf8_lossy(body)
        );
    }
}
