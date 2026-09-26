#![no_main]
use std::sync::LazyLock;
static RUNTIME: LazyLock<tokio::runtime::Runtime> = LazyLock::new(|| {
    tokio::runtime::Builder::new_current_thread()
        .build()
        .unwrap()
});
libfuzzer_sys::fuzz_target!(|data: &[u8]| {
    RUNTIME.block_on(plenora_storage_ftp::fuzz_listing(data));
});
