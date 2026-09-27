
use super::{is_public_address, resolve_network_target};
use std::net::{IpAddr, Ipv4Addr, Ipv6Addr};

#[test]
fn private_and_documentation_addresses_are_not_public() {
    assert!(!is_public_address(IpAddr::V4(Ipv4Addr::LOCALHOST)));
    assert!(!is_public_address(IpAddr::V4(Ipv4Addr::new(10, 0, 0, 1))));
    assert!(!is_public_address(IpAddr::V6(Ipv6Addr::LOCALHOST)));
    assert!(is_public_address(IpAddr::V4(Ipv4Addr::new(1, 1, 1, 1))));
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
