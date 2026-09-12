use std::net::{IpAddr, Ipv4Addr, Ipv6Addr, SocketAddr};

use crate::{
    ErrorCategory, ErrorPhase, RemoteEffect, RetryDisposition, StorageError, StorageResult,
};

/// Resolves a network target once and returns the exact addresses the caller
/// must connect to.
///
/// Callers must dial the returned addresses rather than the original host name.
/// Validating a name and then letting the transport resolve it again would let
/// a second, attacker-controlled resolution reach a private address that the
/// policy just rejected.
pub async fn resolve_network_target(
    host: &str,
    port: u16,
    allow_private_network: bool,
) -> StorageResult<Vec<SocketAddr>> {
    if host.is_empty() || port == 0 {
        return Err(StorageError::invalid_configuration(
            "NETWORK_TARGET_INVALID",
            "network host and port must be valid",
        ));
    }
    if let Ok(address) = host.parse::<IpAddr>() {
        if !allow_private_network && !is_public_address(address) {
            return private_target_error();
        }
        return Ok(vec![SocketAddr::new(address, port)]);
    }
    let addresses = tokio::net::lookup_host((host, port))
        .await
        .map_err(|_| dns_resolution_error())?
        .collect::<Vec<_>>();
    if addresses.is_empty() {
        return Err(dns_resolution_error());
    }
    if !allow_private_network
        && addresses
            .iter()
            .any(|address| !is_public_address(address.ip()))
    {
        return private_target_error();
    }
    Ok(addresses)
}

fn dns_resolution_error() -> StorageError {
    StorageError::new(
        ErrorCategory::Transient,
        ErrorPhase::Connect,
        RemoteEffect::None,
        RetryDisposition::Safe,
        "DNS_RESOLUTION_FAILED",
        "storage endpoint DNS resolution failed",
    )
}

fn private_target_error<T>() -> StorageResult<T> {
    Err(StorageError::invalid_configuration(
        "PRIVATE_NETWORK_FORBIDDEN",
        "private-network storage endpoint requires explicit engine authorization",
    ))
}

fn is_public_address(address: IpAddr) -> bool {
    match address {
        IpAddr::V4(address) => is_public_ipv4(address),
        IpAddr::V6(address) => is_public_ipv6(address),
    }
}

fn is_public_ipv4(address: Ipv4Addr) -> bool {
    let octets = address.octets();
    !(address.is_private()
        || address.is_loopback()
        || address.is_link_local()
        || address.is_broadcast()
        || address.is_documentation()
        || address.is_unspecified()
        || address.is_multicast()
        || octets[0] == 0
        || (octets[0] == 100 && (64..=127).contains(&octets[1]))
        || (octets[0] == 192 && octets[1] == 0 && octets[2] == 0)
        || (octets[0] == 198 && (18..=19).contains(&octets[1]))
        || octets[0] >= 240)
}

fn is_public_ipv6(address: Ipv6Addr) -> bool {
    if let Some(mapped) = address.to_ipv4_mapped() {
        return is_public_ipv4(mapped);
    }
    let segments = address.segments();
    !(address.is_loopback()
        || address.is_unspecified()
        || address.is_multicast()
        || (segments[0] & 0xfe00) == 0xfc00
        || (segments[0] & 0xffc0) == 0xfe80
        || (segments[0] == 0x2001 && segments[1] == 0x0db8))
}

#[cfg(test)]
mod tests {
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
}
