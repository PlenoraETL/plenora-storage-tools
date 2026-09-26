#![no_main]
libfuzzer_sys::fuzz_target!(|data: &[u8]| {
    plenora_storage_s3::fuzz_listing(data);
});
