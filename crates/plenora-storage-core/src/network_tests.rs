use super::{is_public_address, resolve_network_target};
use std::net::{IpAddr, Ipv4Addr, Ipv6Addr};

#[test]
fn private_and_documentation_addresses_are_not_public() {
    assert!(!is_public_address(IpAddr::V4(Ipv4Addr::LOCALHOST)));
    assert!(!is_public_address(IpAddr::V4(Ipv4Addr::new(10, 0, 0, 1))));
    assert!(!is_public_address(IpAddr::V6(Ipv6Addr::LOCALHOST)));
    assert!(is_public_address(IpAddr::V4(Ipv4Addr::new(1, 1, 1, 1))));
}

#[test]
fn special_address_ranges_and_mapped_ipv4_cannot_bypass_network_policy() {
    for text in [
        "0.1.2.3",
        "0.0.0.0",
        "10.0.0.1",
        "172.16.0.1",
        "192.168.0.1",
        "127.0.0.1",
        "169.254.169.254",
        "192.0.2.1",
        "198.51.100.1",
        "203.0.113.1",
        "255.255.255.255",
        "224.0.0.1",
        "100.64.0.1",
        "100.127.255.254",
        "192.0.0.1",
        "198.18.0.1",
        "198.19.255.254",
        "240.0.0.1",
        "::",
        "::1",
        "ff02::1",
        "fc00::1",
        "fdff::1",
        "fe80::1",
        "febf::1",
        "2001:db8::1",
        "::ffff:127.0.0.1",
        "::ffff:169.254.169.254",
        "::ffff:192.168.0.1",
    ] {
        assert!(!is_public_address(text.parse().unwrap()), "{text}");
    }
    for text in [
        "1.1.1.1",
        "100.63.255.254",
        "100.128.0.1",
        "2606:4700:4700::1111",
        "::ffff:1.1.1.1",
    ] {
        assert!(is_public_address(text.parse().unwrap()), "{text}");
    }
}

#[tokio::test]
async fn hostname_resolution_rejects_loopback_and_errors_without_endpoint_details() {
    use crate::{ErrorCategory, ErrorPhase, RemoteEffect, RetryDisposition};

    let error = resolve_network_target("localhost", 443, false)
        .await
        .unwrap_err();
    assert_eq!(error.code, "PRIVATE_NETWORK_FORBIDDEN");
    let allowed = resolve_network_target("localhost", 443, true)
        .await
        .unwrap();
    assert!(!allowed.is_empty());
    assert!(allowed.iter().all(|address| address.ip().is_loopback()));

    let error = resolve_network_target("1.1.1.1", 0, true)
        .await
        .unwrap_err();
    assert_eq!(error.code, "NETWORK_TARGET_INVALID");
    // NUL is rejected by the local resolver, without depending on external DNS.
    let error = resolve_network_target("private-endpoint\0", 443, true)
        .await
        .unwrap_err();
    assert_eq!(error.code, "DNS_RESOLUTION_FAILED");
    assert_eq!(error.category, ErrorCategory::Transient);
    assert_eq!(error.phase, ErrorPhase::Connect);
    assert_eq!(error.remote_effect, RemoteEffect::None);
    assert_eq!(error.retry, RetryDisposition::Safe);
    assert!(
        !serde_json::to_string(&error)
            .unwrap()
            .contains("private-endpoint")
    );
}

#[tokio::test]
async fn resolution_returns_the_addresses_the_caller_must_dial() {
    let public = resolve_network_target("1.1.1.1", 443, false)
        .await
        .expect("a public literal address is allowed");
    assert_eq!(public.len(), 1);
    assert_eq!(public[0].port(), 443);
    assert_eq!(public[0].ip(), IpAddr::V4(Ipv4Addr::new(1, 1, 1, 1)));

    assert_eq!(
        resolve_network_target("127.0.0.1", 9_000, false)
            .await
            .expect_err("a private literal address must fail closed")
            .code,
        "PRIVATE_NETWORK_FORBIDDEN"
    );
    assert_eq!(
        resolve_network_target("127.0.0.1", 9_000, true)
            .await
            .expect("an authorized private address still resolves")
            .len(),
        1
    );
    assert_eq!(
        resolve_network_target("", 443, true)
            .await
            .expect_err("an empty host is invalid")
            .code,
        "NETWORK_TARGET_INVALID"
    );
}
