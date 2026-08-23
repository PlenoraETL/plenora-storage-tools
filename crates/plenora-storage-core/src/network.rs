use std::net::{IpAddr, Ipv4Addr, Ipv6Addr};

use crate::{
    ErrorCategory, ErrorPhase, RemoteEffect, RetryDisposition, StorageError, StorageResult,
};

pub async fn validate_network_target(
    host: &str,
    port: u16,
    allow_private_network: bool,
) -> StorageResult<()> {
    if host.is_empty() || port == 0 {
        return Err(StorageError::invalid_configuration(
            "NETWORK_TARGET_INVALID",
            "network host and port must be valid",
        ));
    }
    if allow_private_network {
        return Ok(());
    }
    if let Ok(address) = host.parse::<IpAddr>() {
        return if is_public_address(address) {
            Ok(())
        } else {
            private_target_error()
        };
    }
    let addresses = tokio::net::lookup_host((host, port)).await.map_err(|_| {
        StorageError::new(
            ErrorCategory::Transient,
            ErrorPhase::Connect,
            RemoteEffect::None,
            RetryDisposition::Safe,
            "DNS_RESOLUTION_FAILED",
            "storage endpoint DNS resolution failed",
        )
    })?;
    if addresses
        .into_iter()
        .any(|address| !is_public_address(address.ip()))
    {
        return private_target_error();
    }
    Ok(())
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
    use super::is_public_address;
    use std::net::{IpAddr, Ipv4Addr, Ipv6Addr};

    #[test]
    fn private_and_documentation_addresses_are_not_public() {
        assert!(!is_public_address(IpAddr::V4(Ipv4Addr::LOCALHOST)));
        assert!(!is_public_address(IpAddr::V4(Ipv4Addr::new(10, 0, 0, 1))));
        assert!(!is_public_address(IpAddr::V6(Ipv6Addr::LOCALHOST)));
        assert!(is_public_address(IpAddr::V4(Ipv4Addr::new(1, 1, 1, 1))));
    }
}
